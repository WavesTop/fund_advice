"""Offline behaviour checks; synthetic values below are never real fund fixtures."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import probe_fund_source as probe

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_fund_source.py"


def sample_document():
    return {
        "schema_version": 1,
        "samples": [
            {"code": f"{i:06d}", "name": f"测试基金{i}", "tags": ["synthetic"],
             "evidence_urls": ["https://example.test/fixture"]}
            for i in range(1, 17)
        ],
    }


def catalog_result():
    return {
        "columns": ["基金代码", "基金简称", "基金类型"],
        "rows": [{"code": "000001", "name": "测试基金1", "fund_type": "混合型-灵活"}],
        "ignored_private_field": "SECRET_NOT_FOR_REPORT",
    }


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.samples = self.root / "samples.json"
        self.output = self.root / "output"
        self.save_samples(sample_document())

    def save_samples(self, data):
        self.samples.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--samples", str(self.samples),
             "--output-dir", str(self.output), *args],
            capture_output=True, text=True, timeout=5,
        )

    def invoke_main(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = probe.main([
                "--catalog", "--samples", str(self.samples),
                "--output-dir", str(self.output),
            ])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_list_is_offline_and_does_not_write(self):
        with patch.object(probe, "run_catalog", side_effect=AssertionError("network forbidden")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                status = probe.main([
                    "--list-samples", "--samples", str(self.samples),
                    "--output-dir", str(self.output),
                ])
        self.assertEqual(status, 0)
        self.assertEqual(len(json.loads(out.getvalue())["samples"]), 16)
        self.assertFalse(self.output.exists())
        # A separate interpreter must also handle the offline entry point.
        self.assertEqual(self.cli("--list-samples").returncode, 0)

    def test_default_and_conflicting_modes_do_not_request_catalog(self):
        for args in [[], ["--catalog", "--list-samples"]]:
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_timeout_must_be_finite_and_positive(self):
        for value in ["0", "-1", "nan", "inf"]:
            with self.subTest(value=value):
                self.assertNotEqual(self.cli("--catalog", f"--timeout={value}").returncode, 0)
        self.assertFalse(self.output.exists())

    def test_invalid_sample_documents(self):
        documents = [[], {"schema_version": True, "samples": []}]
        too_short = sample_document()
        too_short["samples"].pop()
        documents.append(too_short)
        for code in [123456, "１２３４５６", "12345", "000002"]:
            bad = sample_document()
            bad["samples"][0]["code"] = code
            documents.append(bad)
        for field in ["name", "tags", "evidence_urls"]:
            bad = sample_document()
            bad["samples"][0][field] = "" if field == "name" else []
            documents.append(bad)
        for document in documents:
            with self.subTest(document=document):
                self.save_samples(document)
                with self.assertRaises(probe.ProbeError):
                    probe.load_samples(self.samples)

    def test_catalog_rejects_invalid_structure_and_values(self):
        broken = [None, [], {"columns": "bad", "rows": []}]
        for rows in [[], [None], [{"code": "1", "name": "甲"}],
                     [{"code": "000001", "name": None}],
                     [{"code": "999999", "name": " "}],
                     [{"code": "000001", "name": "甲"}] * 2]:
            broken.append({"columns": ["基金代码", "基金简称", "基金类型"], "rows": rows})
        broken.append({"columns": ["fund_code", "name"], "rows": catalog_result()["rows"]})
        for result in broken:
            with self.subTest(result=result):
                with self.assertRaises(probe.ProbeError):
                    probe.build_report(sample_document()["samples"], result, 0.1, "test")

    def test_success_writes_whitelist_and_reports_missing_samples(self):
        with patch.object(probe, "run_catalog", return_value=(catalog_result(), 0.1)):
            status, stdout, stderr = self.invoke_main()
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        report_text = (Path(stdout.strip()) / "report.json").read_text()
        report = json.loads(report_text)
        self.assertEqual(report["row_count"], 1)
        self.assertEqual(report["matched_samples"][0]["code"], "000001")
        self.assertTrue(report["matched_samples"][0]["name_matches_exactly"])
        self.assertEqual(report["matched_samples"][0]["source_fund_type"], "混合型-灵活")
        self.assertEqual(report["adapter_samples"][0]["code"], "000001")
        self.assertEqual(len(report["missing_sample_codes"]), 15)
        self.assertNotIn("SECRET_NOT_FOR_REPORT", report_text)
        self.assertNotIn("rows", report)

    def test_name_difference_is_visible_without_claiming_identity_equivalence(self):
        result = catalog_result()
        result["rows"][0]["name"] = "另一名称"
        report = probe.build_report(sample_document()["samples"], result, 0.1, "test")
        self.assertFalse(report["matched_samples"][0]["name_matches_exactly"])
        self.assertEqual(report["matched_samples"][0]["expected_name"], "测试基金1")

    def test_same_names_keep_separate_codes_but_duplicate_codes_fail(self):
        result = catalog_result()
        result["rows"].append({**result["rows"][0], "code": "000002"})
        report = probe.build_report(sample_document()["samples"], result, 0.1, "test")
        self.assertEqual([item["code"] for item in report["matched_samples"]], ["000001", "000002"])
        result["rows"][1]["code"] = "000001"
        with self.assertRaises(probe.ProbeError) as error:
            probe.build_report(sample_document()["samples"], result, 0.1, "test")
        self.assertEqual(error.exception.code, "duplicate_response")

    def test_catalog_type_is_required_and_cannot_identify_exchange_listing(self):
        for value in [None, "", 123]:
            result = catalog_result()
            result["rows"][0]["fund_type"] = value
            with self.assertRaises(probe.ProbeError):
                probe.build_report(sample_document()["samples"], result, 0.1, "test")

    def test_real_child_hard_timeout(self):
        started = time.monotonic()
        with patch.object(probe, "_child_code", return_value="import time; time.sleep(3)"):
            with self.assertRaises(probe.ProbeError) as error:
                probe.run_catalog(0.05)
        self.assertEqual(error.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 2)

    def test_child_failure_is_safe_and_reported(self):
        with patch.object(probe, "_child_code", return_value="raise RuntimeError('API_KEY=SECRET_VALUE')"):
            status, stdout, stderr = self.invoke_main()
        self.assertNotEqual(status, 0)
        content = (Path(stdout.strip()) / "report.json").read_text()
        self.assertNotIn("SECRET_VALUE", content + stderr)
        report = json.loads(content)
        self.assertEqual(report["error"]["code"], "upstream_failure")
        self.assertIsInstance(report["elapsed_seconds"], (int, float))
        self.assertIn("akshare_version", report)

    def test_untrusted_child_error_is_not_echoed(self):
        child = 'print(\'{"error": "API_KEY=SECRET_VALUE"}\')'
        with patch.object(probe, "_child_code", return_value=child):
            with self.assertRaises(probe.ProbeError) as error:
                probe.run_catalog(2)
        self.assertEqual(error.exception.code, "invalid_response")
        self.assertNotIn("SECRET_VALUE", str(error.exception))

    def test_output_failure_does_not_claim_success_or_expose_exception(self):
        with patch.object(probe, "run_catalog", return_value=(catalog_result(), 0.1)):
            with patch.object(probe, "write_report", side_effect=OSError("SECRET_PATH")):
                status, stdout, stderr = self.invoke_main()
        self.assertNotEqual(status, 0)
        self.assertEqual(stdout, "")
        self.assertIn("output_error", stderr)
        self.assertNotIn("SECRET_PATH", stderr)

    def test_repeated_reports_use_separate_paths(self):
        first = probe.write_report(self.output, {"status": "success"})
        second = probe.write_report(self.output, {"status": "error"})
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads((first / "report.json").read_text())["status"], "success")


if __name__ == "__main__":
    unittest.main()
