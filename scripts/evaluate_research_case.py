"""真实本地HTTP验收单例；显式--execute才创建可能收费的研究任务。

不直接调用供应商。公司必须是运行器的虚构公司，资料来自已获准导入目录。
报告/冻结输入保留在忽略目录；终端只输出编号、状态、费用和机器指标。
同case-key重跑复用原命令，不能用刷新作为新收费授权。
"""
import argparse
from contextlib import closing
from http.cookiejar import CookieJar
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler, HTTPRedirectHandler

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from services.research.evaluation import compare_reports, evaluate_report
from services.research.store import ResearchStore


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="在已运行工作台中执行获准真实模型验收")
    parser.add_argument("--root", type=Path, default=Path(".bidradar-data/r12-workbench"))
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--case-key", required=True)
    parser.add_argument("--company", type=int, choices=(0, 1), default=0)
    parser.add_argument("--parent-run-id")
    parser.add_argument("--question")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    import re
    if (not re.fullmatch(r"[a-f0-9]{64}", args.notice_id) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", args.case_key)
            or not 0 <= args.wait_seconds <= 360 or bool(args.parent_run_id) != bool(args.question)):
        parser.error("invalid case arguments")
    root = args.root.resolve()
    runtime = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    accounts = json.loads((root / "demo-accounts.json").read_text(encoding="utf-8"))
    if accounts.get("simulation") is not True or runtime.get("companies") != "fictional_only":
        raise ValueError("fictional_environment_required")
    if not args.execute:
        print(json.dumps({"execute": False, "notice_id": args.notice_id, "company": args.company,
                          "model_calls": 0, "instruction": "Only --execute submits bounded tasks"}))
        return 0
    jar = CookieJar()
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar), NoRedirect())
    origin = runtime["url"]
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", origin):
        raise ValueError("invalid_runtime_url")
    def request(path, value=None):
        headers = {"Accept": "application/json"}
        raw = None
        if value is not None:
            raw = json.dumps(value, ensure_ascii=False).encode()
            headers.update({"Content-Type": "application/json", "Origin": origin})
            csrf = next((c.value for c in jar if c.name == "br_csrf"), None)
            if csrf:
                headers["X-CSRF-Token"] = csrf
        try:
            with opener.open(Request(origin + path, raw, headers), timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            with exc:
                detail = json.load(exc)
            raise RuntimeError(detail.get("error", {}).get("code", "http_error")) from None
    account = accounts["accounts"][0 if args.company == 0 else 3]
    request("/api/login", {"username": account["username"], "password": account["password"]})
    wid = accounts["workspaces"][args.company]["id"]
    prefix = f"/api/workspaces/{wid}/"
    case_key = "eval-" + args.case_key
    runs = []
    if args.parent_run_id:
        run = request(prefix + "research/questions", {"parent_run_id": args.parent_run_id, "question": args.question, "key": case_key})
        runs.append(run)
    else:
        revision = request(prefix + "profile")["current"]["revision"]
        detail = request(prefix + "notices/" + args.notice_id)
        observation = detail["current"]["observation_id"]
        selection = request(prefix + "selections", {"profile_revision": revision, "items": [{"notice_id": args.notice_id,
                              "observation_id": observation}], "key": case_key + "-select"})
        selected = request(prefix + "selections/" + selection["id"])
        if selected["state"] == "awaiting_decision":
            selected = request(prefix + "selections/" + selected["id"] + "/confirm", {"expected_version": selected["version"], "key": case_key + "-confirm"})
        for mode in ("baseline", "agent"):
            runs.append(request(prefix + "research/analyses", {"selection_id": selected["id"], "notice_id": args.notice_id,
                                                               "mode": mode, "key": case_key + "-" + mode}))
    output_dir = root / "evaluations"
    output_dir.mkdir(exist_ok=True)
    output = output_dir / (args.case_key + ".json")
    deadline = time.monotonic() + args.wait_seconds
    while True:
        runs = [request(prefix + "research/runs/" + run["id"]) for run in runs]
        record = {"case_key": args.case_key, "workspace_id": wid, "sample_source": "catalog_frozen",
                  "recorded_at": time.time(), "runs": runs, "comparison": None}
        # 此CLI是research所属验收工具，只读本服务自身任务库来核验冻结输入；不跨服务读库。
        with closing(ResearchStore(root / "research")) as store:
            manifests = [store.load(wid, run["id"])["manifest"] for run in runs]
        record["manifests"] = manifests
        if len(runs) == 2 and all(run["report"] is not None for run in runs):
            record["comparison"] = compare_reports(manifests[1], runs[0]["report"], runs[1]["report"])
        elif len(runs) == 1 and runs[0]["report"]:
            record["comparison"] = evaluate_report(manifests[0], runs[0]["report"])
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if (all(run["state"] not in ("queued", "running", "retry_wait") for run in runs)
                or time.monotonic() >= deadline):
            break
        time.sleep(2)
    print(json.dumps({"file": str(output), "runs": [{k: run[k] for k in ("id", "state", "mode", "reason")} for run in runs],
                      "budget": request(prefix + "research/budget")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
