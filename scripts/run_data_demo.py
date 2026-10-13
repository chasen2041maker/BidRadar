"""离线演示整条数据链：采集（虚构数据）→ 导出证据包 → 规范化 → 导入目录 → 查询。

用法：python scripts/run_data_demo.py
全程使用虚构数据，不访问任何网站。只通过各服务的命令行工具串起来，不直接读写任何服务的数据库。
输出文件放在 .bidradar-data/demo-chain/，终端只显示摘要。重复运行可以验证幂等（不会重复导入）。
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    """依次运行各个命令行工具，并打印摘要。"""
    parser = argparse.ArgumentParser(description="数据阶段离线整链演示（非真实采购数据）")
    parser.add_argument("--root", type=Path, default=Path(".bidradar-data/demo-chain"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    def invoke(module, arguments, *, data=None, log):
        process = subprocess.run(
            [sys.executable, "-m", module, *map(str, arguments)],
            input=data,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=workspace,
            env=env,
            timeout=30,
        )
        (root / log).write_text(process.stdout, encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"{module}:exit_{process.returncode}")
        return process.stdout, json.loads(process.stdout.splitlines()[-1])

    _, run = invoke(
        "services.ingestion", ["--store", root / "ingestion", "demo", "--key", "chain-v2"], log="ingestion.jsonl"
    )
    evidence, _ = invoke(
        "services.ingestion", ["--store", root / "ingestion", "export", run["run"]["id"]], log="evidence.json"
    )
    normalized, _ = invoke("services.processing", [], data=evidence, log="normalized.json")
    _, imported = invoke(
        "services.catalog", ["--store", root / "catalog", "import"], data=normalized, log="import.json"
    )
    _, repeated = invoke(
        "services.catalog", ["--store", root / "catalog", "import"], data=normalized, log="repeat-import.json"
    )
    _, query = invoke(
        "services.catalog",
        ["--store", root / "catalog", "query", "--simulation", "--keyword", "软件", "--as-of", "2026-10-01T00:00:00Z"],
        log="query.json",
    )
    if query["total"] != 1 or repeated["added"] != 0:
        raise RuntimeError("demo_acceptance_failed")
    _, detail = invoke(
        "services.catalog",
        ["--store", root / "catalog", "detail", query["items"][0]["notice_id"], "--as-of", "2026-10-01T00:00:00Z"],
        log="detail.json",
    )
    print(
        json.dumps(
            {
                "simulation": True,
                "run_id": run["run"]["id"],
                "catalog_count": query["total"],
                "first_import_added": imported["added"],
                "repeat_import_added": repeated["added"],
                "budget_cny": detail["current"]["facts"]["budget"]["value"]["amount"],
                "history_count": len(detail["history"]),
                "evidence_directory": str(root),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
