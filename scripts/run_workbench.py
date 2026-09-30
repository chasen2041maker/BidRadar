"""启动 R1-A 两个独立本地服务；预置虚构公司，不下载数据、不调用模型。

第一次运行生成测试账号与随机密码，仅保存到忽略目录。公开规范包通过 catalog CLI
导入；这个启动器不会把 catalog ORM 交给 workspace。Ctrl+C 关闭本次子进程。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from services.workspace.store import WorkspaceStore
from services.workspace.catalog_client import CatalogClient, CatalogError


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def prepare(root, bundles):
    """只初始化带本任务标记的独立目录；已有密码和用户历史绝不覆盖。"""
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "demo-accounts.json"
    if not marker.exists():
        if (root / "workspace").exists():
            raise ValueError("existing_workspace_without_demo_marker")
        store = WorkspaceStore(root / "workspace")
        try:
            fixtures = [("demo.admin", "山岚管理员（虚构）"), ("demo.member", "山岚协作成员（虚构）"),
                        ("demo.viewer", "山岚只读成员（虚构）"), ("other.admin", "海隅管理员（虚构）")]
            accounts = []
            for username, name in fixtures:
                password = secrets.token_urlsafe(18)
                uid = store.bootstrap_user(username, password, name)
                accounts.append({"username": username, "password": password, "display_name": name, "user_id": uid})
            a = store.bootstrap_workspace("山岚软件（虚构）", accounts[0]["user_id"])
            b = store.bootstrap_workspace("海隅智能（虚构）", accounts[3]["user_id"])
            store.bootstrap_member(a, accounts[1]["user_id"], "member")
            store.bootstrap_member(a, accounts[2]["user_id"], "viewer")
            _write(marker, {"simulation": True, "accounts": accounts,
                            "workspaces": [{"id": a, "name": "山岚软件（虚构）"}, {"id": b, "name": "海隅智能（虚构）"}]})
        finally:
            store.close()
    else:
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if existing.get("simulation") is not True or not (root / "workspace").is_dir():
            raise ValueError("invalid_demo_marker")
    token_path = root / "catalog-token.txt"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        if os.name != "nt":
            token_path.chmod(0o600)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    demo = subprocess.run([sys.executable, "scripts/run_data_demo.py", "--root", str(root / "demo-data")],
                          cwd=REPO, env=env, capture_output=True, timeout=60)
    if demo.returncode:
        raise RuntimeError("demo_catalog_preparation_failed")
    # 演示与真实包的simulation标记来自已验证契约；不把合成数据改名冒充真实公告。
    inputs = [root / "demo-data" / "normalized.json", *bundles]
    results = []
    for index, source in enumerate(inputs):
        result = subprocess.run([sys.executable, "-m", "services.catalog", "--store", str(root / "catalog"),
                                 "import", str(source.resolve())], cwd=REPO, env=env, capture_output=True, timeout=30)
        (root / f"import-{index}.json").write_bytes(result.stdout)
        if result.returncode:
            raise RuntimeError("catalog_import_failed")
        results.append(json.loads(result.stdout))
    return {"mode": "local_development", "accounts_file": str(marker), "root": str(root),
            "imported": sum(r["added"] for r in results), "model_calls": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description="中文本地工作台；仅虚构企业，公开资料须事先获准")
    parser.add_argument("--root", type=Path, default=Path(".bidradar-data/r1-workbench"))
    parser.add_argument("--bundle", type=Path, action="append", default=[], help="已获准的规范JSON包，可重复传入")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--catalog-port", type=int, default=8766)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if len(args.bundle) > 10 or args.port == args.catalog_port or not all(1024 <= p <= 65535 for p in (args.port, args.catalog_port)):
        parser.error("最多10个包，两个不同的本地端口须在1024至65535之间")
    root = args.root.resolve()
    result = prepare(root, args.bundle)
    result["url"] = f"http://127.0.0.1:{args.port}"
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.prepare_only:
        return 0
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    token = root / "catalog-token.txt"
    commands = [[sys.executable, "-m", "services.catalog.http_api", "--store", str(root / "catalog"),
                 "--port", str(args.catalog_port), "--token-file", str(token)],
                [sys.executable, "-m", "services.workspace.http_api", "--store", str(root / "workspace"),
                 "--port", str(args.port), "--catalog-url", f"http://127.0.0.1:{args.catalog_port}",
                 "--catalog-token-file", str(token)]]
    children, logs = [], []
    try:
        for name, command in zip(("catalog", "workspace"), commands):
            log = (root / (name + ".log")).open("ab")
            logs.append(log)
            children.append(subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=log, creationflags=flags))
        opener = build_opener(ProxyHandler({}))
        client = CatalogClient(f"http://127.0.0.1:{args.catalog_port}", token.read_text(encoding="utf-8").strip(), timeout=1)
        deadline = time.monotonic() + 15
        ready = False
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in children):
                raise RuntimeError("local_service_start_failed")
            try:
                with opener.open(result["url"] + "/health", timeout=1) as response:
                    ready = json.load(response).get("status") == "ok"
                if ready:
                    client.query(page_size=1)  # 核实本次令牌可访问目录，不能只凭网页端口宣布联调就绪。
                    if any(p.poll() is not None for p in children):
                        raise RuntimeError("local_service_start_failed")
                    break
            except (OSError, CatalogError):
                ready = False
                time.sleep(.15)
        if not ready:
            raise RuntimeError("local_service_start_timeout")
        _write(root / "runtime.json", {"url": result["url"], "pids": [p.pid for p in children], "model_calls": 0})
        print("本地工作台已就绪，使用账号文件中的虚构账号登录；Ctrl+C停止本次两个服务。", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(.5)
        raise RuntimeError("local_service_stopped")
    except KeyboardInterrupt:
        return 0
    finally:
        # 只停止本启动器创建的进程，不按端口杀进程，不影响其他工作树的服务。
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
