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
    discovery = {}
    if source == "cn_hainan":
        from services.ingestion.sources.public_notices import canonical_notice_url
        for listing in report["captures"]:
            if listing["kind"] != "listing" or not listing["sha256"]:
                continue
            try:
                parsed_list = parse_capture("listing", store.read_blob(listing["sha256"]), listing["headers"],
                                            listing["final_url"] or listing["url"], source)
            except (ValueError, TypeError, UnicodeError):
                parsed_list = {"status": "parse_error"}
            if parsed_list["status"] not in ("ok", "partial", "empty"):
                failures.append({"capture_id": listing["id"], "url": listing["url"], "error": "export_listing_parse_error"})
                continue
            for item in parsed_list.get("items", ()):
                identity = canonical_notice_url(source, item["url"])
                if identity and item.get("published_text"):
                    discovery.setdefault(identity, []).append({"capture_id": listing["id"],
                        "raw_sha256": listing["sha256"], "url": listing["url"],
                        "locator": item["locator"], "published_text": item["published_text"]})
    for capture in report["captures"]:
        if capture["error_code"]:
            failures.append({"capture_id": capture["id"], "url": capture["url"], "error": capture["error_code"]})
        if not capture["sha256"]:
            continue
        body = store.read_blob(capture["sha256"])
        common = {"capture_id": capture["id"], "source_url": capture["url"],
                  "final_url": capture["final_url"], "observed_at": capture["fetched_at"],
                  "raw_sha256": capture["sha256"]}
        if capture["kind"] == "api_page" and source in tianjin.SOURCE_RESOURCES:
            parsed = tianjin.parse_page(body, run["request"]["page_size"])
            if parsed["status"] not in ("ok", "empty"):
                if not capture["error_code"]:
                    failures.append({"capture_id": capture["id"], "url": capture["url"], "error": "export_parse_error"})
                continue
            for row in parsed["rows"]:
                identity = "snapshot:" + row["record_sha256"]
                fields = row["fields"]
                material_fields = tianjin.MATERIAL_FIELDS[source]
                # 公开定义确认了附件字段，但字段内容不是下载许可，也不是已取得文件。
                material_status = ("reference_only" if any((fields.get(k) or "").strip() for k in material_fields)
                                   else "attachment_reference_missing" if material_fields else "not_provided_by_api")
                # API记录快照不是采购网站全文；无附件/原公告URL时保留明确缺口。
                documents.append({**common, "parser_version": tianjin.PARSER_VERSION,
                    "source_record_key": identity, "identity_kind": "content_snapshot",
                    "content": {"status": "ok", "title": fields.get("公告标题"),
                        "body_text": "\n".join(k + "：" + v for k, v in fields.items() if v),
                        "metadata": [{"label": k, "text": v,
                            "locator": f"$.list[{row['row_index']}][{column_index}]"}
                            for column_index, (k, v) in enumerate(fields.items()) if v],
                        "segments": [], "links": [], "attachments": [],
                        "material_status": material_status, "attribution": tianjin.ATTRIBUTION}})
        elif capture["kind"] == "notice" and source in ("cn_ccgp", "cn_hainan"):
            try:
                content = parse_capture("notice", body, capture["headers"], capture["final_url"] or capture["url"], source)
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
            from services.ingestion.sources.public_notices import canonical_notice_url
            identity = canonical_notice_url(source, capture["final_url"] or capture["url"])
            if identity is None:
                raise StoreError("notice_url_outside_source")
            if discovery.get(identity):
                # 海南详情不显示发布日期；日期来自已归档列表而非本次获取时间。
                # 明确写跨原件定位并冻结列表哈希，直接URL入口无列表时保持日期缺失。
                content["discovery_evidence"] = discovery[identity]
                content["metadata"] = list(content.get("metadata", ())) + [
                    {"label": "公告发布时间", "text": evidence["published_text"],
                     "locator": "capture:" + evidence["capture_id"] + " sha256:" + evidence["raw_sha256"]
                                + " " + evidence["locator"]}
                    for evidence in discovery[identity]]
            documents.append({**common, "parser_version": PARSER_VERSION,
                              "source_record_key": identity,
                              "identity_kind": "source_url", "content": content})
    return {"schema_version": 1, "kind": "raw_evidence_bundle", "source_id": source,
            "simulation": run["request"].get("simulation", False),
            "run_id": run_id, "run_status": run["status"],
            "coverage": "bounded_sample", "documents": documents, "failures": failures}
