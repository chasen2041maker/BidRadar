"""DeepSeek 文本工具调用适配器。密钥由宿主传入；本模块不建任务、不扣费、不重试。

requested_model 保留用户选定的 deepseek-v4-flash。官方 2026-10-01 文档说明
该旧名已路由到 DeepSeek-V4.1-Flash；每次保存实际 model，不隐瞒供应商升级。
"""
from __future__ import annotations

from http.client import HTTPSConnection, HTTPException
import math
import ssl
import time

from .evidence import canonical, strict_json, redact, EvidenceError

REQUESTED_MODEL = "deepseek-v4-flash"
OFFICIAL_RESOLUTION = "DeepSeek-V4.1-Flash"
PRICE_VERSION = "deepseek-flash-cny-2026-10-01-peak-estimate"
EGRESS_VERSION = "contact-keys-formatted-phones-v2"
VERSION = "deepseek-chat-v2-temperature-zero"
TEMPERATURE = 0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024


class ProviderError(Exception):
    """仅稳定错误码；usage_unknown=True 时调用方必须保留费用预留并停止自动重试。"""
    def __init__(self, code, usage_unknown=False):
        super().__init__(code)
        self.code, self.usage_unknown = code, bool(usage_unknown)


def _count(value):
    return type(value) is int and 0 <= value <= 100_000_000


def prepare_egress(messages, tools):
    """返回脱敏深副本，供宿主在hash/预留/保存attempt之前调用，供应商出口再次兜底。

    覆盖整个消息历史和工具schema，而非仅首次证据。JSON内容/arguments先解析再递归
    处理，避免邮箱/联系标签的正则吞掉JSON分隔符；工具协议标识与原始引用ID不变。
    本函数不接受额外的外发内容通道；未知结构/重复JSON键均在请求前拒绝。
    """
    try:
        value = strict_json(canonical({"messages": messages, "tools": tools}), limit=MAX_REQUEST_BYTES)
        if not isinstance(value["messages"], list) or not isinstance(value["tools"], list):
            raise ValueError()

        def clean(item, key=None):
            if isinstance(item, dict):
                result = {}
                for name, child in item.items():
                    # 用户/工具的嵌入JSON也可能把联系方式放在属性名里；值脱敏不够。
                    # 两个键清理后重名则拒绝请求，不能静默覆盖采购字段或改变工具语义。
                    clean_name = redact(name)
                    if clean_name in result:
                        raise ValueError()
                    result[clean_name] = clean(child, name)
                return result
            if isinstance(item, list):
                return [clean(child) for child in item]
            if isinstance(item, str):
                if key == "arguments":
                    parsed = strict_json(item, limit=MAX_REQUEST_BYTES)
                    if not isinstance(parsed, dict):
                        raise ValueError()
                    return canonical(clean(parsed))
                if key == "content" and item.lstrip().startswith(("{", "[")):
                    # 普通自然语言允许以括号开头；真正JSON必须按结构脱敏，保持工具结果可读。
                    try:
                        parsed = strict_json(item, limit=MAX_REQUEST_BYTES)
                    except EvidenceError:
                        return redact(item)
                    return canonical(clean(parsed))
                return redact(item)
            return item

        result = clean(value)
        return result["messages"], result["tools"]
    except (ValueError, TypeError, RecursionError, EvidenceError):
        raise ProviderError("invalid_model_request") from None


def usage_cost(usage, peak=True):
    """返回估算 micro-CNY（向上取整），无可用完整用量返回 None；不重复加思考token。

    output_tokens 对应 completion_tokens，已含 reasoning_tokens。缓存仅按供应商
    usage 实报结算；预留时不假定命中。价目页：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
    """
    if (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "cached_tokens"}
            or any(not _count(v) for v in usage.values()) or usage["cached_tokens"] > usage["input_tokens"]):
        return None
    miss, hit, output = usage["input_tokens"] - usage["cached_tokens"], usage["cached_tokens"], usage["output_tokens"]
    numerator = miss * 200 + hit * 4 + output * 800
    return (numerator + (99 if peak else 199)) // (100 if peak else 200)


def estimate_tokens(messages, tools):
    """UTF-8字节数加协议余量的保守估计，不宣称是供应商 tokenizer 的精确计数。"""
    try:
        size = len(canonical({"messages": messages, "tools": tools}).encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("invalid_model_request") from None
    if not isinstance(messages, list) or not isinstance(tools, list) or size > MAX_REQUEST_BYTES:
        raise ProviderError("model_request_too_large")
    return size + 4096 + 128 * len(messages) + 512 * len(tools)


def estimate_reservation(messages, tools, max_output_tokens=4096):
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096:
        raise ProviderError("invalid_output_limit")
    return usage_cost({"input_tokens": estimate_tokens(messages, tools), "output_tokens": max_output_tokens,
                       "cached_tokens": 0})


def _usage(value):
    if not isinstance(value, dict):
        return None
    keys = ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "total_tokens")
    if (any(not _count(value.get(k)) for k in keys)
            or value["prompt_tokens"] != value["prompt_cache_hit_tokens"] + value["prompt_cache_miss_tokens"]
            or value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]):
        return None
    return {"input_tokens": value["prompt_tokens"], "output_tokens": value["completion_tokens"],
            "cached_tokens": value["prompt_cache_hit_tokens"]}


