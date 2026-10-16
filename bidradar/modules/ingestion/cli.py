"""采集的命令行：python -m bidradar ingest <命令>。从旧版 services/ingestion/__main__.py 搬来，命令和参数不变。

常用命令：
    demo            用虚构数据离线演示整条采集流程（不联网）
    collect         按 CCGP 搜索采集（需要准入文件）
    collect-public  按栏目采集 CCGP 或海南（需要 --allow-network 和准入文件）
    collect-tianjin 采集天津开放接口（需要自己注册的令牌）
    resume          恢复中断的运行
    show / cancel   查看、取消运行
    replay          用当前解析器重新解析已归档的原件
    export          导出证据包 JSON
    publish         导出 + 规范化 + 导入商机目录（v2 新增，替代旧版 processing 和 catalog 两条命令）
    verify          校验所有已归档原件的 SHA-256
每行输出一个 JSON 事件。采集记录在 PostgreSQL 的 ingestion schema 里，原始文件在 <data_dir>/blobs/。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from bidradar.infra.blobs import BlobError, BlobStore
from bidradar.infra.db import make_engine, make_session_factory
from bidradar.infra.errors import AppError
from bidradar.modules.ingestion import service
from bidradar.modules.ingestion.archive import Store, StoreError
from bidradar.modules.ingestion.pipeline import execute, replay, request_spec, public_request_spec
from bidradar.modules.ingestion.transport import FetchError, SourcePolicy, Transport


def emit(value):
    """输出一行 JSON 事件。"""
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main(argv, *, data_dir: Path, database_url: str | None = None, session=None) -> int:
    """解析命令行并执行。退出码：0 成功，2 部分成功，3 被拦截，4 失败，5 已取消，6 未完成。"""
    parser = argparse.ArgumentParser(
        prog="python -m bidradar ingest", description="BidRadar首源获取：先登记运行，再归档/解析；默认不联网。"
    )
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
    public.add_argument(
        "--title-term", action="append", default=[], help="有限栏目内的标题包含词，多个按或匹配；不等于全网搜索"
    )
    public.add_argument("--allow-network", action="store_true")
    public.add_argument("--policy", type=Path)
    public.add_argument("--dns-mode", choices=("system", "google-doh"), default="system")
    tj = commands.add_parser("collect-tianjin", help="天津官方免费接口有限采样；需本人注册的本地令牌")
    tj.add_argument("--key", required=True)
    tj.add_argument(
        "--resource",
        choices=("negotiation", "consultation", "correction"),
        default="negotiation",
        help="已核对的官方资源；一次只取一个资源，不自动扩抓",
    )
    tj.add_argument("--start-page", type=int, default=1)
    tj.add_argument("--pages", type=int, default=1)
    tj.add_argument("--page-size", type=int, default=10)
    tj.add_argument("--allow-network", action="store_true")
    tj.add_argument("--token-file", type=Path)
    tj.add_argument(
        "--dns-mode",
        choices=("system", "google-doh"),
        default="system",
        help="显式选择公网DNS解析；默认系统DNS，不自动降级",
    )
    resume = commands.add_parser("resume", help="仅恢复中断的同一运行；终态不自动重试")
    resume.add_argument("run_id")
    resume.add_argument("--policy", type=Path)
    resume.add_argument("--allow-network", action="store_true")
    resume.add_argument("--token-file", type=Path)
    resume.add_argument("--dns-mode", choices=("system", "google-doh"), help="仅可与原天津运行一致；默认沿用已保存模式")
    for name in ("show", "cancel"):
        commands.add_parser(name).add_argument("run_id")
    commands.add_parser("replay", help="校验原件后离线重解析，不覆盖历史").add_argument("capture_id")
    commands.add_parser("export", help="校验原件并导出带版本的处理证据JSON").add_argument("run_id")
    commands.add_parser("publish", help="导出、规范化并导入商机目录").add_argument("run_id")
    commands.add_parser("verify", help="核对账本引用的全部本地原件及SHA256")
    demo = commands.add_parser("demo", help="虚构样本离线端到端演示，绝不联网")
    demo.add_argument("--key", default="offline-demo-v1")
    args = parser.parse_args(argv)
    # 测试会传入一个跑在回滚事务里的会话；命令行自己连库。
    engine = None
    if session is None:
        engine = make_engine(database_url)
        session = make_session_factory(engine)()
    store = None
    try:
        # 先检查参数。参数有误时直接退出，不会登记半个运行，更不会开始联网。
        request = (
            request_spec(
                keyword=args.keyword,
                notice_urls=args.notice_url,
                pages=args.pages,
                max_notices=args.max_notices,
                max_attachments=args.max_attachments,
                attachments=args.attachments,
                start_date=args.start_date,
                end_date=args.end_date,
            )
            if args.command == "collect"
            else None
        )
        if args.command == "collect-public":
            request = public_request_spec(
                source_id="cn_ccgp" if args.source == "ccgp" else "cn_hainan",
                category=args.category,
                notice_urls=args.notice_url,
                start_page=args.start_page,
                pages=args.pages,
                max_notices=args.max_notices,
                max_attachments=args.max_attachments,
                attachments=args.attachments,
                title_terms=args.title_term,
                dns_mode=args.dns_mode,
            )
        store = Store(session, BlobStore(data_dir / "blobs"))
        if args.command == "collect-tianjin":
            from bidradar.modules.ingestion import tianjin

            request = tianjin.request_spec(
                start_page=args.start_page,
                pages=args.pages,
                page_size=args.page_size,
                dns_mode=args.dns_mode,
                source_id=tianjin.RESOURCE_KEYS[args.resource],
            )
            run_id, created = store.create_run(request, args.key)
            emit({"event": "run_registered", "run_id": run_id, "created": created, "simulation": False})
            transport = tianjin.TianjinTransport(
                allow_network=args.allow_network,
                token_file=args.token_file or tianjin.DEFAULT_TOKEN_FILE,
                dns_mode=args.dns_mode,
                source_id=request["source_id"],
            )
            report = tianjin.execute(store, run_id, transport)
        elif args.command in ("collect", "collect-public", "demo"):
            if args.command == "demo":
                from bidradar.modules.ingestion.demo import DemoTransport, demo_request

                request = demo_request()
                # 演示请求带上 simulation 标记，和真实请求永远不会被当成同一个运行。
                request["simulation"] = True
                transport = DemoTransport()
            else:
                transport = Transport(
                    SourcePolicy.from_file(args.policy) if args.policy else None,
                    dns_mode=args.dns_mode if args.command == "collect-public" else "system",
                    allow_network=args.allow_network if args.command == "collect-public" else True,
                )
            run_id, created = store.create_run(request, args.key)
            emit(
                {"event": "run_registered", "run_id": run_id, "created": created, "simulation": args.command == "demo"}
            )
            report = execute(store, run_id, transport)
        elif args.command == "resume":
            from bidradar.modules.ingestion import tianjin

            if store.run(args.run_id)["request"].get("source_id") in tianjin.SOURCE_RESOURCES:
                dns_mode = store.run(args.run_id)["request"].get("dns_mode", "system")
                if args.dns_mode is not None and args.dns_mode != dns_mode:
                    raise ValueError("dns_mode_must_match_run")
                transport = tianjin.TianjinTransport(
                    allow_network=args.allow_network,
                    token_file=args.token_file or tianjin.DEFAULT_TOKEN_FILE,
                    dns_mode=dns_mode,
                    source_id=store.run(args.run_id)["request"]["source_id"],
                )
                report = tianjin.execute(store, args.run_id, transport)
            elif store.run(args.run_id)["request"].get("simulation"):
                from bidradar.modules.ingestion.demo import DemoTransport

                transport = DemoTransport()
                report = execute(store, args.run_id, transport)
            else:
                request = store.run(args.run_id)["request"]
                dns_mode = request.get("dns_mode", "system")
                if args.dns_mode is not None and args.dns_mode != dns_mode:
                    raise ValueError("dns_mode_must_match_run")
                transport = Transport(
                    SourcePolicy.from_file(args.policy) if args.policy else None,
                    dns_mode=dns_mode,
                    allow_network=args.allow_network if request.get("discovery") == "public_category" else True,
                )
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
            from bidradar.modules.ingestion.export import export_bundle

            emit(export_bundle(store, args.run_id))
            return 0
        elif args.command == "publish":
            emit({"event": "published", **service.publish_run(session, store.blobs, args.run_id)})
            return 0
        else:
            hashes = store.blob_digests()
            for digest in hashes:
                store.read_blob(digest)
            emit({"event": "archive_verified", "blob_count": len(hashes)})
            return 0
        emit({"event": "run_report", **report})
        return {"succeeded": 0, "partial": 2, "blocked": 3, "failed": 4, "cancelled": 5, "queued": 6, "running": 6}[
            report["run"]["status"]
        ]
    except KeyboardInterrupt:
        emit({"event": "interrupted", "message": "运行已释放，可按同一run_id恢复"})
        return 130
    except SQLAlchemyError:
        # 数据库出错不等于"没抓到东西"。返回固定错误码，数据保持原样，方便检查和恢复。
        emit({"event": "error", "code": "storage_database_error"})
        return 4
    except (StoreError, FetchError, BlobError, AppError, ValueError, OSError) as exc:
        emit({"event": "error", "code": getattr(exc, "code", str(exc))})
        return 4
    finally:
        if engine is not None:
            session.close()
            engine.dispose()
