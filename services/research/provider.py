"""DeepSeek 接口适配：把消息和工具发给 DeepSeek，把回复整理成统一格式。

这个模块只管"发请求、收响应"，不建任务、不记账、不重试。密钥由调用方传进来。

关于模型名：请求里写的是选定的 deepseek-v4-flash。按 DeepSeek 2026-10-01 的文档，
这个名字已被路由到 DeepSeek-V4.1-Flash，所以每次都会记下响应里实际返回的模型名。

prepare_egress 负责在外发前去掉联系方式；usage_cost 和 estimate_reservation 负责算钱。
"""
from __future__ import annotations

from http.client import HTTPSConnection, HTTPException
import math
import ssl
import time

from .evidence import canonical, strict_json, redact, EvidenceError

# 这些值会冻结进任务配置，任何一个变了，旧任务都不能用新参数续跑。
REQUESTED_MODEL = "deepseek-v4-flash"
OFFICIAL_RESOLUTION = "DeepSeek-V4.1-Flash"
PRICE_VERSION = "deepseek-flash-cny-2026-10-01-peak-estimate"
EGRESS_VERSION = "contact-keys-formatted-phones-v2"
VERSION = "deepseek-chat-v2-temperature-zero"
TEMPERATURE = 0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024


class ProviderError(Exception):
    """调用模型出错。code 是错误码。

    usage_unknown=True 表示请求可能已经被处理并计费，但不知道花了多少：
    调用方必须保留预留的费用，并且不能自动重试。
    """
    def __init__(self, code, usage_unknown=False):
        super().__init__(code)
        self.code, self.usage_unknown = code, bool(usage_unknown)


def _count(value):
    return type(value) is int and 0 <= value <= 100_000_000


def prepare_egress(messages, tools):
    """返回去掉联系方式后的消息和工具副本，原对象不变。

    处理范围是整段对话历史和工具定义，不只是第一条消息。
    消息内容如果本身是 JSON，先解析再逐个字段处理，免得正则把 JSON 的引号、逗号一起删掉。
    工具调用的 ID、证据 ID 这类标识保持原样。结构不认识或有重复键时直接拒绝。
    """
    try:
        value = strict_json(canonical({"messages": messages, "tools": tools}), limit=MAX_REQUEST_BYTES)
        if not isinstance(value["messages"], list) or not isinstance(value["tools"], list):
            raise ValueError()

        def clean(item, key=None):
            if isinstance(item, dict):
                result = {}
                for name, child in item.items():
                    # 联系方式也可能出现在键名里，所以键名也要处理。
                    # 处理后两个键重名就拒绝请求，不能悄悄覆盖掉其中一个。
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
                    # 以括号开头的内容先试着按 JSON 解析；解析不了就当普通文本处理。
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
    """按用量估算费用，单位 micro-CNY，向上取整。用量不完整时返回 None。

    价格（峰时，每百万 token）：未命中缓存的输入 2 元，命中缓存的输入 0.04 元，输出 8 元。
    价目页：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
    预留时假设缓存全部未命中；结算时按供应商实际报告的命中数算。
    """
    if (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "cached_tokens"}
            or any(not _count(v) for v in usage.values()) or usage["cached_tokens"] > usage["input_tokens"]):
        return None
    miss, hit, output = usage["input_tokens"] - usage["cached_tokens"], usage["cached_tokens"], usage["output_tokens"]
    numerator = miss * 200 + hit * 4 + output * 800
    return (numerator + (99 if peak else 199)) // (100 if peak else 200)


def estimate_tokens(messages, tools):
    """粗略估计输入 token 数：按 UTF-8 字节数再加一些余量。宁可多估，不是精确值。"""
    try:
        size = len(canonical({"messages": messages, "tools": tools}).encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        raise ProviderError("invalid_model_request") from None
    if not isinstance(messages, list) or not isinstance(tools, list) or size > MAX_REQUEST_BYTES:
        raise ProviderError("model_request_too_large")
    return size + 4096 + 128 * len(messages) + 512 * len(tools)


def estimate_reservation(messages, tools, max_output_tokens=4096):
    """一次调用需要预留多少钱：输入按估计值、输出按上限、缓存按全部未命中来算。"""
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096:
        raise ProviderError("invalid_output_limit")
    return usage_cost({"input_tokens": estimate_tokens(messages, tools), "output_tokens": max_output_tokens,
                       "cached_tokens": 0})


def _usage(value):
    """把 DeepSeek 返回的 usage 转成 {input_tokens, output_tokens, cached_tokens}；数字对不上就返回 None。"""
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
    """DeepSeek 客户端。

    安全上的约束：只连接 api.deepseek.com，不走代理、不跟随重定向；关闭思考模式；
    整个请求有总的超时时间；响应大小有上限。

    exchange 参数只给测试用，用来注入假的网络层：
        exchange(payload: bytes, timeout: float) -> (status, headers, body)
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
        """当前的模型参数。会参与请求指纹的计算，也会冻结进任务配置。"""
        return {"provider": "deepseek", "requested_model": self.requested_model,
                "official_resolution": OFFICIAL_RESOLUTION, "price_version": PRICE_VERSION,
                "provider_version": VERSION, "temperature": TEMPERATURE,
                "thinking": "disabled", "max_output_tokens": self.max_output_tokens}

    def _https_exchange(self, payload, timeout):
        """真正发 HTTPS 请求。每次读数据前都按剩余时间重设超时，保证总耗时不超过 timeout。"""
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
            # DeepSeek 在生成期间可能不断发送空行保持连接，所以每次读之前都要检查总截止时间。
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
        """发送一次对话请求，返回 {message, usage, model, provider_request_id, finish_reason, ...}。

        有工具时开放工具调用；没有工具时（语义核验）要求模型只返回 JSON。
        出错时抛 ProviderError，并标明费用是否可能已经发生。
        """
        messages, tools = prepare_egress(messages, tools)
        estimate_tokens(messages, tools)
        payload = {"model": self.requested_model, "messages": messages, "stream": False,
                   "thinking": {"type": "disabled"}, "max_tokens": self.max_output_tokens,
                   "temperature": TEMPERATURE}
        # 温度设为 0 只是降低随机性，同样的输入仍可能得到不同输出，也不保证正确。
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        else:
            # 语义核验只要 JSON，不需要工具。（DeepSeek 要求此时系统提示词里出现"JSON"字样。）
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
            # 400/401/402/403/404/429 是明确的拒绝，请求没被处理；其他状态码说明结果不确定。
            # 异常里只放错误码，不放供应商返回的正文，更不会带上密钥。
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
            # 已经关闭思考模式，如果还返回了思考内容，说明协议出了问题，按异常处理。
            if message.get("reasoning_content"):
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError, EvidenceError):
            raise ProviderError("invalid_provider_response", True) from None
        return {"message": safe_message, "usage": _usage(result.get("usage")), "model": result["model"],
                "provider_request_id": result["id"], "finish_reason": choice["finish_reason"],
                "requested_model": self.requested_model, "official_resolution": OFFICIAL_RESOLUTION,
                "system_fingerprint": result.get("system_fingerprint")}

    __call__ = complete
