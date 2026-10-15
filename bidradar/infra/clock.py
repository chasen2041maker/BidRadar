"""当前时间。所有代码都从这里取时间，测试才能把时间"拨快"。

用法：from bidradar.infra import clock; clock.now()
测试里：monkeypatch.setattr(clock, "now", lambda: 某个固定时间)

只能写成 clock.now() 调用，不能 from bidradar.infra.clock import now：
后者在 import 时就把函数绑死了，测试替换不到。
"""

from datetime import UTC, datetime


def now() -> datetime:
    """带时区的 UTC 时间。数据库列都是 timestamptz，不存不带时区的时间。"""
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    """转成接口统一用的格式：2026-10-01T03:56:00Z（UTC，精确到秒）。"""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
