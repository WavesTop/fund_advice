import io
import datetime as dt
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from backend.storage.timeseries import get_timeseries, import_timeseries
from scripts.fund_nav_ths import BASE_URL, fetch_ths_fund_nav, parse_nav_script
from scripts.import_fund_timeseries import refresh_fund_timeseries
from scripts.probe_fund_source import ProbeError


def dense_rows(start: str, end: str, latest: str):
    day, final = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    rows = []
    while day <= final:
        if day.weekday() < 5:
            rows.append([day.strftime("%Y%m%d"), latest if day == final else "1.0000"])
        day += dt.timedelta(days=1)
    return rows


SAMPLES = {
    "011041": {
        "name": "天弘国证生物医药ETF发起式联接C",
        "established": "2021-01-27",
        "unit": dense_rows("2021-01-27", "2026-09-18", "0.4229"),
        "accumulated": dense_rows("2021-01-27", "2026-09-18", "0.4229"),
    },
    "012970": {
        "name": "鹏华国证半导体芯片ETF联接C",
        "established": "2021-08-24",
        "unit": dense_rows("2021-08-24", "2026-09-18", "1.4021"),
        "accumulated": dense_rows("2021-08-24", "2026-09-18", "1.4021"),
    },
}


def profile(code, sample):
    return f'''<html><ul class="g-dialog">
      <li><span class="key">基金代码</span><span class="value">{code}</span></li>
      <li><span class="key">基金简称</span><span class="value">{sample["name"]}</span></li>
      <li><span class="key">成立日期</span><span class="value">{sample["established"]}</span></li>
      <li><span class="key">投资类型</span><span class="value">{sample.get("fund_type", "指数型")}</span></li>
    </ul></html>'''.encode()


def script(code, kind, rows):
    import json
    return f"var {kind}_{code}=".encode() + json.dumps(rows, separators=(",", ":")).encode()


class Response(io.BytesIO):
    def __init__(self, body, url, content_type):
        super().__init__(body)
        self.url = url
        self.headers = {"Content-Type": content_type, "Last-Modified": "Fri, 18 Sep 2026 15:45:09 GMT"}

    def geturl(self):
        return self.url

    def getcode(self):
        return 200


def opener_for(code, sample, *, override=None):
    mapping = {
        f"{BASE_URL}/{code}/interduce.html": (profile(code, sample), "text/html"),
        f"{BASE_URL}/{code}/json/jsondwjz.json": (script(code, "dwjz", sample["unit"]), "application/json"),
        f"{BASE_URL}/{code}/json/jsonljjz.json": (script(code, "ljjz", sample["accumulated"]), "application/json"),
    }
    mapping.update(override or {})

    def open_request(request, timeout):
        del timeout
        body, content_type = mapping[request.full_url]
        self_headers = {key.lower(): value for key, value in request.header_items()}
        if "mozilla" not in self_headers["user-agent"].lower() or code not in self_headers["referer"]:
            raise AssertionError("请求必须携带浏览器身份和当前基金 Referer")
        return Response(body, request.full_url, content_type)

    return open_request


