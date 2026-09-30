"""R1/R2 本地四服务运行器；只预置虚构企业，真实模型须显式开启。

所有进程仍通过HTTP按所属领域访问数据。实际模型预算固定在仓库外本轮授权账本，
更换--root仅换业务环境，不重置预算；命令行、日志和runtime.json不含密钥值。
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from scripts.run_workbench import prepare, _write
from services.common.owner_clients import WorkspaceAccessClient, CatalogEvidenceClient, ResearchClient, TrackingClient
from services.research.budget import BudgetLedger

BUDGET_PATH = Path.home() / ".bidradar/budgets/deepseek-r12-20261001.sqlite3"
KEY_PATH = Path.home() / ".bidradar/credentials/deepseek-api-key.txt"


def seed_profiles(root):
    """仅首次预置虚构声明；已有人修改的正式版本绝不覆盖。声明仍不等于证明。"""
    from services.workspace.store import WorkspaceStore
    account_data = json.loads((root / "demo-accounts.json").read_text(encoding="utf-8"))
    examples = [(0, 0, {"company_name": "山岚软件（虚构）", "city": "天津",
                       "project_types": "软件开发、知识库、智能体、系统集成",
                       "capabilities": "4人软件团队，Python、API集成、RAG和Agent应用开发",
                       "delivery_constraints": "可短期出差10天以内，不接受连续60天驻场",
                       "cases": "声明做过企业知识库试点，未提交证明", "qualifications": None,
                       "staffing": "4名软件工程师，无驻场替补团队", "commercial_constraints": "需核对付款进度"}),
                (3, 1, {"company_name": "海隅智能（虚构）", "city": "海口",
                       "project_types": "硬件运维与现场技术服务", "capabilities": "设备安装与现场运维，缺少软件研发团队",
                       "delivery_constraints": "可连续60天驻场", "cases": None, "qualifications": None,
                       "staffing": "8名现场维护人员", "commercial_constraints": None})]
    with closing(WorkspaceStore(root / "workspace")) as store:
        for account_index, company_index, payload in examples:
            actor = account_data["accounts"][account_index]["user_id"]
            wid = account_data["workspaces"][company_index]["id"]
            if store.profile(actor, wid)["current"] is None:
                proposal = store.propose_profile(actor, wid, payload, 0, "r12-fictional-profile")
                store.confirm_profile(actor, wid, proposal["id"], 0, "r12-fictional-confirm")


def serve(name, config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    root, ports, tokens = Path(config["root"]), config["ports"], config["tokens"]
    url = lambda service: f"http://127.0.0.1:{ports[service]}"
    stop, worker = threading.Event(), None
    if name == "catalog":
        from services.catalog.http_api import create_server
        server = create_server(root / "catalog", token=tokens["catalog"], port=ports[name])
    elif name == "workspace":
        from services.workspace.http_api import create_server
        server = create_server(root / "workspace", url("catalog"), tokens["catalog"], port=ports[name],
                               internal_tokens={"research": tokens["research_workspace"], "tracking": tokens["tracking_workspace"]},
                               research_client=ResearchClient(url("research"), tokens["workspace_research"]),
                               tracking_client=TrackingClient(url("tracking"), tokens["workspace_tracking"]))
    elif name == "research":
        from services.research.http_api import create_server
        from services.research.provider import DeepSeekProvider
        from services.research.store import ResearchStore
        from services.research.worker import run_worker
        with closing(ResearchStore(root / "research")):
            pass
        provider = None
        if config["enable_model"]:
            with KEY_PATH.open("r", encoding="utf-8") as stream:
                secret = stream.read(257).strip()
            if len(secret) > 256 or not secret:
                raise ValueError("invalid_model_credential")
            provider = DeepSeekProvider(secret)
        access = WorkspaceAccessClient(url("workspace"), tokens["research_workspace"])
        catalog = CatalogEvidenceClient(url("catalog"), tokens["catalog"])
        tracking = TrackingClient(url("tracking"), tokens["research_tracking"])
        server = create_server(root / "research", access, catalog, tracking,
                               tokens={"workspace": tokens["workspace_research"], "tracking": tokens["tracking_research"]},
                               budget_path=BUDGET_PATH, port=ports[name])
        worker = threading.Thread(target=run_worker, args=(root / "research", access, catalog, tracking, provider, BUDGET_PATH),
                                  kwargs={"stop_event": stop}, daemon=True)
    else:
        from services.tracking.http_api import create_server
        from services.tracking.store import TrackingStore
        from services.tracking.worker import run_worker
        with closing(TrackingStore(root / "tracking")):
            pass
        access = WorkspaceAccessClient(url("workspace"), tokens["tracking_workspace"])
        catalog = CatalogEvidenceClient(url("catalog"), tokens["catalog"])
        research = ResearchClient(url("research"), tokens["tracking_research"])
        server = create_server(root / "tracking", access, catalog, research,
                               tokens={"workspace": tokens["workspace_tracking"], "research": tokens["research_tracking"]}, port=ports[name])
        worker = threading.Thread(target=run_worker, args=(root / "tracking", access, catalog, research),
                                  kwargs={"stop_event": stop}, daemon=True)
    if worker:
        worker.start()
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description="R1/R2本地虚构企业工作台，模型20元固定授权账本")
    parser.add_argument("--root", type=Path, default=Path(".bidradar-data/r12-workbench"))
    parser.add_argument("--bundle", type=Path, action="append", default=[])
    parser.add_argument("--enable-model", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--port", type=int, default=8865)
    parser.add_argument("--serve", choices=("workspace", "catalog", "research", "tracking"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.serve:
        if not args.config:
            parser.error("service config required")
        serve(args.serve, args.config)
        return 0
    if not 1024 <= args.port <= 65532 or len(args.bundle) > 10:
        parser.error("port须在1024..65532，最多10个获准包")
    root = args.root.resolve()
    prepare(root, args.bundle)
    seed_profiles(root)
    with closing(BudgetLedger(BUDGET_PATH)) as ledger:
        budget = ledger.summary()
    config_path = root / "service-config.json"
    token_names = ("catalog", "workspace_research", "workspace_tracking", "research_workspace",
                   "research_tracking", "tracking_workspace", "tracking_research")
    existing = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
    tokens = existing["tokens"] if existing else {name: secrets.token_urlsafe(32) for name in token_names}
    config = {"root": str(root), "enable_model": args.enable_model, "tokens": tokens,
              "ports": {name: args.port + i for i, name in enumerate(("workspace", "catalog", "research", "tracking"))}}
    _write(config_path, config)
    if args.prepare_only:
        print(json.dumps({"prepared": True, "budget": budget, "accounts_file": str(root / "demo-accounts.json")}, ensure_ascii=False))
        return 0
    children, logs = [], []
    try:
        for name in ("catalog", "workspace", "research", "tracking"):
            log = (root / (name + ".log")).open("ab")
            logs.append(log)
            command = [sys.executable, str(Path(__file__).resolve()), "--serve", name, "--config", str(config_path)]
            children.append(subprocess.Popen(command, cwd=REPO, stdout=log, stderr=log,
                                             env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        # 活进程还不等于端到端就绪；调用每个服务的真实身份及所属公司接口。
        account_data = json.loads((root / "demo-accounts.json").read_text(encoding="utf-8"))
        actor, wid = account_data["accounts"][0]["user_id"], account_data["workspaces"][0]["id"]
        base = {"schema_version": 1, "actor_id": actor, "workspace_id": wid}
        deadline = time.monotonic() + 20
        while True:
            if any(p.poll() is not None for p in children):
                raise RuntimeError("local_service_start_failed")
            try:
                WorkspaceAccessClient(f"http://127.0.0.1:{args.port}", tokens["research_workspace"], timeout=1).authorize(actor, wid, "read")
                CatalogEvidenceClient(f"http://127.0.0.1:{args.port+1}", tokens["catalog"], timeout=1).changes()
                ResearchClient(f"http://127.0.0.1:{args.port+2}", tokens["workspace_research"], timeout=1).request(
                    "POST", "/internal/v1/runs/list", {**base, "before": None, "limit": 1})
                TrackingClient(f"http://127.0.0.1:{args.port+3}", tokens["workspace_tracking"], timeout=1).request(
                    "POST", "/internal/v1/notifications", {**base, "after": 0, "limit": 1})
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("local_service_start_timeout") from None
                time.sleep(.15)
        runtime = {"url": f"http://127.0.0.1:{args.port}", "pids": [p.pid for p in children],
                   "mode": "local_development", "model_enabled": args.enable_model, "budget_file": str(BUDGET_PATH),
                   "companies": "fictional_only"}
        _write(root / "runtime.json", runtime)
        print(json.dumps(runtime, ensure_ascii=False), flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(.5)
        raise RuntimeError("local_service_stopped")
    except KeyboardInterrupt:
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
