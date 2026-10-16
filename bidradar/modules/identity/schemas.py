"""identity 接口的请求和响应结构（Pydantic），只给 api.py 用。"""

from pydantic import BaseModel, Field

from bidradar.infra.api_types import StrictRequest

from bidradar.modules.identity.models import User


class LoginRequest(StrictRequest):
    username: str = Field(max_length=200)
    password: str = Field(max_length=2000)


class EmptyRequest(StrictRequest):
    pass


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(id=user.id, username=user.username, display_name=user.display_name)


class LoginResponse(BaseModel):
    user: UserOut


class WorkspaceOut(BaseModel):
    workspace_id: str
    name: str
    role: str


class SessionResponse(BaseModel):
    user: UserOut
    workspaces: list[WorkspaceOut]
    mode: str = "local_development"


class LogoutResponse(BaseModel):
    logged_out: bool = True
