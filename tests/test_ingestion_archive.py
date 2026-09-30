"""公共资料账本的离线恢复回归：仅操作临时目录，不联网、不使用真实资料。"""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.ingestion.archive import Store, StoreError


SEARCH_URL = "https://search.ccgp.gov.cn/bxsearch?kw=test"
DETAIL_URL = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260930_test.htm"


class IngestionArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "archive"
        self.store = self.open_store()
        # 推进租约使用受控时钟，不用等待120秒，也不改机器时间。
        clock_patch = patch("services.ingestion.archive.time.time", return_value=1000.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def open_store(self):
        store = Store(self.root)
        self.addCleanup(store.close)
        return store

    def started(self, key="run-1", url=SEARCH_URL, kind="search"):
        run_id, created = self.store.create_run({"source_id": "cn_ccgp", "keywords": "test"}, key)
        self.assertTrue(created)
        token = self.store.acquire(run_id)
        self.store.enqueue(run_id, token, [{"kind": kind, "url": url}])
        return run_id, token, self.store.next_task(run_id)

    def response(self, body=b"synthetic original\r\n\x00\xff", url=SEARCH_URL):
        return SimpleNamespace(body=body, final_url=url, fetched_at="2026-09-30T10:00:00Z",
                               http_status=200, attempts=1,
                               headers={"Content-Type": "text/html; charset=utf-8",
                                        "ETag": '"synthetic"', "Set-Cookie": "must-not-store",
                                        "Authorization": "must-not-store"})

    def test_same_key_and_equivalent_request_reuse_persistent_run(self):
        request = {"source_id": "cn_ccgp", "range": {"to": None, "from": "2026-09-01"}}
        first, created = self.store.create_run(request, "same-key")
        self.assertTrue(created)
        second = self.open_store()
        repeated, created = second.create_run(
            {"range": {"from": "2026-09-01", "to": None}, "source_id": "cn_ccgp"}, "same-key")
        self.assertEqual((repeated, created), (first, False))
        self.assertEqual(second.run(first)["request"], request)
        self.assertEqual(second.run(first)["status"], "queued")

    def test_same_key_changed_request_is_conflict(self):
        run_id, _ = self.store.create_run({"keywords": "old"}, "same-key")
        with self.assertRaisesRegex(StoreError, "idempotency_conflict"):
            self.store.create_run({"keywords": "changed"}, "same-key")
        self.assertEqual(self.store.run(run_id)["request"], {"keywords": "old"})

    def test_invalid_idempotency_keys_do_not_create_runs(self):
        for key in (None, "", "  ", "x" * 129):
            with self.subTest(key=key), self.assertRaisesRegex(StoreError, "invalid_idempotency_key"):
                self.store.create_run({}, key)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_record_preserves_original_bytes_hash_and_capture_metadata(self):
        run_id, token, task = self.started()
        response = self.response()
        capture_id = self.store.record(run_id, token, task, response=response,
                                       parsed={"status": "ok"}, parser_version="test-parser/1")
        capture = self.store.capture(capture_id)
        # 原件允许非UTF-8字节和NUL，不能经过解码/行尾整理后再计算指纹。
        self.assertEqual(capture["sha256"], sha256(response.body).hexdigest())
        self.assertEqual(self.store.read_blob(capture["sha256"]), response.body)
        self.assertEqual(capture["byte_count"], len(response.body))
        self.assertEqual(capture["fetched_at"], response.fetched_at)
        self.assertEqual(capture["http_status"], 200)
        self.assertEqual(capture["parsed"], {"status": "ok"})
        self.assertEqual(capture["parser_version"], "test-parser/1")
        self.assertEqual(capture["headers"], {"Content-Type": "text/html; charset=utf-8", "ETag": '"synthetic"'})
        self.assertIsNone(self.store.next_task(run_id))

    def test_changed_bytes_create_versions_and_identical_bytes_reuse_across_runs(self):
        captures = []
        for index, body in enumerate((b"version one", b"version two", b"version one")):
            run_id, token, task = self.started(f"version-{index}")
            captures.append(self.store.capture(self.store.record(
                run_id, token, task, response=self.response(body))))
        # 内容版本按URL+哈希复用，三次获取事实仍各自保留，不改旧版本。
        self.assertEqual(captures[0]["version_id"], captures[2]["version_id"])
        self.assertNotEqual(captures[0]["version_id"], captures[1]["version_id"])
        versions = self.store.db.execute("SELECT version,sha256 FROM versions ORDER BY version").fetchall()
        self.assertEqual([tuple(row) for row in versions],
                         [(1, sha256(b"version one").hexdigest()), (2, sha256(b"version two").hexdigest())])
        self.assertEqual(len(list(self.store.blobs.glob("*.bin"))), 2)
        self.assertEqual(len({capture["id"] for capture in captures}), 3)

    def test_same_bytes_at_different_urls_share_blob_but_not_source_version(self):
        captures = []
        for index, url in enumerate((SEARCH_URL, DETAIL_URL)):
            run_id, token, task = self.started(f"url-{index}", url=url)
            captures.append(self.store.capture(self.store.record(
                run_id, token, task, response=self.response(b"same bytes", url))))
        self.assertEqual(captures[0]["sha256"], captures[1]["sha256"])
        self.assertNotEqual(captures[0]["version_id"], captures[1]["version_id"])
        self.assertEqual(len(list(self.store.blobs.glob("*.bin"))), 1)

    def test_completed_task_and_deduplicated_following_survive_reopen(self):
        run_id, token, task = self.started()
        following = {"kind": "detail", "url": DETAIL_URL, "parent_url": SEARCH_URL,
                     "payload": {"title": "虚构采购"}}
        capture_id = self.store.record(run_id, token, task, response=self.response(),
                                       following=[following, following])
        self.store.release(run_id, token)
        self.store.close()
        recovered = self.open_store()
        recovered.acquire(run_id)
        self.assertEqual(recovered.task_count(run_id, "detail"), 1)
        self.assertEqual(recovered.next_task(run_id)["url"], DETAIL_URL)
        self.assertEqual(recovered.next_task(run_id)["payload"], following["payload"])
        self.assertEqual([item["id"] for item in recovered.report(run_id)["captures"]], [capture_id])

    def test_invalid_following_rolls_back_capture_version_and_parent_completion(self):
        run_id, token, task = self.started()
        valid = {"kind": "detail", "url": DETAIL_URL}
        invalid = {"kind": "detail", "url": None}
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record(run_id, token, task, response=self.response(), following=[valid, invalid])
        # 后继入库失败时不能留下“父任务完成，但孩子消失”的半次提交。
        self.assertEqual(self.store.report(run_id)["captures"], [])
        self.assertEqual(self.store.next_task(run_id)["id"], task["id"])
        self.assertEqual(self.store.task_count(run_id, "detail"), 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM versions").fetchone()[0], 0)
        self.assertEqual(self.store.read_blob(sha256(self.response().body).hexdigest()), self.response().body)

    def test_interruption_in_transaction_rolls_back_and_expired_run_recovers(self):
        run_id, token, task = self.started()
        enqueue = self.store._enqueue

        def interrupted(run, tasks):
            enqueue(run, tasks)
            raise KeyboardInterrupt("synthetic interruption before commit")

        with patch.object(self.store, "_enqueue", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.store.record(run_id, token, task, response=self.response(),
                                  following=[{"kind": "detail", "url": DETAIL_URL}])
        self.assertEqual(self.store.report(run_id)["captures"], [])
        self.assertEqual(self.store.task_count(run_id, "detail"), 0)
        self.clock.return_value = 1121.0
        recovered = self.open_store()
        new_token = recovered.acquire(run_id)
        recovered.record(run_id, new_token, recovered.next_task(run_id), response=self.response())
        self.assertEqual(len(recovered.report(run_id)["captures"]), 1)
        self.assertEqual(recovered.report(run_id)["pending"], 0)

    def test_duplicate_task_cannot_create_extra_capture_or_following(self):
        run_id, token, task = self.started()
        capture_id = self.store.record(run_id, token, task, response=self.response())
        with self.assertRaisesRegex(StoreError, "task_not_pending_in_run"):
            self.store.record(run_id, token, task, response=self.response(),
                              following=[{"kind": "detail", "url": DETAIL_URL}])
        self.assertEqual([item["id"] for item in self.store.report(run_id)["captures"]], [capture_id])
        self.assertEqual(self.store.task_count(run_id, "detail"), 0)

    def test_task_from_another_run_cannot_be_recorded(self):
        first_id, first_token, _ = self.started("first")
        second_id, _, second_task = self.started("second")
        with self.assertRaisesRegex(StoreError, "task_not_pending_in_run"):
            self.store.record(first_id, first_token, second_task, response=self.response())
        self.assertEqual(self.store.report(first_id)["captures"], [])
        self.assertEqual(self.store.report(second_id)["captures"], [])
        self.assertEqual(self.store.report(second_id)["pending"], 1)

    def test_forged_task_fields_do_not_replace_persisted_url_or_kind(self):
        run_id, token, task = self.started()
        forged = dict(task, url="https://attacker.invalid/private", kind="attachment")
        capture = self.store.capture(self.store.record(run_id, token, forged, response=self.response()))
        self.assertEqual((capture["url"], capture["kind"]), (SEARCH_URL, "search"))
        version = self.store.db.execute("SELECT url FROM versions WHERE id=?", (capture["version_id"],)).fetchone()
        self.assertEqual(version["url"], SEARCH_URL)

    def test_cancel_is_persistent_and_old_worker_cannot_write_blob_or_finish(self):
        run_id, token, task = self.started()
        other = self.open_store()
        other.cancel(run_id)
        with patch.object(self.store, "put_blob", wraps=self.store.put_blob) as write:
            with self.assertRaisesRegex(StoreError, "lease_lost_or_cancelled"):
                self.store.record(run_id, token, task, response=self.response())
            write.assert_not_called()
        with self.assertRaisesRegex(StoreError, "lease_lost_or_cancelled"):
            self.store.finish(run_id, token, "succeeded")
        self.store.release(run_id, token)
        with self.assertRaisesRegex(StoreError, "run_terminal"):
            other.acquire(run_id)
        self.assertEqual(other.run(run_id)["status"], "cancelled")
        self.assertEqual(other.report(run_id)["captures"], [])

    def test_cancel_between_blob_and_transaction_prevents_publication(self):
        run_id, token, task = self.started()
        other = self.open_store()
        write_blob = self.store.put_blob

        def cancel_after_write(data):
            digest = write_blob(data)
            other.cancel(run_id)
            return digest

        with patch.object(self.store, "put_blob", side_effect=cancel_after_write):
            with self.assertRaisesRegex(StoreError, "lease_lost_or_cancelled"):
                self.store.record(run_id, token, task, response=self.response())
        # 已写原件可以成为孤儿，但取消后不能发布capture或改变任务状态。
        self.assertEqual(self.store.report(run_id)["captures"], [])
        self.assertEqual(self.store.report(run_id)["pending"], 1)
        self.assertEqual(self.store.run(run_id)["status"], "cancelled")

    def test_expired_lease_cannot_record_or_renew(self):
        run_id, token, task = self.started()
        self.clock.return_value = 1120.0
        with patch.object(self.store, "put_blob", wraps=self.store.put_blob) as write:
            with self.assertRaisesRegex(StoreError, "lease_lost_or_cancelled"):
                self.store.record(run_id, token, task, response=self.response())
            write.assert_not_called()
        with self.assertRaisesRegex(StoreError, "lease_lost_or_cancelled"):
            self.store.heartbeat(run_id, token)
        self.assertEqual(self.store.report(run_id)["captures"], [])

    def test_two_executors_and_stale_release_cannot_steal_live_lease(self):
        run_id, token, task = self.started()
        other = self.open_store()
        with self.assertRaisesRegex(StoreError, "run_busy"):
            other.acquire(run_id)
        self.clock.return_value = 1121.0
        new_token = other.acquire(run_id)
        self.assertNotEqual(new_token, token)
        self.store.release(run_id, token)
        with self.assertRaisesRegex(StoreError, "lease_lost_or_cancelled"):
            self.store.record(run_id, token, task, response=self.response())
        other.record(run_id, new_token, task, response=self.response())
        other.finish(run_id, new_token, "succeeded")
        self.assertEqual(len(other.report(run_id)["captures"]), 1)

    def test_heartbeat_extends_lease_and_release_allows_immediate_resume(self):
        run_id, token, _ = self.started()
        self.clock.return_value = 1100.0
        self.store.heartbeat(run_id, token)
        self.assertEqual(self.store.run(run_id)["lease_until"], 1220.0)
        self.clock.return_value = 1121.0
        with self.assertRaisesRegex(StoreError, "run_busy"):
            self.open_store().acquire(run_id)
        self.store.release(run_id, token)
        self.assertEqual(self.store.run(run_id)["status"], "queued")
        self.assertNotEqual(self.store.acquire(run_id), token)

    def test_error_capture_preserves_unknown_body_and_status_without_fake_zero(self):
        run_id, token, task = self.started()
        capture = self.store.capture(self.store.record(run_id, token, task,
                                                     error="network_timeout", attempts=2))
        self.assertEqual(capture["error_code"], "network_timeout")
        self.assertEqual(capture["attempts"], 2)
        for field in ("http_status", "sha256", "byte_count", "version_id", "parsed"):
            self.assertIsNone(capture[field], field)
        self.assertIsNone(self.store.next_task(run_id))

    def test_corrupt_blob_cannot_be_read_or_silently_overwritten(self):
        original = b"immutable original"
        digest = self.store.put_blob(original)
        path = self.store.blobs / (digest + ".bin")
        path.write_bytes(b"synthetic disk corruption")
        with self.assertRaisesRegex(StoreError, "blob_corrupt"):
            self.store.read_blob(digest)
        with self.assertRaisesRegex(StoreError, "blob_corrupt"):
            self.store.put_blob(original)
        self.assertEqual(path.read_bytes(), b"synthetic disk corruption")

    def test_missing_and_invalid_blob_ids_are_rejected(self):
        with self.assertRaisesRegex(StoreError, "blob_missing"):
            self.store.read_blob("0" * 64)
        for digest in ("../outside", "..\\outside", "a" * 63, "A" * 64, "0" * 64 + "/x", None):
            with self.subTest(digest=digest), self.assertRaisesRegex(StoreError, "invalid_blob_id"):
                self.store.read_blob(digest)

    def test_replay_keeps_capture_original_parse_and_bytes_unchanged(self):
        run_id, token, task = self.started()
        capture_id = self.store.record(run_id, token, task, response=self.response(),
                                       parser_version="parser/1", parsed={"title": "旧解析"})
        before = self.store.capture(capture_id)
        replay = self.store.save_replay(capture_id, "parser/2", {"title": "修复后解析"})
        self.assertEqual(self.store.capture(capture_id), before)
        self.assertEqual(self.store.read_blob(before["sha256"]), self.response().body)
        row = self.store.db.execute("SELECT * FROM replays WHERE id=?", (replay["id"],)).fetchone()
        self.assertEqual((row["capture_id"], row["parser_version"]), (capture_id, "parser/2"))
        self.assertEqual(json.loads(row["parsed_json"]), {"title": "修复后解析"})
        self.assertEqual(len(self.store.report(run_id)["captures"]), 1)

    def test_replay_requires_existing_capture(self):
        with self.assertRaisesRegex(StoreError, "capture_not_found"):
            self.store.save_replay("missing", "parser/2", {})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM replays").fetchone()[0], 0)

    def test_nonempty_unmarked_directory_is_not_adopted(self):
        unrelated = Path(self.tmp.name) / "unrelated"
        unrelated.mkdir()
        (unrelated / "important.txt").write_bytes(b"unrelated local file")
        with self.assertRaisesRegex(StoreError, "store_directory_not_empty"):
            Store(unrelated)
        self.assertEqual((unrelated / "important.txt").read_bytes(), b"unrelated local file")
        self.assertFalse((unrelated / "ingestion.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
