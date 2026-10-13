"""公司内共享资源的权限规则（纯函数，不查数据库）。

这是最早写的一小段权限规则，目前只覆盖"查看报告"和"发起分析"两个动作。
它没有接入实际的请求流程；线上权限检查在 workspace/store.py 的 _authorize 和 authorize 里。
"""
from dataclasses import dataclass
from enum import Enum


class Role(Enum):
    """公司内的角色。注意：平台管理员不是公司管理员，不会自动拥有公司权限。"""

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Action(Enum):
    """目前支持的动作：查看报告、发起分析。"""

    VIEW_REPORT = "view_report"
    REQUEST_ANALYSIS = "request_analysis"


class Visibility(Enum):
    """资源的可见范围。受限材料（以及由它生成的报告）不按普通公司资源处理。"""

    COMPANY = "company"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class Membership:
    """当前成员关系。必须由服务端从数据库读取，不能用前端提交的值。"""

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
    """判断一个成员能否对本公司的普通资源执行某个动作。

    规则：
        - 不是有效成员、跨公司访问、受限资源：一律拒绝。
        - 查看报告：三种角色都可以。
        - 发起分析：管理员和成员可以，只读成员不行。
        - 其他动作：拒绝。
    resource_workspace_id 和 visibility 要由资源所在的服务查出来，不能用请求里的值。
    返回 True 只代表角色规则通过；登录、材料权限、预算等检查在别处完成。
    """
    # 没有成员关系、已被移出、active 不是真正的 True（比如字符串 "true"），都拒绝。
    if not isinstance(membership, Membership) or membership.active is not True:
        return False
    identifiers = (membership.user_id, membership.workspace_id, resource_workspace_id)
    if any(not isinstance(value, str) or not value.strip() for value in identifiers):
        return False

    # 先挡住跨公司访问。管理员也只能管本公司。
    if membership.workspace_id != resource_workspace_id:
        return False
    if not isinstance(membership.role, Role) or visibility is not Visibility.COMPANY:
        return False

    # 逐个列出允许的动作；只读成员能看报告，但不能发起分析。
    if action is Action.VIEW_REPORT:
        return membership.role in (Role.ADMIN, Role.MEMBER, Role.VIEWER)
    if action is Action.REQUEST_ANALYSIS:
        return membership.role in (Role.ADMIN, Role.MEMBER)
    return False  # 没列出的动作一律拒绝。
