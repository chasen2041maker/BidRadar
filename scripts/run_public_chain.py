"""公开采购第一层的一次完整运行：采集CLI→证据JSON→规范JSON→目录CLI。

原文只留本地目录；终端输出计数/状态/证据位置。脚本不直读任何服务的数据库，
不调模型，不定时循环。相同key只取回同一运行，新的时间窗口需新的key。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from uuid import uuid4


def main(argv=None):
    parser = argparse.ArgumentParser(description="公开采购有限获取到目录；原始日志仅保存在本地")
    parser.add_argument("--root", type=Path, default=Path(".bidradar-data/public-chain"))
    parser.add_argument("--source", choices=("ccgp", "hainan"), required=True)
    entry = parser.add_mutually_exclusive_group(required=True)
    entry.add_argument("--category")
    entry.add_argument("--notice-url", action="append", default=[])
    parser.add_argument("--key", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--dns-mode", choices=("system", "google-doh"), default="system")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--max-notices", type=int, default=10)
    parser.add_argument("--max-attachments", type=int, default=5)
    parser.add_argument("--attachments", action="store_true")
    parser.add_argument("--title-term", action="append", default=[])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    started = datetime.now(timezone.utc).isoformat()
    checks = []

    def invoke(module, arguments, *, data=None, accepted=(0,), timeout=40):
        command = [sys.executable, "-m", module, *map(str, arguments)]
        process = subprocess.run(command, input=data, text=True, encoding="utf-8", capture_output=True,
                                 cwd=workspace, env=env, timeout=timeout)
        checks.append({"module": module, "arguments": list(map(str, arguments)), "exit_code": process.returncode})
        if process.returncode not in accepted:
            raise RuntimeError(module + ":exit_" + str(process.returncode))
        return process.stdout, json.loads(process.stdout.splitlines()[-1]), process.returncode

    base = ["--store", root / "ingestion"]
    collect = [*base, "collect-public", "--source", args.source, "--key", args.key,
        "--policy", args.policy.resolve(), "--dns-mode", args.dns_mode,
        "--start-page", args.start_page, "--pages", args.pages,
        "--max-notices", args.max_notices, "--max-attachments", args.max_attachments]
    if args.category:
        collect += ["--category", args.category]
    for url in args.notice_url:
        collect += ["--notice-url", url]
    for term in args.title_term:
        collect += ["--title-term", term]
    if args.attachments:
        collect += ["--attachments"]
    if args.allow_network:
        collect += ["--allow-network"]
    raw, report, exit_code = invoke("services.ingestion", collect, accepted=(0, 2, 3, 4, 5), timeout=240)
    if "run" not in report:
        raise RuntimeError("collection_not_registered")
    run_id = report["run"]["id"]
    # 同一个幂等运行可多次离线验收；每次日志独立，失败也不会覆盖上次成功清单。
    evidence_dir = root / "runs" / run_id / uuid4().hex
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def save(name, value):
        (evidence_dir / name).write_text(value, encoding="utf-8")

    save("acquisition.jsonl", raw)
    evidence, _, _ = invoke("services.ingestion", [*base, "export", run_id])
    save("evidence.json", evidence)
    normalized, observations, _ = invoke("services.processing", [], data=evidence)
    save("normalized.json", normalized)
    catalog_base = ["--store", root / "catalog"]
    imported, first, _ = invoke("services.catalog", [*catalog_base, "import"], data=normalized)
    save("import.json", imported)
    repeated, second, _ = invoke("services.catalog", [*catalog_base, "import"], data=normalized)
    save("repeat-import.json", repeated)
    if second["added"] != 0:
        raise RuntimeError("catalog_idempotency_failed")
    # 查询和原件校验属于整链验收；部分/受阻运行也要把其真实状态带入目录。
    query, _, _ = invoke("services.catalog", [*catalog_base, "query", "--page-size", 100])
    save("query.json", query)
    verified, _, _ = invoke("services.ingestion", [*base, "verify"])
    save("archive-verification.json", verified)
    for capture in report["captures"]:
        if capture["sha256"]:
            replayed, _, _ = invoke("services.ingestion", [*base, "replay", capture["id"]])
            save("replay-" + capture["id"] + ".json", replayed)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=workspace, capture_output=True,
                               text=True, encoding="utf-8", check=True).stdout)
    registered = json.loads(raw.splitlines()[0])
    manifest = {"mode": "live_transport" if args.allow_network else "network_disabled",
        "network_opt_in": args.allow_network, "reused_existing_run": registered.get("created") is False,
        "simulation": False, "run_id": run_id, "source": args.source, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(), "workspace": str(workspace),
        "head": head, "working_tree_dirty": dirty, "python": platform.python_version(),
        "platform": platform.platform(), "run_status": report["run"]["status"], "exit_code": exit_code,
        "captures": len(report["captures"]), "added": first["added"], "repeat_added": second["added"],
        "checks": checks, "evidence_directory": str(evidence_dir),
        "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in evidence_dir.glob("*.json*")
                  if p.name != "manifest.json"}}
    save("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({k: manifest[k] for k in ("run_id", "source", "run_status", "exit_code", "captures",
                                               "added", "repeat_added", "evidence_directory")}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error)}, ensure_ascii=False))
        raise SystemExit(4)
