"""数据获取阶段统一CLI；运行日志是JSON行，原文只写入本地专属归档目录。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from services.ingestion.archive import Store, StoreError
from services.ingestion.pipeline import execute, replay, request_spec, public_request_spec
from services.ingestion.transport import FetchError, SourcePolicy, Transport


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BidRadar首源获取：先登记运行，再归档/解析；默认不联网。")
    parser.add_argument("--store", type=Path, default=Path(".bidradar-data"), help="ingestion专属本地目录")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="按已批准来源规则获取；无有效policy即拒绝")
    source = collect.add_mutually_exclusive_group(required=True)
    source.add_argument("--keyword")
    source.add_argument("--notice-url", action="append", default=[])
    collect.add_argument("--key", required=True, help="幂等键：同请求重复运行返回原run_id")
    collect.add_argument("--pages", type=int, default=1)
    collect.add_argument("--max-notices", type=int, default=10)
    collect.add_argument("--max-attachments", type=int, default=5)
    collect.add_argument("--attachments", action="store_true")
    collect.add_argument("--start-date")
    collect.add_argument("--end-date")
    collect.add_argument("--policy", type=Path)
    public = commands.add_parser("collect-public", help="CCGP/海南公开栏目有限获取；显式联网与有效policy均必需")
    public.add_argument("--source", choices=("ccgp", "hainan"), required=True)
    public_entry = public.add_mutually_exclusive_group(required=True)
    public_entry.add_argument("--category", help="CCGP如zygg/jzxcs；海南如cggg")
    public_entry.add_argument("--notice-url", action="append", default=[])
    public.add_argument("--key", required=True)
    public.add_argument("--start-page", type=int, default=1)
    public.add_argument("--pages", type=int, default=1)
    public.add_argument("--max-notices", type=int, default=10)
    public.add_argument("--max-attachments", type=int, default=5)
    public.add_argument("--attachments", action="store_true")
    public.add_argument("--title-term", action="append", default=[], help="有限栏目内的标题包含词，多个按或匹配；不等于全网搜索")
    public.add_argument("--allow-network", action="store_true")
    public.add_argument("--policy", type=Path)
    public.add_argument("--dns-mode", choices=("system", "google-doh"), default="system")
    tj = commands.add_parser("collect-tianjin", help="天津官方免费接口有限采样；需本人注册的本地令牌")
    tj.add_argument("--key", required=True)
    tj.add_argument("--resource", choices=("negotiation", "consultation", "correction"), default="negotiation",
                    help="已核对的官方资源；一次只取一个资源，不自动扩抓")
    tj.add_argument("--start-page", type=int, default=1)
    tj.add_argument("--pages", type=int, default=1)
    tj.add_argument("--page-size", type=int, default=10)
    tj.add_argument("--allow-network", action="store_true")
    tj.add_argument("--token-file", type=Path)
    tj.add_argument("--dns-mode", choices=("system", "google-doh"), default="system",
                    help="显式选择公网DNS解析；默认系统DNS，不自动降级")
    resume = commands.add_parser("resume", help="仅恢复中断的同一运行；终态不自动重试")
    resume.add_argument("run_id")
    resume.add_argument("--policy", type=Path)
    resume.add_argument("--allow-network", action="store_true")
    resume.add_argument("--token-file", type=Path)
    resume.add_argument("--dns-mode", choices=("system", "google-doh"),
                        help="仅可与原天津运行一致；默认沿用已保存模式")
    for name in ("show", "cancel"):
        commands.add_parser(name).add_argument("run_id")
    commands.add_parser("replay", help="校验原件后离线重解析，不覆盖历史").add_argument("capture_id")
    commands.add_parser("export", help="校验原件并导出带版本的处理证据JSON").add_argument("run_id")
    commands.add_parser("verify", help="核对账本引用的全部本地原件及SHA256")
    demo = commands.add_parser("demo", help="虚构样本离线端到端演示，绝不联网")
    demo.add_argument("--key", default="offline-demo-v1")
    args = parser.parse_args(argv)
    store = None
    try:
        # 必须先校验请求；参数错误不会留下半个运行或隐式开始联网。
        request = request_spec(keyword=args.keyword, notice_urls=args.notice_url, pages=args.pages,
                               max_notices=args.max_notices, max_attachments=args.max_attachments,
                               attachments=args.attachments, start_date=args.start_date, end_date=args.end_date
                               ) if args.command == "collect" else None
        if args.command == "collect-public":
            request = public_request_spec(source_id="cn_ccgp" if args.source == "ccgp" else "cn_hainan",
                category=args.category, notice_urls=args.notice_url, start_page=args.start_page, pages=args.pages,
                max_notices=args.max_notices, max_attachments=args.max_attachments, attachments=args.attachments,
                title_terms=args.title_term, dns_mode=args.dns_mode)
        store = Store(args.store)
        if args.command == "collect-tianjin":
            from services.ingestion import tianjin
            request = tianjin.request_spec(start_page=args.start_page, pages=args.pages,
                                          page_size=args.page_size, dns_mode=args.dns_mode,
                                          source_id=tianjin.RESOURCE_KEYS[args.resource])
            run_id, created = store.create_run(request, args.key)
            emit({"event": "run_registered", "run_id": run_id, "created": created, "simulation": False})
            transport = tianjin.TianjinTransport(allow_network=args.allow_network,
                                               token_file=args.token_file or tianjin.DEFAULT_TOKEN_FILE,
                                               dns_mode=args.dns_mode, source_id=request["source_id"])
            report = tianjin.execute(store, run_id, transport)
        elif args.command in ("collect", "collect-public", "demo"):
            if args.command == "demo":
                from services.ingestion.demo import DemoTransport, demo_request
                request = demo_request()
                # 演示与真实请求使用不同幂等内容，不能把演示成绩当真实采集。
                request["simulation"] = True
                transport = DemoTransport()
            else:
                transport = Transport(SourcePolicy.from_file(args.policy) if args.policy else None,
                    dns_mode=args.dns_mode if args.command == "collect-public" else "system",
                    allow_network=args.allow_network if args.command == "collect-public" else True)
            run_id, created = store.create_run(request, args.key)
            emit({"event": "run_registered", "run_id": run_id, "created": created,
                  "simulation": args.command == "demo"})
            report = execute(store, run_id, transport)
        elif args.command == "resume":
            from services.ingestion import tianjin
            if store.run(args.run_id)["request"].get("source_id") in tianjin.SOURCE_RESOURCES:
                dns_mode = store.run(args.run_id)["request"].get("dns_mode", "system")
                if args.dns_mode is not None and args.dns_mode != dns_mode:
                    raise ValueError("dns_mode_must_match_run")
                transport = tianjin.TianjinTransport(allow_network=args.allow_network,
                                                   token_file=args.token_file or tianjin.DEFAULT_TOKEN_FILE,
                                                   dns_mode=dns_mode,
                                                   source_id=store.run(args.run_id)["request"]["source_id"])
                report = tianjin.execute(store, args.run_id, transport)
            elif store.run(args.run_id)["request"].get("simulation"):
                from services.ingestion.demo import DemoTransport
                transport = DemoTransport()
                report = execute(store, args.run_id, transport)
            else:
                request = store.run(args.run_id)["request"]
                dns_mode = request.get("dns_mode", "system")
                if args.dns_mode is not None and args.dns_mode != dns_mode:
                    raise ValueError("dns_mode_must_match_run")
                transport = Transport(SourcePolicy.from_file(args.policy) if args.policy else None,
                    dns_mode=dns_mode,
                    allow_network=args.allow_network if request.get("discovery") == "public_category" else True)
                report = execute(store, args.run_id, transport)
        elif args.command == "show":
            report = store.report(args.run_id)
        elif args.command == "cancel":
            store.cancel(args.run_id)
            report = store.report(args.run_id)
        elif args.command == "replay":
            emit({"event": "replay", **replay(store, args.capture_id)})
            return 0
        elif args.command == "export":
            from services.ingestion.export import export_bundle
            emit(export_bundle(store, args.run_id))
            return 0
        else:
            hashes = [row[0] for row in store.db.execute("SELECT DISTINCT sha256 FROM captures WHERE sha256 IS NOT NULL")]
            for digest in hashes:
                store.read_blob(digest)
            emit({"event": "archive_verified", "blob_count": len(hashes)})
            return 0
        emit({"event": "run_report", **report})
        return {"succeeded": 0, "partial": 2, "blocked": 3, "failed": 4,
                "cancelled": 5, "queued": 6, "running": 6}[report["run"]["status"]]
    except KeyboardInterrupt:
        emit({"event": "interrupted", "message": "运行已释放，可按同一run_id恢复"})
        return 130
    except sqlite3.DatabaseError:
        # 数据库损坏/锁冲突不是零结果；给稳定错误码，原目录留给检查与恢复。
        emit({"event": "error", "code": "storage_database_error"})
        return 4
    except (StoreError, FetchError, ValueError, OSError) as exc:
        emit({"event": "error", "code": getattr(exc, "code", str(exc))})
        return 4
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
