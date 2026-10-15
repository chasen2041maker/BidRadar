"""catalog 的 HTTP 路由：搜索公告、公告详情。

公告是公开数据，但接口挂在 /api/workspaces/{公司}/ 下面：只有登录了、并且是这家公司的成员才能看。
这样以后给目录加"本公司的备注""是否已研究过"之类的私有信息时，权限已经在位。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from bidradar.app.deps import CurrentUser, DbSession, WorkspaceId
from bidradar.modules.catalog import service
from bidradar.modules.workspace import service as workspace

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["catalog"])


@router.get("/notices")
def search_notices(
    workspace_id: WorkspaceId,
    user: CurrentUser,
    session: DbSession,
    keyword: Annotated[str, Query(max_length=200)] = "",
    kind: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    simulation: bool = False,
    cursor: Annotated[str | None, Query(max_length=2000)] = None,
) -> dict[str, Any]:
    workspace.authorize(session, user.id, workspace_id, "read")
    return service.search(
        session, keyword=keyword, kind=kind or None, simulation=simulation, page_size=page_size, cursor=cursor
    )


@router.get("/notices/{notice_id}")
def get_notice(
    workspace_id: WorkspaceId,
    notice_id: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    workspace.authorize(session, user.id, workspace_id, "read")
    return service.get_notice(session, notice_id)
