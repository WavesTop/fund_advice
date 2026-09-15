import tempfile
import unittest
from pathlib import Path

from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from scripts.import_fund_batch import RequestLimiter, claim_items, failed_items, run_batch, status_summary
from scripts.probe_fund_source import ProbeError


class BatchFundImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=Path(self.tmp.name) / "app.sqlite3")
        import_catalog(self.settings, [
            {"code": "000001", "name": "甲", "fund_type": "混合型"},
            {"code": "000002", "name": "乙", "fund_type": "混合型"},
            {"code": "000003", "name": "丙", "fund_type": "混合型"},
        ], policy_version="test")

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, refresher, *, limit=2, retry_failed=False, limiter=None):
        return run_batch(self.settings, "all-history-v1", start_date="20000101", end_date="20260914",
                         registry_path=None, timeout=1, limit=limit, retry_failed=retry_failed,
                         request_interval=0, refresher=refresher, limiter=limiter)

    def test_resume_keeps_failure_and_only_retries_when_requested(self):
        seen = []

        def first(settings, code, **kwargs):
            seen.append((code, kwargs["before_request"]))
            kwargs["before_request"]()
            if code == "000002":
                raise ProbeError("network_error", "安全的网络错误")
            return {"code": code}

        limiter = type("Limiter", (), {"calls": 0, "wait": lambda self: setattr(self, "calls", self.calls + 1)})()
        result = self.invoke(first, limiter=limiter)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(limiter.calls, 2)
        self.assertEqual(status_summary(self.settings, "all-history-v1"), {
            "run_id": "all-history-v1", "total": 3, "pending": 1, "running": 0, "succeeded": 1, "failed": 1,
        })
        self.assertEqual(failed_items(self.settings, "all-history-v1")[0]["last_error_code"], "network_error")

        self.invoke(lambda *_args, **_kwargs: {"ok": True})
        self.assertEqual(status_summary(self.settings, "all-history-v1")["succeeded"], 2)
        self.assertEqual(status_summary(self.settings, "all-history-v1")["failed"], 1)

        self.invoke(lambda *_args, **_kwargs: {"ok": True}, limit=2, retry_failed=True)
        summary = status_summary(self.settings, "all-history-v1")
        self.assertEqual((summary["succeeded"], summary["failed"], summary["pending"]), (3, 0, 0))

    def test_interrupted_running_item_is_claimed_on_resume(self):
        # A process can stop after a claim but before writing a result.  That
        # item must be eligible when the same run id resumes.
        from scripts.import_fund_batch import ensure_run
        ensure_run(self.settings, "interrupted-v1", "20000101", "20260914", None)
        self.assertEqual(claim_items(self.settings, "interrupted-v1", 1, False), ["000001"])
        result = run_batch(self.settings, "interrupted-v1", start_date="20000101", end_date="20260914",
                           registry_path=None, timeout=1, limit=1, request_interval=0,
                           refresher=lambda *_args, **_kwargs: {"ok": True})
        self.assertEqual(result["processed"], 1)
        self.assertEqual(status_summary(self.settings, "interrupted-v1")["succeeded"], 1)

    def test_run_scope_cannot_change_and_limiter_spaces_requests(self):
        clock = lambda: 10.0
        pauses = []
        limiter = RequestLimiter(0.25, clock=clock, sleep=pauses.append)
        limiter.wait()
        limiter.wait()
        self.assertEqual(pauses, [0.25])
        self.invoke(lambda *_args, **_kwargs: {"ok": True}, limit=1)
        with self.assertRaises(ValueError):
            run_batch(self.settings, "all-history-v1", start_date="20010101", end_date="20260914",
                      registry_path=None, timeout=1, limit=1, request_interval=0,
                      refresher=lambda *_args, **_kwargs: {"ok": True})


if __name__ == "__main__":
    unittest.main()