class DeepSeekProvider:
    """固定 HTTPS 主机、禁代理/重定向、非思考模式、总截止；exchange 仅供可信测试注入。

    exchange(payload: bytes, timeout: float) -> (status: int, headers: dict, body: bytes)
    注入器本身属于受信程序，不能从请求/模型/配置JSON加载。默认实现只访问官方端点。
    """
    def __init__(self, api_key, *, requested_model=REQUESTED_MODEL, timeout=60, max_output_tokens=4096, exchange=None):
        if not isinstance(api_key, str) or not 8 <= len(api_key) <= 512 or any(ord(c) <= 32 or ord(c) >= 127 for c in api_key):
            raise ProviderError("invalid_api_key")
        if (requested_model != REQUESTED_MODEL or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 60
                or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096):
            raise ProviderError("invalid_provider_config")
        self._key, self.timeout, self.max_output_tokens = api_key, timeout, max_output_tokens
        self.requested_model = requested_model
        self._exchange = exchange or self._https_exchange

    @property
    def metadata(self):
        return {"provider": "deepseek", "requested_model": self.requested_model,
                "official_resolution": OFFICIAL_RESOLUTION, "price_version": PRICE_VERSION,
                "provider_version": VERSION, "temperature": TEMPERATURE,
                "thinking": "disabled", "max_output_tokens": self.max_output_tokens}

    def _https_exchange(self, payload, timeout):
        deadline = time.monotonic() + timeout
        connection = HTTPSConnection("api.deepseek.com", 443, timeout=timeout, context=ssl.create_default_context())
        response = None
        try:
            connection.request("POST", "/chat/completions", body=payload, headers={
                "Authorization": "Bearer " + self._key, "Content-Type": "application/json",
                "Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close"})
            def remaining():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                sock = connection.sock
                if sock is None and response is not None and response.fp is not None:
                    sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(left)
            remaining()
            response = connection.getresponse()
            headers = {}
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in headers and lower in ("content-length", "content-type", "content-encoding", "transfer-encoding"):
                    raise ProviderError("invalid_provider_headers", True)
                headers[lower] = value
            if "content-length" in headers and "transfer-encoding" in headers:
                raise ProviderError("invalid_provider_headers", True)
            if "content-length" in headers:
                try:
                    length = int(headers["content-length"])
                except ValueError:
                    raise ProviderError("invalid_provider_headers", True) from None
                if not 0 <= length <= MAX_RESPONSE_BYTES:
                    raise ProviderError("provider_response_too_large", True)
            chunks, size = [], 0
            # DeepSeek的非流式keep-alive可能不断返回空行；每次读仍使用同一总截止。
            while True:
                remaining()
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ProviderError("provider_response_too_large", True)
            if "content-length" in headers and size != int(headers["content-length"]):
                raise ProviderError("provider_incomplete_response", True)
            return response.status, headers, b"".join(chunks)
        finally:
            if response is not None:
                response.close()
            connection.close()

    def complete(self, messages, tools):
        messages, tools = prepare_egress(messages, tools)
        estimate_tokens(messages, tools)
        payload = {"model": self.requested_model, "messages": messages, "stream": False,
                   "thinking": {"type": "disabled"}, "max_tokens": self.max_output_tokens,
                   "temperature": TEMPERATURE}
        # 温度只降低非思考模式的采样随机性，不保证确定性或事实正确；metadata同值参与计费身份。
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        else:
            # 语义审查节点只需JSON，不开放工具；system须包含JSON字样。
            payload["response_format"] = {"type": "json_object"}
        raw = canonical(payload).encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ProviderError("model_request_too_large")
        try:
            status, headers, body = self._exchange(raw, self.timeout)
        except ProviderError:
            raise
        except (OSError, HTTPException, TimeoutError):
            raise ProviderError("provider_network_error", True) from None
        if status != 200:
            # 服务器拒绝与结果不明分别记录；不把上游错误正文/密钥拼进异常。
            code = {400: "provider_bad_request", 401: "provider_unauthorized", 402: "provider_insufficient_balance",
                    403: "provider_forbidden", 404: "provider_model_unavailable", 429: "provider_rate_limited"}.get(status, "provider_unavailable")
            raise ProviderError(code, status not in (400, 401, 402, 403, 404, 429))
        if (not isinstance(headers, dict) or headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
                or headers.get("content-encoding", "identity").lower() != "identity"
                or not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES):
            raise ProviderError("invalid_provider_response", True)
        try:
            result = strict_json(body.decode("utf-8"), limit=MAX_RESPONSE_BYTES)
            if (not isinstance(result, dict) or not isinstance(result.get("choices"), list) or len(result["choices"]) != 1
                    or result.get("model") not in (REQUESTED_MODEL, "deepseek-flash", "deepseek-v4.1-flash")
                    or not isinstance(result.get("id"), str) or not 1 <= len(result["id"]) <= 256):
                raise ValueError()
            choice = result["choices"][0]
            message = choice["message"]
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or message.get("content") is not None and not isinstance(message["content"], str)
                    or choice.get("finish_reason") not in ("stop", "length", "tool_calls", "content_filter", "insufficient_system_resource")):
                raise ValueError()
            calls = message.get("tool_calls")
            if calls is not None and (not isinstance(calls, list) or len(calls) > 16):
                raise ValueError()
            safe_message = {"role": "assistant", "content": message.get("content")}
            if calls:
                safe_message["tool_calls"] = calls
            # 非思考模式出现思考内容是协议偏离，不能悄悄持久化或送下一轮。
            if message.get("reasoning_content"):
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError, EvidenceError):
            raise ProviderError("invalid_provider_response", True) from None
        return {"message": safe_message, "usage": _usage(result.get("usage")), "model": result["model"],
                "provider_request_id": result["id"], "finish_reason": choice["finish_reason"],
                "requested_model": self.requested_model, "official_resolution": OFFICIAL_RESOLUTION,
                "system_fingerprint": result.get("system_fingerprint")}

    __call__ = complete
