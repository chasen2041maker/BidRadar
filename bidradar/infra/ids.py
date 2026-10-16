"""ID 和指纹。

业务 ID 用随机的 32 位十六进制（uuid4），猜不出来，也不暴露"一共有多少条"。
指纹（fingerprint）是内容的 SHA-256：同样的内容永远得到同一个指纹，用来去重和比对请求是否相同。
"""

import hashlib
import json
import uuid
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


def canonical_json(value: Any) -> str:
    """固定格式的 JSON：键排序、无多余空格、不允许 NaN。同样的数据永远序列化成同样的字符串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def fingerprint(value: Any) -> str:
    """任意 JSON 数据的 SHA-256（十六进制）。"""
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
