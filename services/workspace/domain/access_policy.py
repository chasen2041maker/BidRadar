"""公司共享资源的第一段权限规则；纯函数，不是已接通的登录或鉴权服务。"""
from dataclasses import dataclass
from enum import Enum


class Role(Enum):
    """这是公司内角色；平台管理员不能冒充公司管理员。"""

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Action(Enum):
    """本切片只实现查看普通报告和请求分析，不提前铺开全部权限。"""

    VIEW_REPORT = "view_report"
    REQUEST_ANALYSIS = "request_analysis"


class Visibility(Enum):
    """受限材料及其派生报告不能降成普通公司共享资源。"""

    COMPANY = "company"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class Membership:
    """由服务端读取的当前成员关系，不能直接信任前端提交的这些字段。"""

    user_id: str
    workspace_id: str
    role: Role
    active: bool


def allows_shared_action(
    membership: Membership | None,
    resource_workspace_id: str,
    action: Action,
    visibility: Visibility,
) -> bool:
    """判断当前成员对普通公司资源是否满足这两种动作的基础权限。

    resource_workspace_id 和 visibility 须由资源拥有者查实，不用请求里的值冒充。
    True 只表示本地成员/角色规则通过；身份验证、当前授权查询、输入材料权限、
    外发许可和预算检查仍由之后的调用链完成。本函数不查数据库或消耗模型额度。
    受限资源暂不开放；后续有明确资源授权规则及测试后再单独实现。
    """
    # 缺成员、已移除或伪造的非布尔 active 值，均不能靠“看起来像真”放行。
    if not isinstance(membership, Membership) or membership.active is not True:
        return False
    identifiers = (membership.user_id, membership.workspace_id, resource_workspace_id)
    if any(not isinstance(value, str) or not value.strip() for value in identifiers):
        return False

    # 先挡住跨公司访问；管理员也只管理本公司，不能凭角色跳过这一关。
    if membership.workspace_id != resource_workspace_id:
        return False
    if not isinstance(membership.role, Role) or visibility is not Visibility.COMPANY:
        return False

    # 明确列出允许动作：只读成员能看，但不能发起新的模型分析。
    if action is Action.VIEW_REPORT:
        return membership.role in (Role.ADMIN, Role.MEMBER, Role.VIEWER)
    if action is Action.REQUEST_ANALYSIS:
        return membership.role in (Role.ADMIN, Role.MEMBER)
    return False  # 未实现或未知的动作默认拒绝，不用“非只读就全放行”。
