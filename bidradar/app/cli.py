"""命令行：启动进程、初始化演示数据。

    uv run python -m bidradar api            启动 API（等同于 uvicorn bidradar.app.main:app）
    uv run python -m bidradar worker         启动 Worker（阶段 3 起有后台任务）
    uv run python -m bidradar seed-demo      建两家虚构公司、四个测试账号和初始档案

seed-demo 只建一次：账号已经存在就跳过，绝不重置已有账号的密码。
随机密码写进 <data_dir>/demo-accounts.json（默认 .bidradar-data/v2/，不进 git）。
"""

import argparse
import json
import os
import secrets
from pathlib import Path

from bidradar.app.settings import Settings

# 和旧版工作台一样的两家虚构公司、四个账号，方便对照新旧两套代码。
DEMO_ACCOUNTS = [
    ("demo.admin", "山岚管理员（虚构）"),
    ("demo.member", "山岚协作成员（虚构）"),
    ("demo.viewer", "山岚只读成员（虚构）"),
    ("other.admin", "海隅管理员（虚构）"),
]

DEMO_PROFILES = {
    "山岚软件（虚构）": {
        "company_name": "山岚软件（虚构）",
        "city": "天津",
        "project_types": "软件开发、知识库、智能体、系统集成",
        "capabilities": "4人软件团队，Python、API集成、RAG和Agent应用开发",
        "delivery_constraints": "可短期出差10天以内，不接受连续60天驻场",
        "cases": "声明做过企业知识库试点，未提交证明",
        "qualifications": None,
        "staffing": "4名软件工程师，无驻场替补团队",
        "commercial_constraints": "需核对付款进度",
    },
    "海隅智能（虚构）": {
        "company_name": "海隅智能（虚构）",
        "city": "海口",
        "project_types": "硬件运维与现场技术服务",
        "capabilities": "设备安装与现场运维，缺少软件研发团队",
        "delivery_constraints": "可连续60天驻场",
        "cases": None,
        "qualifications": None,
        "staffing": "8名现场维护人员",
        "commercial_constraints": None,
    },
}


def _write_private(path: Path, value: object) -> None:
    """写 JSON 文件；非 Windows 系统上设成只有本人可读写（里面有测试账号的密码）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def seed_demo(settings: Settings) -> dict:
    """建演示数据，返回账号文件的内容。重复执行是安全的：已存在的账号和公司不会重建。"""
    from bidradar.infra.db import make_engine, make_session_factory
    from bidradar.modules.identity import service as identity
    from bidradar.modules.workspace import service as workspace

    accounts_path = settings.data_dir / "demo-accounts.json"
    if accounts_path.exists():
        return json.loads(accounts_path.read_text(encoding="utf-8"))

    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            if any(identity.find_user(session, name) for name, _ in DEMO_ACCOUNTS):
                raise SystemExit("演示账号已经存在，但找不到 demo-accounts.json。为避免重置密码，已停止。")
            accounts = []
            for username, display_name in DEMO_ACCOUNTS:
                password = secrets.token_urlsafe(18)
                user = identity.create_user(session, username, password, display_name)
                accounts.append(
                    {"username": username, "password": password, "display_name": display_name, "user_id": user.id}
                )
            shanlan = workspace.create_workspace(session, "山岚软件（虚构）", accounts[0]["user_id"])
            haiyu = workspace.create_workspace(session, "海隅智能（虚构）", accounts[3]["user_id"])
            workspace.add_member(session, shanlan, accounts[1]["user_id"], "member")
            workspace.add_member(session, shanlan, accounts[2]["user_id"], "viewer")
            for wid, admin, name in (
                (shanlan, accounts[0], "山岚软件（虚构）"),
                (haiyu, accounts[3], "海隅智能（虚构）"),
            ):
                proposal = workspace.propose_profile(session, admin["user_id"], wid, DEMO_PROFILES[name], 0, "seed")
                workspace.confirm_profile(session, admin["user_id"], wid, proposal.id, 0, "seed-confirm")
            result = {
                "simulation": True,
                "accounts": accounts,
                "workspaces": [{"id": shanlan, "name": "山岚软件（虚构）"}, {"id": haiyu, "name": "海隅智能（虚构）"}],
            }
    finally:
        engine.dispose()
    _write_private(accounts_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bidradar", description="BidRadar v2 命令行")
    commands = parser.add_subparsers(dest="command", required=True)

    api = commands.add_parser("api", help="启动 API 进程")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true", help="改了代码自动重启（开发用）")

    commands.add_parser("worker", help="启动 Worker 进程")
    commands.add_parser("seed-demo", help="建两家虚构公司、四个测试账号和初始档案")

    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "api":
        import uvicorn

        uvicorn.run("bidradar.app.main:app", host=args.host, port=args.port, reload=args.reload)
    elif args.command == "worker":
        from bidradar.app.worker import run

        run(settings)
    elif args.command == "seed-demo":
        result = seed_demo(settings)
        print(f"演示账号在 {settings.data_dir / 'demo-accounts.json'}：")
        for account in result["accounts"]:
            print(f"  {account['username']:<12} {account['display_name']}")
    return 0
