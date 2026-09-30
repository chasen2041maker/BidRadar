"""本机两个 HTTP 服务的真实契约集成；公司与采购材料均为虚构输入。

关注浏览器新引入的边界：Cookie/Origin/CSRF、服务间迟到响应后的撤权、固定版本。
不通过读取业务数据库替代HTTP结果；预置账号仅使用可信开发入口。
"""
from contextlib import closing
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch

from services.catalog.http_api import create_server as catalog_server
from services.catalog.store import Catalog
from services.ingestion.archive import Store
from services.ingestion.demo import DemoTransport, demo_request
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import execute
from services.processing.normalize import normalize_bundle
from services.workspace.http_api import create_server
from services.workspace.store import WorkspaceStore

PASSWORD = "Only-Fictional-HTTP-Test-2026!"


def profile(name="虚构软件公司"):
    return {"company_name": name, "city": None, "project_types": "软件开发", "capabilities": "知识库开发",
            "delivery_constraints": "优先远程，不代表拒绝出差", "cases": None, "qualifications": None,
            "staffing": None, "commercial_constraints": None}


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        # HTTP契约测试缩短KDF，存储专属测试独立验证实际生产常数/盐/会话Hash。
        self.kdf = patch("services.workspace.store.PASSWORD_ITERATIONS", 1000)
        self.kdf.start()
        store = WorkspaceStore(self.workspace)
        self.admin = store.bootstrap_user("admin", PASSWORD, "管理员（虚构）")
        self.member = store.bootstrap_user("member", PASSWORD, "成员（虚构）")
        self.viewer = store.bootstrap_user("viewer", PASSWORD, "只读（虚构）")
        self.other = store.bootstrap_user("other", PASSWORD, "另一公司（虚构）")
        self.a = store.bootstrap_workspace("A公司（虚构）", self.admin)
        self.b = store.bootstrap_workspace("B公司（虚构）", self.other)
        store.bootstrap_member(self.a, self.member, "member")
        store.bootstrap_member(self.a, self.viewer, "viewer")
        proposal = store.propose_profile(self.admin, self.a, profile(), 0, "fixture-proposal")
        store.confirm_profile(self.admin, self.a, proposal["id"], 0, "fixture-confirm")
        store.close()
        ingestion = Store(self.root / "ingestion")
        request = {**demo_request(), "simulation": True, "pages": 1, "attachments": False}
        run, _ = ingestion.create_run(request, "http-integration")
        execute(ingestion, run, DemoTransport())
        self.bundle = normalize_bundle(export_bundle(ingestion, run))
        ingestion.close()
        catalog = Catalog(self.root / "catalog")
        catalog.import_bundle(self.bundle)
        self.item = catalog.query(simulation=True)["items"][0]
        catalog.close()
        self.token = secrets.token_urlsafe(32)
        self.catalog = catalog_server(self.root / "catalog", token=self.token)
        self.web = create_server(self.workspace, f"http://127.0.0.1:{self.catalog.server_port}", self.token)
        self.threads = []
        for server in (self.catalog, self.web):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.origin = f"http://127.0.0.1:{self.web.server_port}"

    def tearDown(self):
        for server in (self.web, self.catalog):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(2)
        self.kdf.stop()
        self.temp.cleanup()

    def request(self, route, *, login=None, data=None, headers=None, method=None, raw=None):
        values = {}
        if login:
            values.update({"Cookie": login["cookie"], "X-CSRF-Token": login["csrf"]})
        body = None
        if data is not None or raw is not None:
            body = raw if raw is not None else json.dumps(data).encode()
            values.update({"Origin": self.origin, "Content-Type": "application/json"})
        values.update(headers or {})
        with closing(HTTPConnection("127.0.0.1", self.web.server_port, timeout=5)) as connection:
            connection.request(method or ("POST" if body is not None else "GET"), route, body, values)
            response = connection.getresponse()
            content = response.read()
            result = json.loads(content) if "application/json" in response.getheader("Content-Type", "") else content
            return response.status, response.getheaders(), result

    def login(self, username="admin"):
        status, headers, result = self.request("/api/login", data={"username": username, "password": PASSWORD})
        self.assertEqual(status, 200, result)
        jar = SimpleCookie()
        for name, value in headers:
            if name.lower() == "set-cookie":
                jar.load(value)
        self.assertTrue(jar["br_session"]["httponly"])
        self.assertEqual(jar["br_session"]["samesite"], "Strict")
        self.assertNotIn("token", result)
        return {"cookie": "; ".join(f"{k}={v.value}" for k, v in jar.items()), "csrf": jar["br_csrf"].value,
                "token": jar["br_session"].value}

    def route(self, action, wid=None):
        return f"/api/workspaces/{wid or self.a}/{action}"

    def create_selection(self, login, key="selection-one"):
        return self.request(self.route("selections"), login=login, data={"profile_revision": 1,
            "items": [{k: self.item[k] for k in ("notice_id", "observation_id")}], "key": key})

    def test_real_http_profile_catalog_selection_replay_and_logout(self):
        login = self.login()
        status, headers, session = self.request("/api/session", login=login)
        self.assertEqual(status, 200)
        self.assertEqual([w["workspace_id"] for w in session["workspaces"]], [self.a])
        self.assertFalse(session["analysis_enabled"])
        status, _, result = self.request(self.route("notices?simulation=true&page_size=8"), login=login)
        self.assertEqual((status, result["total"]), (200, 1))
        self.assertEqual(self.request(self.route("notices/" + self.item["notice_id"]), login=login)[0], 200)
        status, _, created = self.create_selection(login)
        self.assertEqual((status, created["state"], created["profile_revision"]), (200, "awaiting_decision", 1))
        self.assertEqual(self.create_selection(login)[2], created)
        route = self.route("selections/" + created["id"] + "/confirm")
        payload = {"expected_version": 1, "key": "confirm-one"}
        status, _, confirmed = self.request(route, login=login, data=payload)
        self.assertEqual((status, confirmed["state"], len(confirmed["history"])), (200, "selected", 2))
        self.assertEqual(self.request(route, login=login, data=payload)[2], confirmed)
        self.assertEqual(self.request("/api/logout", login=login, data={})[0], 200)
        self.assertEqual(self.request(self.route("profile"), login=login)[0], 401)

    def test_member_proposal_admin_confirmation_and_viewer_readonly(self):
        member, admin, viewer = self.login("member"), self.login(), self.login("viewer")
        payload = {"payload": profile("修改后（虚构）"), "base_revision": 1, "key": "propose-v2"}
        status, _, proposed = self.request(self.route("profile/proposals"), login=member, data=payload)
        self.assertEqual(status, 200)
        confirmation = {"proposal_id": proposed["id"], "expected_revision": 1, "key": "confirm-v2"}
        self.assertEqual(self.request(self.route("profile/confirm"), login=member, data=confirmation)[0], 403)
        self.assertEqual(self.request(self.route("profile/proposals"), login=viewer, data=payload)[0], 403)
        self.assertEqual(self.create_selection(viewer)[0], 403)
        self.assertEqual(self.request(self.route("profile/confirm"), login=admin, data=confirmation)[0], 200)
        current = self.request(self.route("profile"), login=viewer)[2]
        self.assertEqual(len(current["history"]), 2)
        self.assertEqual(current["current"]["proof_status"], "not_provided")
        # 客户端保存的旧档案版本不可在写入时悄悄升级成第二版。
        self.assertEqual(self.create_selection(member)[0], 409)

    def test_cross_company_private_objects_and_revoked_replay_rejected(self):
        member, admin, other = self.login("member"), self.login(), self.login("other")
        _, _, selection = self.create_selection(member)
        self.assertEqual(self.request(self.route("profile"), login=other)[0], 403)
        self.assertEqual(self.request(self.route("selections/" + selection["id"], self.b), login=other)[0], 404)
        status, _, _ = self.request(self.route("members/change"), login=admin,
                                   data={"target_user_id": self.member, "role": "member", "active": False, "expected_version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(self.create_selection(member)[0], 403)  # 同key重放也不能绕过撤权。

    def test_revoke_during_catalog_call_denies_write_and_late_read(self):
        login = self.login("member")
        original = self.web.catalog_client.validate_selection
        def revoke(items):
            result = original(items)
            store = WorkspaceStore(self.workspace)
            store.change_member(self.admin, self.a, self.member, "member", False, next(m["version"] for m in store.members(self.admin, self.a) if m["user_id"] == self.member))
            store.close()
            return result
        with patch.object(self.web.catalog_client, "validate_selection", side_effect=revoke):
            # 私有事务隐藏失去访问权的空间，与不存在对象一致返回404。
            self.assertEqual(self.create_selection(login)[0], 404)
        store = WorkspaceStore(self.workspace)
        self.assertEqual(store.selections(self.admin, self.a), [])
        store.change_member(self.admin, self.a, self.member, "member", True, next(m["version"] for m in store.members(self.admin, self.a) if m["user_id"] == self.member))
        store.close()
        original_detail = self.web.catalog_client.detail
        def revoke_read(identifier):
            result = original_detail(identifier)
            store = WorkspaceStore(self.workspace)
            store.change_member(self.admin, self.a, self.member, "member", False, next(m["version"] for m in store.members(self.admin, self.a) if m["user_id"] == self.member))
            store.close()
            return result
        with patch.object(self.web.catalog_client, "detail", side_effect=revoke_read):
            self.assertEqual(self.request(self.route("notices/" + self.item["notice_id"]), login=login)[0], 403)

    def test_logout_during_catalog_call_is_rechecked_inside_write(self):
        login = self.login()
        original = self.web.catalog_client.validate_selection
        def logout(items):
            result = original(items)
            store = WorkspaceStore(self.workspace)
            store.logout(login["token"])
            store.close()
            return result
        with patch.object(self.web.catalog_client, "validate_selection", side_effect=logout):
            self.assertEqual(self.create_selection(login)[0], 401)

    def test_invalid_fixed_version_empty_and_unsafe_payload_rejected(self):
        login = self.login()
        for items in ([], [{"notice_id": self.item["notice_id"], "observation_id": "0" * 64}],
                      [{"notice_id": self.item["notice_id"], "observation_id": self.item["observation_id"], "title": "伪造"}]):
            status, _, _ = self.request(self.route("selections"), login=login,
                                       data={"profile_revision": 1, "items": items, "key": "invalid"})
            self.assertIn(status, (400, 409))
        self.assertEqual(self.request(self.route("selections"), login=login)[2]["items"], [])

    def test_catalog_failure_is_503_not_success_empty_and_keeps_selection(self):
        login = self.login()
        _, _, saved = self.create_selection(login)
        from services.workspace.catalog_client import CatalogError
        with patch.object(self.web.catalog_client, "query", side_effect=CatalogError("catalog_unavailable", 503)):
            self.assertEqual(self.request(self.route("notices"), login=login)[0], 503)
        self.assertEqual(self.request(self.route("selections/" + saved["id"]), login=login)[2], saved)

    def test_completed_create_replay_survives_later_catalog_failure(self):
        login = self.login()
        _, _, saved = self.create_selection(login)
        from services.workspace.catalog_client import CatalogError
        with patch.object(self.web.catalog_client, "validate_selection", side_effect=CatalogError("catalog_unavailable", 503)) as validate:
            self.assertEqual(self.create_selection(login)[2], saved)
            validate.assert_not_called()
            self.assertEqual(self.create_selection(login, "different-key")[0], 503)

    def test_host_origin_csrf_and_json_framing(self):
        login = self.login()
        self.assertEqual(self.request("/api/session", login=login, headers={"Host": "evil.example"})[0], 400)
        self.assertEqual(self.request("/api/login", data={"username": "admin", "password": PASSWORD}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/logout", login=login, data={}, headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("/api/logout", login=login, data={}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/logout", login=login, raw=b'{"x":1,"x":2}')[0], 400)
        self.assertEqual(self.request("/api/logout", login=login, raw=b'{"x":NaN}')[0], 400)
        self.assertEqual(self.request("/api/session", login=login, method="DELETE")[0], 405)

    def test_headers_static_allowlist_and_no_cors_or_model_endpoint(self):
        status, headers, page = self.request("/")
        headers = dict(headers)
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("商机核查".encode(), page)
        self.assertEqual(self.request("/%2e%2e/AGENTS.md")[0], 400)
        login = self.login()
        self.assertEqual(self.request("/api/research", login=login, data={})[0], 404)
        self.assertEqual(self.request("/health")[2]["model_calls"], 0)

    def test_duplicate_queries_client_metadata_and_last_admin_are_rejected(self):
        login = self.login()
        self.assertEqual(self.request(self.route("notices?keyword=a&keyword=b"), login=login)[0], 400)
        self.assertEqual(self.request(self.route("notices?url=http://evil.example"), login=login)[0], 400)
        self.assertEqual(self.request(self.route("members/change"), login=login,
                         data={"target_user_id": self.admin, "role": "viewer", "active": True, "expected_version": 1})[0], 409)

    def test_stale_member_screen_cannot_restore_revoked_access(self):
        admin = self.login()
        before = self.request(self.route("members"), login=admin)[2]["items"]
        stale = next(m for m in before if m["user_id"] == self.member)
        revoke = {"target_user_id": self.member, "role": stale["role"], "active": False, "expected_version": stale["version"]}
        self.assertEqual(self.request(self.route("members/change"), login=admin, data=revoke)[0], 200)
        stale_save = {"target_user_id": self.member, "role": "viewer", "active": True, "expected_version": stale["version"]}
        self.assertEqual(self.request(self.route("members/change"), login=admin, data=stale_save)[0], 409)
        after = self.request(self.route("members"), login=admin)[2]["items"]
        self.assertFalse(next(m for m in after if m["user_id"] == self.member)["active"])


if __name__ == "__main__":
    unittest.main()
