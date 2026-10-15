"""造测试数据的小函数：建用户、建公司、加成员、登录。

全部走各模块的 service，和真实代码用同一套规则，不直接往表里插数据。
所有名字都是虚构的（AGENTS.md：测试只用虚构数据）。
"""

from hashlib import sha256

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from bidradar.modules.identity import service as identity
from bidradar.modules.identity.models import User
from bidradar.modules.workspace import service as workspace

PASSWORD = "correct-horse-battery"

PROFILE = {
    "company_name": "山岚软件（虚构）",
    "city": "天津",
    "project_types": "软件开发、知识库、智能体",
    "capabilities": "4人软件团队，Python、API集成、RAG和Agent应用开发",
    "delivery_constraints": "可短期出差10天以内，不接受连续60天驻场",
    "cases": None,
    "qualifications": None,
    "staffing": "4名软件工程师",
    "commercial_constraints": None,
}


def make_user(session: Session, username: str, display_name: str | None = None) -> User:
    return identity.create_user(session, username, PASSWORD, display_name or f"{username}（虚构）")


def make_workspace(session: Session, admin: User, name: str = "山岚软件（虚构）") -> str:
    return workspace.create_workspace(session, name, admin.id)


def make_company(session: Session, prefix: str = "a") -> tuple[str, dict[str, User]]:
    """一家有三种角色成员的虚构公司。返回公司 ID 和 {角色: 用户}。"""
    admin = make_user(session, f"{prefix}.admin")
    member = make_user(session, f"{prefix}.member")
    viewer = make_user(session, f"{prefix}.viewer")
    wid = make_workspace(session, admin, name=f"{prefix} 公司（虚构）")
    workspace.add_member(session, wid, member.id, "member")
    workspace.add_member(session, wid, viewer.id, "viewer")
    return wid, {"admin": admin, "member": member, "viewer": viewer}


def confirm_profile(session: Session, wid: str, admin: User, payload: dict | None = None) -> int:
    """给公司确认一版正式档案，返回新的版本号。"""
    current = workspace.authorize(session, admin.id, wid, "admin").current_profile_version
    proposal = workspace.propose_profile(session, admin.id, wid, payload or PROFILE, current, f"p{current}")
    return workspace.confirm_profile(session, admin.id, wid, proposal.id, current, f"c{current}").version


def login(client: TestClient, username: str, password: str = PASSWORD) -> None:
    """用测试客户端登录，并像前端一样把 br_csrf 的值放进之后每个请求的 X-CSRF-Token 头。"""
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["br_csrf"]


# ---- 公告 ----


def raw_notice_bundle(
    *,
    title: str = "虚构软件采购招标公告",
    key: str = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t1.htm",
    observed: str = "2026-09-30T10:00:00Z",
    entries: list[tuple[str, str]] | None = None,
    segments: list[str] | None = None,
) -> dict:
    """一份虚构的采集证据包（采集模块导出的格式），只含一条公告。"""
    if entries is None:
        entries = [
            ("项目编号", "DEMO-001"),
            ("采购单位", "虚构采购方"),
            ("预算（万元）", "12.34"),
            ("公告发布时间", "2026-09-29"),
            ("投标截止时间", "2026-10-10 09:30（北京时间）"),
        ]
    return {
        "schema_version": 1,
        "kind": "raw_evidence_bundle",
        "source_id": "cn_ccgp",
        "simulation": True,
        "run_id": "demo-run",
        "run_status": "succeeded",
        "coverage": "bounded_sample",
        "failures": [],
        "documents": [
            {
                "capture_id": "capture-" + sha256(key.encode()).hexdigest()[:8],
                "source_url": key,
                "final_url": key,
                "source_record_key": key,
                "identity_kind": "source_url",
                "raw_sha256": sha256((title + observed).encode()).hexdigest(),
                "observed_at": observed,
                "parser_version": "fixture-v1",
                "content": {
                    "status": "ok",
                    "title": title,
                    "metadata": [
                        {"label": k, "text": v, "locator": f"fixture[{i}]"} for i, (k, v) in enumerate(entries)
                    ],
                    "segments": [{"text": t, "locator": f"segment[{i}]"} for i, t in enumerate(segments or [])],
                    "links": [],
                },
            }
        ],
    }


def import_notice(session: Session, **kwargs) -> dict:
    """导入一条虚构公告，返回它的规范化观察。"""
    from bidradar.modules.catalog import service as catalog
    from bidradar.modules.ingestion.normalize import normalize_bundle

    bundle = normalize_bundle(raw_notice_bundle(**kwargs))
    catalog.import_observations(session, bundle)
    return bundle["observations"][0]


# ---- 招标文件 ----

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
TENDER_PDF = FIXTURES / "tender-fictional.pdf"
TENDER_DOCX = FIXTURES / "tender-fictional.docx"


def upload_and_parse(session: Session, blobs, user: User, wid: str, path=None) -> str:
    """上传一份虚构招标文件，并像 Worker 一样把它解析完。返回文件 ID。"""
    from bidradar.infra import jobs
    from bidradar.modules.documents import service as documents

    path = path or TENDER_PDF
    document = documents.upload(session, blobs, user.id, wid, filename=path.name, data=path.read_bytes())
    job = jobs.claim(session, [documents.JOB_PARSE], "test-worker")
    if job is not None:
        documents.handle_parse(session, blobs, job)
    return document.id
