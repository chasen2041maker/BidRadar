"""业务错误：只带一个错误码和 HTTP 状态码。

为什么不直接抛 HTTPException：service 也给 Worker 调用，Worker 里没有 HTTP。
api 层统一把 AppError 转成 {"error": {"code": ..., "message": ...}}（见 app/main.py），前端按错误码翻译成中文提示。

错误码里绝不带 SQL、密码、令牌、私有数据或别家公司是否存在这类信息。
"""


class AppError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


# 几个常用的错误，统一错误码，避免各模块各写一套拼法。
def not_found() -> AppError:
    """资源不存在，或者不属于当前公司。两种情况返回同一个错误，别人就猜不出别家公司有什么。"""
    return AppError("not_found", 404)


def forbidden() -> AppError:
    return AppError("forbidden", 403)


def conflict(code: str) -> AppError:
    return AppError(code, 409)
