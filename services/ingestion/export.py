"""原件拥有者输出版本化证据包；其他服务只收JSON，不能打开本服务数据库。

导出时验证原字节并重解析，固定解析版本；不覆盖获取当时的结果。解析失败仍有
失败记录，空包不能被解释为“没有商机”。未完成运行拒绝导出，避免半页目录。
"""
from services.ingestion.archive import StoreError
from services.ingestion.pipeline import PARSER_VERSION, parse_capture
from services.ingestion import tianjin


def export_bundle(store, run_id: str) -> dict:
    report = store.report(run_id)
    run = report["run"]
    if run["status"] in ("queued", "running"):
        raise StoreError("run_not_terminal")
    documents, failures = [], []
    source = run["request"].get("source_id", "cn_ccgp")
    acquired = {item["url"]: item for item in report["captures"] if item["kind"] == "attachment"}
    for capture in report["captures"]:
        if capture["error_code"]:
            failures.append({"capture_id": capture["id"], "url": capture["url"], "error": capture["error_code"]})
        if not capture["sha256"]:
            continue
        body = store.read_blob(capture["sha256"])
        common = {"capture_id": capture["id"], "source_url": capture["url"],
                  "final_url": capture["final_url"], "observed_at": capture["fetched_at"],
                  "raw_sha256": capture["sha256"]}
        if capture["kind"] == "api_page" and source == tianjin.SOURCE_ID:
            parsed = tianjin.parse_page(body, run["request"]["page_size"])
            if parsed["status"] not in ("ok", "empty"):
                if not capture["error_code"]:
                    failures.append({"capture_id": capture["id"], "url": capture["url"], "error": "export_parse_error"})
                continue
            for row in parsed["rows"]:
                identity = "snapshot:" + row["record_sha256"]
                fields = row["fields"]
                # API记录快照不是采购网站全文；无附件/原公告URL时保留明确缺口。
                documents.append({**common, "parser_version": tianjin.PARSER_VERSION,
                    "source_record_key": identity, "identity_kind": "content_snapshot",
                    "content": {"status": "ok", "title": fields.get("公告标题"),
                        "body_text": "\n".join(k + "：" + v for k, v in fields.items() if v),
                        "metadata": [{"label": k, "text": v,
                            "locator": f"$.list[{row['row_index']}][{column_index}]"}
                            for column_index, (k, v) in enumerate(fields.items()) if v],
                        "segments": [], "links": [], "attachments": [],
                        "material_status": "not_provided_by_api", "attribution": tianjin.ATTRIBUTION}})
        elif capture["kind"] == "notice" and source == "cn_ccgp":
            try:
                content = parse_capture("notice", body, capture["headers"], capture["final_url"] or capture["url"])
            except (ValueError, TypeError, UnicodeError):
                content = {"status": "parse_error"}
            if content["status"] not in ("ok", "partial"):
                if not capture["error_code"]:
                    failures.append({"capture_id": capture["id"], "url": capture["url"], "error": "export_parse_error"})
                continue
            original_links = {link["url"]: link for link in (capture["parsed"] or {}).get("attachments", ())}
            for link in content.get("attachments", ()):
                attachment = acquired.get(link["url"])
                if attachment:
                    if attachment["sha256"]:
                        store.read_blob(attachment["sha256"])
                    link.update(fetch_status="failed" if attachment["error_code"] else "fetched",
                                capture_id=attachment["id"], sha256=attachment["sha256"])
                else:
                    link["fetch_status"] = original_links.get(link["url"], {}).get("status", "not_fetched")
            documents.append({**common, "parser_version": PARSER_VERSION,
                              "source_record_key": capture["final_url"] or capture["url"],
                              "identity_kind": "source_url", "content": content})
    return {"schema_version": 1, "kind": "raw_evidence_bundle", "source_id": source,
            "simulation": run["request"].get("simulation", False),
            "run_id": run_id, "run_status": run["status"],
            "coverage": "bounded_sample", "documents": documents, "failures": failures}
