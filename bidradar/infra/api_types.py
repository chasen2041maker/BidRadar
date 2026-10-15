"""各模块 schemas.py 共用的几个 Pydantic 类型。

IsoDateTime：接口里的时间统一输出成 2026-10-01T03:56:00Z（UTC，精确到秒），前端不用处理时区。
StrictRequest：请求结构的基类，多传一个不认识的字段就报错，而不是悄悄忽略。
    前端拼错字段名时能马上发现，攻击者也没法塞进一个"role"之类的字段碰运气。
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer

from bidradar.infra import clock

IsoDateTime = Annotated[datetime, PlainSerializer(clock.iso, return_type=str)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
