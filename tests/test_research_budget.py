"""金额全是虚构单位，不调用模型；检验争用、恢复和未知计费占额。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from services.research.budget import BudgetLedger, BudgetError


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = self.open()

    def open(self):
        return BudgetLedger(self.path, cap=100, workspace_cap=90, run_cap=60)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_unknown_is_not_free_or_retriable_after_restart(self):
        self.ledger.reserve("a", "run1", "call1", "h", 40)
        self.ledger.abort("call1", dispatched=True)
        self.ledger.close()
        self.ledger = self.open()
        self.assertEqual(self.ledger.summary()["charged_or_reserved"], 40)
        for ident in ("call1", "new-attempt"):
            with self.assertRaisesRegex(BudgetError, "billing_unknown"):
                self.ledger.reserve("a", "run1", ident, "h", 1)
        self.ledger.reserve("a", "run2", "call2", "h", 50)
        with self.assertRaisesRegex(BudgetError, "budget_exhausted"):
            self.ledger.reserve("a", "run3", "call3", "h", 1)

    def test_known_response_replays_without_reserving_twice(self):
        result = {"message": {"content": "fictional"}, "usage": {"input_tokens": 2, "output_tokens": 3}, "model": "fake"}
        self.ledger.reserve("a", "run1", "call1", "h", 50)
        self.ledger.settle("call1", result, 12)
        self.assertEqual(self.ledger.reserve("a", "run1", "call1", "h", 50), result)
        self.assertEqual(self.ledger.summary("a")["charged_or_reserved"], 12)
        self.assertEqual(self.ledger.summary("b")["attempts"], 0)
        with self.assertRaisesRegex(BudgetError, "budget_attempt_conflict"):
            self.ledger.reserve("b", "run1", "call1", "h", 50)

    def test_parallel_reservation_cannot_overdraw_global_cap(self):
        def reserve(n):
            ledger = self.open()
            try:
                ledger.reserve(str(n), str(n), str(n), "hash", 60)
                return "ok"
            except BudgetError as exc:
                return exc.code
            finally:
                ledger.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertCountEqual(results, ["ok", "budget_exhausted"])
        self.assertEqual(self.ledger.summary()["charged_or_reserved"], 60)

    def test_missing_usage_holds_estimate_and_overestimate_truthfully_suspends(self):
        self.ledger.reserve("a", "1", "1", "h", 30)
        with self.assertRaisesRegex(BudgetError, "billing_unknown"):
            self.ledger.settle("1", {"usage": None}, None)
        self.assertEqual(self.ledger.summary()["charged_or_reserved"], 30)
        self.ledger.reserve("b", "2", "2", "h", 20)
        self.ledger.settle("2", {"usage": {}}, 25)
        self.assertTrue(self.ledger.summary()["blocked"])

    def test_missing_database_does_not_reset_authorized_budget(self):
        self.ledger.close()
        self.path.unlink()
        with self.assertRaisesRegex(BudgetError, "budget_ledger_missing"):
            self.open()