class TonghuashunFundNavTests(unittest.TestCase):
    def test_verified_real_response_shapes_for_011041_and_012970(self):
        for code, sample in SAMPLES.items():
            with self.subTest(code=code):
                calls = []
                result = fetch_ths_fund_nav(
                    code, sample["name"], sample["established"], "2026-09-18",
                    timeout=1, before_request=lambda: calls.append(1), opener=opener_for(code, sample),
                )
                self.assertEqual(result["identity"], {
                    "code": code, "name": sample["name"], "fund_type": "指数型",
                    "established_on": sample["established"],
                })
                self.assertEqual(result["rows"][-1], {
                    "date": "2026-09-18", "unit_nav": sample["unit"][-1][1],
                    "accumulated_nav": sample["accumulated"][-1][1],
                })
                self.assertEqual(result["source_evidence"]["source_row_count"], len(sample["unit"]))
                self.assertEqual(result["source_evidence"]["published_at"], None)
                self.assertEqual(len(calls), 3)

    def test_transient_http_failures_are_retried_within_total_timeout(self):
        code, sample = "011041", SAMPLES["011041"]
        stable = opener_for(code, sample)
        attempts = 0

        def flaky(request, timeout):
            nonlocal attempts
            if request.full_url.endswith("jsondwjz.json"):
                attempts += 1
                if attempts < 3:
                    raise urllib.error.HTTPError(request.full_url, 502, "temporary", {}, None)
            return stable(request, timeout)

        with patch("scripts.fund_nav_ths.time.sleep") as pause:
            result = fetch_ths_fund_nav(
                code, sample["name"], sample["established"], "2026-09-18",
                timeout=1, opener=flaky,
            )
        self.assertEqual(result["rows"][-1]["date"], "2026-09-18")
        self.assertEqual(attempts, 3)
        self.assertEqual(pause.call_count, 2)

    def test_identity_variable_dates_and_numbers_are_fail_closed(self):
        sample = SAMPLES["011041"]
        with self.assertRaises(ProbeError) as caught:
            fetch_ths_fund_nav("011041", "名称不一致", "2021-01-27", "2026-09-18",
                               timeout=1, opener=opener_for("011041", sample))
        self.assertEqual(caught.exception.code, "invalid_response")

        money_profile = {**sample, "fund_type": "货币型"}
        with self.assertRaises(ProbeError) as caught:
            fetch_ths_fund_nav(
                "011041", sample["name"], sample["established"], "2026-09-18",
                timeout=1, opener=opener_for("011041", money_profile),
            )
        self.assertEqual(caught.exception.code, "unsupported_semantics")

        floating_money_profile = {**sample, "fund_type": "货币型"}
        result = fetch_ths_fund_nav(
            "011041", sample["name"], sample["established"], "2026-09-18",
            timeout=1, expected_fund_type="货币型-浮动净值",
            opener=opener_for("011041", floating_money_profile),
        )
        self.assertEqual(result["identity"]["fund_type"], "货币型")

        qdii_reit_profile = {**sample, "fund_type": "QDII-REITs"}
        result = fetch_ths_fund_nav(
            "011041", sample["name"], sample["established"], "2026-09-18",
            timeout=1, opener=opener_for("011041", qdii_reit_profile),
        )
        self.assertEqual(result["identity"]["fund_type"], "QDII-REITs")

    def test_structurally_valid_fragment_is_rejected(self):
        sample = SAMPLES["011041"]
        fragment = [["20260918", "0.4229"]]
        opener = opener_for("011041", sample, override={
            f"{BASE_URL}/011041/json/jsondwjz.json": (script("011041", "dwjz", fragment), "application/json"),
            f"{BASE_URL}/011041/json/jsonljjz.json": (script("011041", "ljjz", fragment), "application/json"),
        })
        with self.assertRaises(ProbeError) as caught:
            fetch_ths_fund_nav("011041", sample["name"], "1900-01-01", dt.date.today().isoformat(),
                               timeout=1, opener=opener)
        self.assertEqual(caught.exception.code, "truncated_response")
        cases = [
            (b'var dwjz_999999=[["20260918","1.0"]]', "invalid_response"),
            (b'var dwjz_011041=[["20260918","1"],["20260918","2"]]', "duplicate_response"),
            (b'var dwjz_011041=[["20260918","0"]]', "invalid_response"),
            (b'var dwjz_011041=[["20260918","1e3"]]', "invalid_response"),
            (b'var dwjz_011041=[["2026-09-18","1"]]', "invalid_response"),
        ]
        for payload, error in cases:
            with self.subTest(error=error), self.assertRaises(ProbeError) as caught:
                parse_nav_script(payload, "011041", "dwjz")
            self.assertEqual(caught.exception.code, error)
        mismatch = opener_for("011041", sample, override={
            f"{BASE_URL}/011041/json/jsonljjz.json": (
                b'var ljjz_011041=[["20210127","1.0000"],["20260918","0.4229"]]', "application/json"
            ),
        })
        with self.assertRaises(ProbeError) as caught:
            fetch_ths_fund_nav("011041", sample["name"], "2021-01-27", "2026-09-18",
                               timeout=1, opener=mismatch)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_refresh_imports_ths_and_failed_refresh_preserves_previous_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            sample = SAMPLES["011041"]
            import_catalog(settings, [{"code": "011041", "name": sample["name"], "fund_type": "指数型-股票"}],
                           policy_version="fixture", source_id="fund_catalog.test_fixture")
            old = [{"date": "2026-09-17", "unit_nav": "9.9999", "accumulated_nav": "9.9999"}]
            import_timeseries(settings, "011041", "nav", old,
                              source_id="fund_nav.test_fixture", policy_version="fixture")
            fetched_rows = [
                {"date": "2021-01-27", "unit_nav": "1.0000", "accumulated_nav": "1.0000"},
                {"date": "2026-09-17", "unit_nav": "0.4240", "accumulated_nav": "0.4240"},
                {"date": "2026-09-18", "unit_nav": "0.4229", "accumulated_nav": "0.4229"},
            ]
            fetched = {
                "rows": fetched_rows,
                "identity": {"code": "011041", "name": sample["name"], "established_on": "2021-01-27"},
                "source_evidence": {"published_at": None, "source_row_count": 3,
                                    "source_min_date": "2021-01-27", "source_max_date": "2026-09-18"},
            }
            with patch("scripts.import_fund_timeseries.fetch_ths_fund_nav", return_value=fetched):
                result = refresh_fund_timeseries(settings, "011041", start_date="20260918", end_date="20260918")
            self.assertEqual(result["source_id"], "fund_nav.ths")
            stored = get_timeseries(settings, "011041", "nav")
            self.assertEqual(stored["rows"], fetched["rows"])
            self.assertEqual(stored["source_id"], "fund_nav.ths")
            with patch("scripts.import_fund_timeseries.fetch_ths_fund_nav",
                       side_effect=ProbeError("invalid_response", "结构漂移")):
                with self.assertRaises(ProbeError):
                    refresh_fund_timeseries(settings, "011041", start_date="20260918", end_date="20260918")
            self.assertEqual(get_timeseries(settings, "011041", "nav")["rows"], fetched["rows"])

    def test_nav_refresh_always_requests_lifetime_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            sample = SAMPLES["012970"]
            import_catalog(settings, [{"code": "012970", "name": sample["name"], "fund_type": "指数型-股票"}],
                           policy_version="fixture", source_id="fund_catalog.test_fixture")
            fetched = {
                "rows": [{"date": "2021-08-24", "unit_nav": "1.0000", "accumulated_nav": "1.0000"}],
                "identity": {"code": "012970", "name": sample["name"], "established_on": "2021-08-24"},
                "source_evidence": {"published_at": None, "source_row_count": 1,
                                    "source_min_date": "2021-08-24", "source_max_date": "2021-08-24"},
            }
            with patch("scripts.import_fund_timeseries.fetch_ths_fund_nav", return_value=fetched) as fetch:
                refresh_fund_timeseries(settings, "012970", end_date="20260918")
                self.assertEqual(fetch.call_args.args[2:4], ("1900-01-01", dt.date.today().isoformat()))
                refresh_fund_timeseries(settings, "012970", start_date="20260101", end_date="20260918")
                self.assertEqual(fetch.call_args.args[2:4], ("1900-01-01", dt.date.today().isoformat()))

    def test_fragment_cannot_replace_broader_local_nav(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            sample = SAMPLES["011041"]
            import_catalog(settings, [{"code": "011041", "name": sample["name"], "fund_type": "指数型-股票"}],
                           policy_version="fixture", source_id="fund_catalog.test_fixture")
            old = [
                {"date": "2021-01-27", "unit_nav": "1", "accumulated_nav": "1"},
                {"date": "2026-09-18", "unit_nav": "0.4229", "accumulated_nav": "0.4229"},
            ]
            import_timeseries(settings, "011041", "nav", old,
                              source_id="fund_nav.test_fixture", policy_version="fixture")
            fragment = {
                "rows": [old[-1]],
                "identity": {"code": "011041", "name": sample["name"], "established_on": "2021-01-27"},
                "source_evidence": {"source_row_count": 1, "source_min_date": "2026-09-18",
                                    "source_max_date": "2026-09-18"},
            }
            with patch("scripts.import_fund_timeseries.fetch_ths_fund_nav", return_value=fragment):
                with self.assertRaises(RuntimeError) as caught:
                    refresh_fund_timeseries(settings, "011041")
            self.assertIn("truncated_response", str(caught.exception))
            self.assertEqual(get_timeseries(settings, "011041", "nav")["rows"], old)

    def test_candidate_refresh_preserves_manager_nav_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(database_path=Path(directory) / "app.sqlite3")
            sample = SAMPLES["012970"]
            import_catalog(settings, [{"code": "012970", "name": sample["name"], "fund_type": "指数型-股票"}],
                           policy_version="fixture", source_id="fund_catalog.test_fixture")
            official = [{"date": "2026-09-18", "unit_nav": "1.4021", "accumulated_nav": "1.4021"}]
            import_timeseries(settings, "012970", "nav", official,
                              source_id="fund_nav.manager_documents", policy_version="official")
            with patch("scripts.import_fund_timeseries.fetch_ths_fund_nav") as fetch:
                result = refresh_fund_timeseries(settings, "012970")
            self.assertTrue(result["preserved"])
            fetch.assert_not_called()
            self.assertEqual(get_timeseries(settings, "012970", "nav")["source_id"],
                             "fund_nav.manager_documents")

    def test_money_funds_and_reits_are_rejected_before_nav_network(self):
        cases = (
            ("000009", "易方达天天理财货币A", "货币型-普通货币"),
            ("508000", "华安张江产业园REIT", "Reits"),
        )
        for code, name, fund_type in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                settings = Settings(database_path=Path(directory) / "app.sqlite3")
                import_catalog(settings, [{"code": code, "name": name, "fund_type": fund_type}],
                               policy_version="fixture", source_id="fund_catalog.test_fixture")
                with patch("scripts.import_fund_timeseries.fetch_ths_fund_nav") as fetch:
                    with self.assertRaises(ValueError):
                        refresh_fund_timeseries(settings, code)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
