#!/usr/bin/env python3
"""Archive annual THS price history for a frozen, isolated validation snapshot.

This does not open the production database or generate cookies. Each public URL
is requested once; a board receives extended history only if both years and all
overlapping local closes pass validation. The frozen 90-board pool is preserved.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.trading_calendar import TradingCalendar
from backend.integrations.http_transport import MAX_BYTES, get_bytes

VERSION = "ths-annual-validation-history-v1"
YEARS = (2025, 2026)
FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _number(raw: str, field: str) -> str:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 不是有效数值") from exc
    if not value.is_finite() or value < 0 or (field in FIELDS[:4] and value == 0):
        raise ValueError(f"{field} 必须是有限的{'正数' if field in FIELDS[:4] else '非负数'}")
    return format(value, "f")


def parse_annual(raw: bytes, *, code: str, year: int, as_of: date,
                 calendar: TradingCalendar) -> list[dict[str, str]]:
    """Reject wrong identities, future rows and any missing annual session."""
    callback = f"quotebridge_v4_line_bk_{code}_01_{year}"
    match = re.fullmatch(r"\s*" + re.escape(callback) + r"\((\{.*\})\);?\s*",
                         raw.decode("gb18030", "strict"), flags=re.DOTALL)
    if not match:
        raise ValueError("年度 JSONP 回调与板块代码、周期或年份不匹配")
    payload = json.loads(match.group(1), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), str):
        raise ValueError("年度响应缺少 data 字符串")
    rows, seen = [], set()
    for raw_row in payload["data"].split(";"):
        if not raw_row.strip():
            continue
        fields = raw_row.split(",")
        if len(fields) not in (11, 12) or not re.fullmatch(r"\d{8}", fields[0]):
            raise ValueError("年度日线字段数或日期格式无效")
        day = datetime.strptime(fields[0], "%Y%m%d").date()
        if day.year != year or day > as_of:
            raise ValueError(f"年度日线日期越界：{day.isoformat()}")
        if day in seen:
            raise ValueError(f"年度日线日期重复：{day.isoformat()}")
        seen.add(day)
        row = {"date": day.isoformat(),
               **{field: _number(value, field) for field, value in zip(FIELDS, fields[1:7])}}
        opening, high, low, close = (Decimal(row[field]) for field in FIELDS[:4])
        if not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError(f"年度日线 OHLC 关系无效：{day.isoformat()}")
        rows.append(row)
    expected = [day.isoformat() for day in calendar.sessions(
        date(year, 1, 1), min(date(year, 12, 31), as_of))]
    observed = [row["date"] for row in rows]
    if not expected or observed != expected:
        missing = sorted(set(expected) - set(observed))
        unexpected = sorted(set(observed) - set(expected))
        raise ValueError(f"年度交易日历不完整或无序：缺少 {len(missing)} 日 "
                         f"{missing[:5]}；异常 {len(unexpected)} 日 {unexpected[:5]}")
    return rows


def merge_history(board: dict, annual_rows: list[dict], *, as_of: date,
                  calendar: TradingCalendar) -> tuple[list[dict], int]:
    """Any changed or absent frozen close prevents publication for this board."""
    fetched = {row["date"]: row for row in annual_rows}
    if len(fetched) != len(annual_rows):
        raise ValueError("合并年度日线出现重复日期")
    overlap = 0
    for row in board["rows"]:
        if date.fromisoformat(row["date"]).year in YEARS:
            if row["date"] not in fetched:
                raise ValueError(f"年度响应缺少原始行情日期：{row['date']}")
            if Decimal(str(row["close"])) != Decimal(fetched[row["date"]]["close"]):
                raise ValueError(f"与冻结收盘价不一致：{row['date']}")
            overlap += 1
        else:
            fetched[row["date"]] = row
    if not overlap:
        raise ValueError("补采历史与冻结行情没有可核对的重叠收盘价")
    merged = sorted(fetched.values(), key=lambda row: row["date"])
    expected = [day.isoformat() for day in calendar.sessions(
        date.fromisoformat(merged[0]["date"]), as_of)]
    if [row["date"] for row in merged] != expected:
        raise ValueError("合并后的完整历史与冻结交易日历不连续")
    return merged, overlap


class RequestPacer:
    """Space request starts globally, even with two board workers."""
    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self) -> None:
        with self.lock:
            delay = max(0.0, self.next_start - time.monotonic())
            if delay:
                time.sleep(delay)
            self.next_start = time.monotonic() + self.interval


def collect_year(*, code: str, year: int, directory: Path, timeout: float,
                 pacer: RequestPacer, as_of: date, calendar: TradingCalendar) -> tuple[list[dict], dict]:
    url = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js"
    relative = f"raw/{code}/{year}.js"
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"year": year, "url": url, "status": "failed", "http_status": None,
                "fetched_at": None, "raw_path": None, "sha256": None, "bytes": None,
                "response_truncated": False, "error": None}

    def archive(body: bytes) -> None:
        path.write_bytes(body)
        metadata.update(raw_path=relative, sha256=sha256(body).hexdigest(), bytes=len(body))

    def opener(request: Request, *, timeout: float):
        try:
            response = urlopen(request, timeout=timeout)
            metadata["response_url"] = response.geturl()
            return response
        except HTTPError as exc:
            # Transport normally closes HTTP errors without retaining their body.
            # Preserve the returned refusal/challenge page for this isolated audit.
            body = exc.read(MAX_BYTES + 1)
            metadata["response_truncated"] = len(body) > MAX_BYTES
            archive(body)
            raise

    def observe(event: dict) -> None:
        metadata["http_status"] = event.get("status")
        metadata["media_type"] = event.get("media_type")
        if "body" in event:
            archive(event["body"])

    pacer.wait()
    metadata["fetched_at"] = _utc_now()
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*",
                                        "Referer": "https://q.10jqka.com.cn/"})
        raw = get_bytes(request, timeout=timeout, attempts=1, opener=opener, observe=observe)
        rows = parse_annual(raw, code=code, year=year, as_of=as_of, calendar=calendar)
        metadata.update(status="available", row_count=len(rows), start=rows[0]["date"], end=rows[-1]["date"])
        return rows, metadata
    except Exception as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        return [], metadata


def validate_snapshot(snapshot: dict) -> tuple[date, TradingCalendar]:
    boards = snapshot.get("boards", [])
    if len(boards) != 90 or len({board["code"] for board in boards}) != 90:
        raise ValueError("输入必须保留冻结的完整 90 行业池且代码唯一")
    as_of = date.fromisoformat(snapshot["as_of"])
    if as_of.year != YEARS[-1]:
        raise ValueError("本采集协议仅用于截至 2026 年的冻结样本，年度范围为 2025、2026")
    calendar = TradingCalendar(snapshot["calendar"])
    calendar.sessions(date(YEARS[0], 1, 1), as_of)
    if not calendar.is_session(as_of):
        raise ValueError("冻结截止日必须为已核验交易日")
    for board in boards:
        if (board.get("source_id") != "sector_daily.ths" or not board.get("name")
                or not re.fullmatch(r"\d{6}", board["code"]) or not board.get("rows")):
            raise ValueError("冻结行业身份或行情无效")
        dates = [row["date"] for row in board["rows"]]
        if dates != sorted(set(dates)) or any(day > as_of.isoformat() for day in dates):
            raise ValueError(f"冻结行业 {board['code']} 日期无序、重复或晚于截止日")
        for row in board["rows"]:
            if not calendar.is_session(date.fromisoformat(row["date"])):
                raise ValueError(f"冻结行业 {board['code']} 包含非交易日")
            _number(str(row["close"]), "close")
    return as_of, calendar


def collect(input_path: Path, output: Path, *, timeout: float = 20,
            workers: int = 2, interval: float = 0.5) -> dict:
    if workers not in (1, 2) or not 0 < timeout <= 60 or not 0.25 <= interval <= 10:
        raise ValueError("并发限制为 1—2，请求超时为 0—60 秒，请求间隔为 0.25—10 秒")
    original = input_path.read_bytes()
    snapshot = json.loads(original, object_pairs_hook=_unique_object)
    as_of, calendar = validate_snapshot(snapshot)
    output.mkdir(parents=True, exist_ok=False)
    (output / "base-input.json").write_bytes(original)
    pacer, outcomes = RequestPacer(interval), {}
    started = _utc_now()

    def board_job(board: dict) -> tuple[list[dict], dict]:
        annual, requests = [], []
        for year in YEARS:
            rows, request = collect_year(code=board["code"], year=year, directory=output,
                                         timeout=timeout, pacer=pacer, as_of=as_of, calendar=calendar)
            annual.extend(rows)
            requests.append(request)
        metadata = {"code": board["code"], "name": board["name"], "status": "retained_original",
                    "original_rows": len(board["rows"]), "requests": requests, "error": None}
        if any(request["status"] != "available" for request in requests):
            metadata["error"] = "至少一个年度失败，保留该行业全部原始行情，不发布部分补采历史"
            return board["rows"], metadata
        try:
            merged, overlap = merge_history(board, annual, as_of=as_of, calendar=calendar)
            metadata.update(status="extended", published_rows=len(merged), overlap_closes_checked=overlap,
                            start=merged[0]["date"], end=merged[-1]["date"])
            return merged, metadata
        except Exception as exc:
            metadata["error"] = f"{type(exc).__name__}: {exc}"[:1500]
            return board["rows"], metadata

    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(board_job, board): board["code"] for board in snapshot["boards"]}
        for future in as_completed(jobs):
            code = jobs[future]
            outcomes[code] = future.result()
            if len(outcomes) % 10 == 0 or len(outcomes) == len(jobs):
                print(f"已核验 {len(outcomes)}/{len(jobs)} 行业", flush=True)
    result = deepcopy(snapshot)
    for board in result["boards"]:
        board["rows"] = outcomes[board["code"]][0]
    items = [outcomes[board["code"]][1] for board in snapshot["boards"]]
    requests = [request for item in items for request in item["requests"]]
    extended = sum(item["status"] == "extended" for item in items)
    metadata = {
        "version": VERSION, "started_at": started, "completed_at": _utc_now(),
        "base_input_sha256": sha256(original).hexdigest(), "base_input_path": "base-input.json",
        "frozen_as_of": snapshot["as_of"], "requested_years": list(YEARS),
        "request_policy": {"workers": workers, "minimum_start_interval_seconds": interval,
                           "timeout_seconds": timeout, "attempts_per_url": 1, "cookies": False},
        "coverage": {"pool_count": len(items), "extended_boards": extended,
                     "retained_original_boards": len(items) - extended, "scheduled_requests": len(requests),
                     "validated_annual_responses": sum(r["status"] == "available" for r in requests),
                     "published_rows": sum(len(board["rows"]) for board in result["boards"]),
                     "full_pool_history_ready": extended == len(items)},
        "calendar_version": calendar.version, "calendar_hash": calendar.hash,
        "limitations": [
            "年度响应不含名称；代码与周期由精确 JSONP 回调校验，名称沿用冻结行业身份，重叠收盘价逐条核对。",
            "使用当前冻结的 90 行业池和来源目前返回的历史版本，不能证明当时行业目录及历史价格版本，存在存续与修订偏差。",
            "只补采价格及成交量额；不提供当时的财报、估值、成分、官方权重、总收益或基金实际可交易收益。",
            "任一年请求、日期完整性或重叠核验失败，保留该行业全部原始行；不删除失败行业以改善回放结果。",
            "本地独立验证目录保留响应及哈希，不写生产数据库；数据扩充不等于投资方法通过认证。",
        ],
        "boards": items,
    }
    result["history_collection"] = metadata
    for filename, document in (("input.json", result), ("collection.json", metadata)):
        (output / filename).write_text(json.dumps(document, ensure_ascii=False, indent=2,
                                                allow_nan=False) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="含 boards/calendar/as_of 的冻结 input.json")
    parser.add_argument("--output", type=Path, required=True, help="尚不存在的独立验证目录")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--request-interval", type=float, default=0.5)
    args = parser.parse_args()
    metadata = collect(args.input, args.output, timeout=args.timeout,
                       workers=args.workers, interval=args.request_interval)
    print(json.dumps(metadata["coverage"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
