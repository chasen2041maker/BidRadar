"""用两家虚构公司验证基础权限；不冒充数据库、会话或真实撤权集成测试。"""
from dataclasses import replace
import unittest

from services.workspace.domain.access_policy import (
    Action, Membership, Role, Visibility, allows_shared_action,
)


class SharedAccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.viewer = Membership("user-a", "company-a", Role.VIEWER, True)

    def allowed(self, member, action=Action.VIEW_REPORT, company="company-a",
                visibility=Visibility.COMPANY):
        return allows_shared_action(member, company, action, visibility)

    def test_viewer_can_read_but_cannot_request_analysis(self):
        # 同一个成员和公司，只改操作：查看已有报告不应顺便获得调用模型的权力。
        self.assertTrue(self.allowed(self.viewer))
        self.assertFalse(self.allowed(self.viewer, Action.REQUEST_ANALYSIS))

    def test_three_roles_match_the_two_action_matrix(self):
        for role in Role:
            with self.subTest(role=role):
                member = replace(self.viewer, role=role)
                self.assertTrue(self.allowed(member))
                self.assertEqual(
                    self.allowed(member, Action.REQUEST_ANALYSIS),
                    role in (Role.ADMIN, Role.MEMBER),
                )

    def test_other_company_denied_even_for_admin(self):
        for role in Role:
            for action in Action:
                with self.subTest(role=role, action=action):
                    self.assertFalse(self.allowed(
                        replace(self.viewer, role=role), action, "company-b"
                    ))

    def test_removed_member_and_missing_membership_denied(self):
        for member in (None, replace(self.viewer, active=False)):
            for action in Action:
                with self.subTest(member=member, action=action):
                    self.assertFalse(self.allowed(member, action))

    def test_restricted_resources_denied_for_all_roles(self):
        # 尚未实现逐资源授权，管理员也不能在这条普通共享路径中绕过限制。
        for role in Role:
            for action in Action:
                with self.subTest(role=role, action=action):
                    self.assertFalse(self.allowed(
                        replace(self.viewer, role=role), action,
                        visibility=Visibility.RESTRICTED,
                    ))

    def test_unknown_action_role_and_visibility_denied(self):
        for value in (None, "view_report", "delete", [], 1):
            with self.subTest(action=value):
                self.assertFalse(self.allowed(self.viewer, value))
        for value in (None, "admin", "platform_admin", [], 1):
            with self.subTest(role=value):
                self.assertFalse(self.allowed(replace(self.viewer, role=value)))
        for value in (None, "company", "public", [], 1):
            with self.subTest(visibility=value):
                self.assertFalse(self.allowed(self.viewer, visibility=value))

    def test_missing_identity_or_scope_denied(self):
        for value in (None, "", "   ", 1):
            with self.subTest(value=value):
                self.assertFalse(self.allowed(replace(self.viewer, user_id=value)))
                self.assertFalse(self.allowed(replace(self.viewer, workspace_id=value)))
                self.assertFalse(self.allowed(self.viewer, company=value))

    def test_truthy_active_values_are_not_authorization(self):
        for value in (1, "true", "false", [True], None):
            with self.subTest(value=value):
                self.assertFalse(self.allowed(replace(self.viewer, active=value)))

    def test_dict_cannot_be_used_as_verified_membership(self):
        self.assertFalse(self.allowed({
            "user_id": "user-a", "workspace_id": "company-a",
            "role": "admin", "active": True,
        }))

    def test_calls_do_not_retain_previous_company_or_role(self):
        # 纯函数不能把上次管理员的放行结果带到下次只读或其他公司的调用中。
        admin = replace(self.viewer, role=Role.ADMIN)
        self.assertTrue(self.allowed(admin, Action.REQUEST_ANALYSIS))
        self.assertFalse(self.allowed(self.viewer, Action.REQUEST_ANALYSIS))
        self.assertFalse(self.allowed(admin, company="company-b"))
        self.assertEqual(self.viewer.role, Role.VIEWER)


if __name__ == "__main__":
    unittest.main()
