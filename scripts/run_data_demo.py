"""一条命令验收三个CLI的文件契约；全程虚构数据和本地进程，不访问采购网站。

脚本只编排公开CLI，不导入任一服务的Store，不把三个数据域拼成共享业务库。
原始输出写入忽略目录，终端只显示可核对的摘要；可重复运行验证幂等。
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="数据阶段离线整链演示（非真实采购数据）")
    parser.add_argument("--root", type=Path, default=Path(".bidradar-data/demo-chain"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    def invoke(module, arguments, *, data=None, log):
        process = subprocess.run([sys.executable, "-m", module, *map(str, arguments)], input=data,
                                 text=True, encoding="utf-8", capture_output=True, cwd=workspace, env=env, timeout=30)
        (root / log).write_text(process.stdout, encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"{module}:exit_{process.returncode}")
        return process.stdout, json.loads(process.stdout.splitlines()[-1])
    _, run = invoke("services.ingestion", ["--store", root / "ingestion", "demo", "--key", "chain-v2"], log="ingestion.jsonl")
    evidence, _ = invoke("services.ingestion", ["--store", root / "ingestion", "export", run["run"]["id"]], log="evidence.json")
    normalized, _ = invoke("services.processing", [], data=evidence, log="normalized.json")
    _, imported = invoke("services.catalog", ["--store", root / "catalog", "import"], data=normalized, log="import.json")
    _, repeated = invoke("services.catalog", ["--store", root / "catalog", "import"], data=normalized, log="repeat-import.json")
    _, query = invoke("services.catalog", ["--store", root / "catalog", "query", "--simulation", "--keyword", "软件",
                                         "--as-of", "2026-10-01T00:00:00Z"], log="query.json")
    if query["total"] != 1 or repeated["added"] != 0:
        raise RuntimeError("demo_acceptance_failed")
    _, detail = invoke("services.catalog", ["--store", root / "catalog", "detail", query["items"][0]["notice_id"],
                                          "--as-of", "2026-10-01T00:00:00Z"], log="detail.json")
    print(json.dumps({"simulation": True, "run_id": run["run"]["id"], "catalog_count": query["total"],
                      "first_import_added": imported["added"], "repeat_import_added": repeated["added"],
                      "budget_cny": detail["current"]["facts"]["budget"]["value"]["amount"],
                      "history_count": len(detail["history"]), "evidence_directory": str(root)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
