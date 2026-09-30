"""可查询的本地证据目录：导入不调用模型，query只读目录自有存储。"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from services.catalog.store import Catalog


def main(argv=None):
    parser = argparse.ArgumentParser(description="公共证据目录；不承诺当前可投标")
    parser.add_argument("--store", type=Path, default=Path(".bidradar-data/catalog"))
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="导入规范观察JSON文件或stdin")
    importer.add_argument("input", nargs="?", type=Path)
    query = commands.add_parser("query", help="按标题关键词/公告类型查询；默认只看真实记录")
    query.add_argument("--keyword", default="")
    query.add_argument("--kind")
    query.add_argument("--page-size", type=int, default=20)
    query.add_argument("--simulation", action="store_true")
    query.add_argument("--as-of")
    query.add_argument("--cursor")
    detail = commands.add_parser("detail", help="当前观察、历史及更正候选关系")
    detail.add_argument("notice_id")
    detail.add_argument("--as-of")
    args = parser.parse_args(argv)
    store = None
    try:
        store = Catalog(args.store)
        if args.command == "import":
            if args.input:
                with args.input.open("rb") as stream:
                    raw = stream.read(32 * 1024 * 1024 + 1)
            else:
                raw = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError("bundle_too_large")
            result = store.import_bundle(json.loads(raw.decode("utf-8-sig")))
        elif args.command == "query":
            result = store.query(keyword=args.keyword, kind=args.kind, page_size=args.page_size,
                                 simulation=args.simulation, as_of=args.as_of, cursor=args.cursor)
        else:
            result = store.detail(args.notice_id, as_of=args.as_of)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.DatabaseError, RecursionError):
        print(json.dumps({"error": "catalog_request_failed"}))
        return 4
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
