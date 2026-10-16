"""调用大模型：LLMClient 接口，以及几个实现。

    LLMClient      接口：complete(请求) → 响应。流水线只认这个接口，换模型只换一个实现类
    FakeLLM        测试和离线评测用：按脚本返回响应，不联网、不花钱
    DisabledLLM    没开启模型时用：一调就报 ModelDisabled
    DeepSeekClient 真实调用（OpenAI 兼容接口，JSON 模式）。默认不启用，开启要项目负责人同意
    BudgetedLLM    包在真实实现外面：先预留费用 → 调用 → 按实际用量结算

费用规则沿用旧版（这是旧版做得好的部分，见 docs/agent.md 的"费用控制"）：
    - 调用前按"输入估计 + 输出上限 + 缓存全部未命中"的价格预留费用，超过额度就不发。
    - 结果不确定（超时、断网）时，预留额照算已花，抛 CallOutcomeUnknown，任务停下等人处理，不自动重试。
      重试可能让同一个请求被处理、计费两次。
    - 续跑时先按 call_key 查账本：已经结算过的调用，直接取回当时的响应，不再发送。

账本本身（Ledger）由 research 模块实现，存在数据库里。agent 只认这个接口，不碰数据库。
"""

import http.client
import json
import math
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from bidradar.agent.schemas import Frozen

Purpose = Literal["extract", "extract_retry", "extract_recall", "match", "verify", "summary", "followup"]


class LLMRequest(Frozen):
    purpose: Purpose
    # 稳定的调用键 = 运行 ID + 步骤名 + 第几次调用。续跑时靠它在账本里找回已付费的响应。
    call_key: str
    system: str
    # 外部内容（招标文件原文、档案）都放在 user 里，作为 JSON 数据，不放进 system。
    user: str
    max_output_tokens: int = 2048
    temperature: float = 0.0


class Usage(Frozen):
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0


class LLMResponse(Frozen):
    text: str
    usage: Usage | None
    model: str
    request_id: str | None = None
    # True 表示这是从账本里取回的旧响应（续跑），这次没有真的发请求
    replayed: bool = False


class LLMError(Exception):
    code = "llm_error"

    def __init__(self, code: str | None = None) -> None:
        if code:
            self.code = code
        super().__init__(self.code)


class ModelDisabled(LLMError):
    code = "model_disabled"


class BudgetExceeded(LLMError):
    """额度不够，请求没有发出去。"""

    code = "budget_exceeded"


class CallOutcomeUnknown(LLMError):
    """请求可能已经被处理并计费，但不知道结果。不能自动重试。"""

    code = "billing_unknown"


class CallFailed(LLMError):
    """请求确定没有被处理（比如连接被拒、返回 4xx），没有产生费用，可以按步骤的规则重试。"""

    code = "model_call_failed"


class InvalidModelOutput(LLMError):
    """模型返回了，但内容不是要求的 JSON 结构。钱已经花了。"""

    code = "invalid_model_output"


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


T = TypeVar("T", bound=BaseModel)


def parse_json(response: LLMResponse, model: type[T]) -> T:
    """把模型输出解析成指定结构。不是合法 JSON、或者结构不对，都抛 InvalidModelOutput。

    模型有时会用 ```json 代码块包起来，先去掉再解析；其余一律不做"猜测修复"。
    """
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    try:
        return model.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        raise InvalidModelOutput() from error


# ---- 价格和估算 ----


@dataclass(frozen=True)
class Pricing:
    """每百万 token 的价格，单位 micro-CNY（百万分之一元，全部用整数，避免小数误差）。

    默认值是旧版用过的 DeepSeek 峰时价格（2026-10-01 核对）。接入前请到官方价目页再核对一次：
    https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
    """

    input_miss: int = 2_000_000
    input_hit: int = 40_000
    output: int = 8_000_000
    version: str = "deepseek-flash-cny-2026-10-01-peak"

    def cost(self, usage: Usage) -> int:
        miss = usage.input_tokens - usage.cached_tokens
        total = miss * self.input_miss + usage.cached_tokens * self.input_hit + usage.output_tokens * self.output
        return math.ceil(total / 1_000_000)

    def estimate(self, request: LLMRequest) -> int:
        """预留额：输入按估计值、输出按上限、缓存按全部未命中。宁可多估。"""
        return self.cost(Usage(input_tokens=estimate_tokens(request), output_tokens=request.max_output_tokens))


def estimate_tokens(request: LLMRequest) -> int:
    """粗略估计输入 token 数：按 UTF-8 字节数再加一点余量。中文一个字约 3 字节、约 0.7 个 token，所以偏多估。"""
    return len((request.system + request.user).encode("utf-8")) // 2 + 64


# ---- 账本接口和带预算的客户端 ----


