"""只共享 HTTP 契约适配，不共享业务 ORM；对象权限仍由所属服务逐次检查。"""
from urllib.parse import urlencode

from .local_http import LocalClient


class WorkspaceAccessClient(LocalClient):
    def authorize(self, actor_id, workspace_id, action):
        return self.request("POST", "/internal/v1/authorize", {
            "schema_version": 1, "actor_id": actor_id, "workspace_id": workspace_id, "action": action})

    def context(self, actor_id, workspace_id, profile_revision, selection_id=None):
        return self.request("POST", "/internal/v1/context", {
            "schema_version": 1, "actor_id": actor_id, "workspace_id": workspace_id,
            "profile_revision": profile_revision, "selection_id": selection_id})

    def events(self, after=0, limit=100):
        return self.request("POST", "/internal/v1/events", {"schema_version": 1, "after": after, "limit": limit})


class CatalogEvidenceClient(LocalClient):
    def bundle(self, notice_id, snapshot=None):
        query = "" if snapshot is None else "?" + urlencode({"snapshot": snapshot})
        return self.request("GET", "/v1/bundles/" + notice_id + query)

    def observation(self, notice_id, observation_id):
        return self.request("GET", "/v1/observations/" + notice_id + "/" + observation_id)

    def changes(self, after=0, limit=100):
        return self.request("GET", "/v1/changes?" + urlencode({"after": after, "limit": limit}))


class ResearchClient(LocalClient):
    def create_analysis(self, request):
        return self.request("POST", "/internal/v1/analyses", request)

    def run(self, run_id, workspace_id, actor_id):
        return self.request("POST", "/internal/v1/runs/get", {"schema_version": 1,
                            "run_id": run_id, "workspace_id": workspace_id, "actor_id": actor_id})

    def command(self, workspace_id, actor_id, key):
        return self.request("POST", "/internal/v1/commands/get", {"schema_version": 1,
                            "key": key, "workspace_id": workspace_id, "actor_id": actor_id})

    def cancel(self, run_id, workspace_id, actor_id, key):
        return self.request("POST", "/internal/v1/runs/cancel", {"schema_version": 1,
                            "run_id": run_id, "workspace_id": workspace_id, "actor_id": actor_id, "key": key})


class TrackingClient(LocalClient):
    def check_delegation(self, actor_id, workspace_id, delegation):
        return self.request("POST", "/internal/v1/delegations/check", {"schema_version": 1,
                            "actor_id": actor_id, "workspace_id": workspace_id, "delegation": delegation})