class Ledger(Protocol):
    def lookup(self, call_key: str) -> LLMResponse | None:
        """这个调用之前已经结算过，返回当时的响应；没有返回 None。
        之前那次还在进行中或结果不确定，抛 CallOutcomeUnknown（不允许再发一次）。"""

    def reserve(self, request: LLMRequest, amount: int) -> None:
        """预留费用。超过任务、公司或总额度时抛 BudgetExceeded。"""

    def settle(self, request: LLMRequest, response: LLMResponse, cost: int) -> None: ...

    def mark_unknown(self, request: LLMRequest) -> None: ...

    def release(self, request: LLMRequest) -> None:
        """请求确定没有发出去：释放预留额。"""


class BudgetedLLM:
    def __init__(self, inner: LLMClient, ledger: Ledger, pricing: Pricing) -> None:
        self.inner, self.ledger, self.pricing = inner, ledger, pricing

    def complete(self, request: LLMRequest) -> LLMResponse:
        replayed = self.ledger.lookup(request.call_key)
        if replayed is not None:
            return replayed.model_copy(update={"replayed": True})
        self.ledger.reserve(request, self.pricing.estimate(request))
        try:
            response = self.inner.complete(request)
        except CallOutcomeUnknown:
            self.ledger.mark_unknown(request)
            raise
        except (CallFailed, ModelDisabled):
            self.ledger.release(request)
            raise
        except BaseException:
            # 不知道请求有没有发出去（比如进程被 Ctrl+C 打断）：按"结果不确定"处理，宁可多算。
            self.ledger.mark_unknown(request)
            raise
        if response.usage is None:
            # 供应商没给用量：按预留额记，不能当作免费。
            self.ledger.settle(request, response, self.pricing.estimate(request))
        else:
            self.ledger.settle(request, response, self.pricing.cost(response.usage))
        return response


# ---- 不联网的实现 ----


class DisabledLLM:
    def complete(self, request: LLMRequest) -> LLMResponse:
        raise ModelDisabled()


@dataclass
class FakeLLM:
    """按脚本返回响应。responder 收到请求，返回字符串或可以转成 JSON 的对象。

    calls 记下所有请求，测试可以检查"发了几次、每次发了什么"。
    """

    responder: Callable[[LLMRequest], Any]
    model: str = "fake"
    calls: list[LLMRequest] = field(default_factory=list)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        value = self.responder(request)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        usage = Usage(input_tokens=estimate_tokens(request), output_tokens=len(text) // 2 + 1)
        return LLMResponse(text=text, usage=usage, model=self.model)


# ---- DeepSeek ----


class DeepSeekClient:
    """DeepSeek 的 OpenAI 兼容接口，JSON 模式，温度 0。

    密钥从文件读取（默认 ~/.bidradar/credentials/deepseek-api-key.txt），不出现在日志、URL、错误信息里。
    超时、连接中途断开、响应不完整，一律当作"结果不确定"（CallOutcomeUnknown）：请求可能已经被处理了。
    只有确定没发出去（连不上）或者服务端明确拒绝（4xx），才当作 CallFailed。
    """

    HOST = "api.deepseek.com"
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(self, api_key: str, *, model: str = "deepseek-chat", timeout: float = 60.0) -> None:
        if not api_key or len(api_key) > 256 or any(c.isspace() for c in api_key):
            raise ValueError("invalid_api_key")
        self._key, self.model, self.timeout = api_key, model, timeout

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
                "response_format": {"type": "json_object"},
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        connection = http.client.HTTPSConnection(
            self.HOST, 443, timeout=self.timeout, context=ssl.create_default_context()
        )
        started = time.monotonic()
        try:
            try:
                connection.connect()
            except OSError:
                raise CallFailed("model_unreachable") from None
            try:
                connection.request(
                    "POST",
                    "/chat/completions",
                    body=payload,
                    headers={
                        "Authorization": "Bearer " + self._key,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                response = connection.getresponse()
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
            except (OSError, socket.timeout, http.client.HTTPException):
                raise CallOutcomeUnknown() from None
            if len(body) > self.MAX_RESPONSE_BYTES or time.monotonic() - started > self.timeout:
                raise CallOutcomeUnknown()
            if 400 <= response.status < 500 and response.status != 408:
                raise CallFailed(f"model_http_{response.status}")
            if response.status != 200:
                raise CallOutcomeUnknown()
            try:
                value = json.loads(body)
                text = value["choices"][0]["message"]["content"]
                usage = value.get("usage") or {}
                parsed_usage = Usage(
                    input_tokens=usage["prompt_tokens"],
                    output_tokens=usage["completion_tokens"],
                    cached_tokens=usage.get("prompt_cache_hit_tokens", 0),
                )
            except (ValueError, KeyError, IndexError, TypeError, ValidationError):
                raise InvalidModelOutput() from None
            return LLMResponse(
                text=text, usage=parsed_usage, model=value.get("model", self.model), request_id=value.get("id")
            )
        finally:
            connection.close()
