#!/usr/bin/env python3
"""Verify the actual search API against independently retrieved THS identities."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import random
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bs4 import BeautifulSoup
from asgi_client import ASGITestClient
from backend.api.main import create_app
from backend.core.config import Settings
from backend.integrations.ths_search_catalog import (
    ETF_POOL_KEY, ETF_POOL_URL, ETF_RANK_URL, public_json_query,
)
from scripts.import_sector_heat import fetch_text, _ths_cookie


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runtime" / "verification" / "ths-search")
    parser.add_argument("--seed", type=int, default=20261012)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sources, failures = [], []

    def capture(key: str, url: str, *, body: dict | None = None) -> str | dict:
        start = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        value = public_json_query(url, body) if body is not None else fetch_text(url, 12, headers={"Cookie": _ths_cookie()})
        raw = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
        path = args.output / f"source-{key}.json" if body is not None else args.output / f"source-{key}.html"
        path.write_text(raw, encoding="utf-8")
        sources.append({"key": key, "url": url, "requested_at": start, "sha256": sha256(raw.encode()).hexdigest(),
                        "path": str(path), "request_body": body})
        return value

    concept_html = capture("concept-directory", "https://q.10jqka.com.cn/gn/")
    soup = BeautifulSoup(concept_html, "html.parser")
    native = json.loads(soup.find("input", id="gnSection")["value"])
    concepts = [{"code": row["platecode"], "page_code": row["cid"], "name": row["platename"],
                 "kind": "概念", "change": row.get("199112"), "net_flow": row.get("zjjlr")}
                for row in native.values()]
    by_page = {row["page_code"]: row for row in concepts}
    for anchor in soup.select('a[href*="/gn/detail/code/"]'):
        page_code = anchor["href"].rstrip("/").rsplit("/", 1)[-1]
        if page_code.isdigit() and len(page_code) == 6:
            by_page.setdefault(page_code, {"code": None, "page_code": page_code,
                "name": anchor.get_text(strip=True), "kind": "概念"})
    industry_html = capture("industry-directory", "https://q.10jqka.com.cn/thshy/")
    soup = BeautifulSoup(industry_html, "html.parser")
    industries = {}
    for anchor in soup.select('a[href*="/thshy/detail/code/"]'):
        code = anchor["href"].rstrip("/").rsplit("/", 1)[-1]
        if code.isdigit() and len(code) == 6:
            industries[code] = {"code": code, "page_code": code, "name": anchor.get_text(strip=True), "kind": "行业"}
    by_page.update(industries)
    index_html = capture("index-directory", "https://q.10jqka.com.cn/zs/")
    index_soup = BeautifulSoup(index_html, "html.parser")
    indexes = {}
    for anchor in index_soup.select('a[href*="/zs/detail/code/"]'):
        code = anchor["href"].rstrip("/").rsplit("/", 1)[-1].upper()
        if len(code) == 6 and code.isalnum() and anchor.get_text(strip=True):
            indexes[code] = {"code": code, "name": anchor.get_text(strip=True), "kind": "指数"}
    pool_root = capture("etf-pool", ETF_POOL_URL, body={"businessPoolKey": ETF_POOL_KEY,
        "custom": {"fieldList": ["etfName", "subMarket", "tradeCode"], "offset": 0, "limit": 10000}})
    pool = pool_root["data"]
    columns = {entry["type"]: index for index, entry in enumerate(pool["indexes"])}
    etfs = [{"code": str(values[columns["tradeCode"]]), "name": str(values[columns["etfName"]]),
             "fund_name": str(values[columns["simpleName"]]), "pool_code": str(values[columns["code"]]),
             "kind": "ETF"} for values in pool["itemList"]]
    if len(etfs) != pool["total"]:
        raise ValueError("ETF样本池未完整读取")
    hot_root = capture("etf-hot100", ETF_RANK_URL, body={
        "code_selectors": {"include": [{"type": "stock_code", "values": [row["pool_code"] for row in etfs]}]},
        "indexes": [{"index_id": "price_change_ratio_pct"}, {"index_id": "ths-hot-data-day-attention-rate"}],
        "sort": [{"idx": 1, "order": "desc"}], "page_info": {"page_begin": 0, "page_size": 100}})
    etf_lookup = {row["pool_code"]: row for row in etfs}
    hot100 = [etf_lookup[row["code"]] for row in hot_root["data"]["data"]]
    board_ranks = {}
    for label, field in (("board_net_flow100", "je"), ("board_change100", "tradezdf")):
        rows = []
        for category, pages in (("gnzjl", 2), ("hyzjl", 2)):
            for page in range(1, pages + 1):
                url = f"https://data.10jqka.com.cn/funds/{category}/field/{field}/order/desc/page/{page}/"
                html = capture(f"{label}-{category}-{page}", url)
                table = BeautifulSoup(html, "html.parser")
                page_rows = table.select("tbody tr")
                if not page_rows:
                    raise ValueError(f"榜单分页为空：{url}")
                for tr in page_rows:
                    cells = tr.find_all("td")
                    anchor = tr.select_one('a[href*="/detail/code/"]')
                    if anchor is None or len(cells) < 7:
                        continue
                    page_code = anchor["href"].rstrip("/").rsplit("/", 1)[-1]
                    if not page_code.isdigit() or len(page_code) != 6:
                        ranked_name = anchor.get_text(strip=True)
                        matches = [identity for identity in by_page.values()
                                   if identity["name"].replace(" ", "").casefold() == ranked_name.replace(" ", "").casefold()
                                   and identity["kind"] == ("概念" if category == "gnzjl" else "行业")]
                        if len(matches) != 1:
                            raise ValueError(f"榜单未给出代码且名称身份不唯一：{ranked_name}")
                        page_code = matches[0]["page_code"]
                    identity = by_page.get(page_code)
                    if identity is None:
                        identity = {"code": None, "page_code": page_code, "name": anchor.get_text(strip=True), "kind": "概念"}
                        by_page[page_code] = identity
                    measure = cells[6 if field == "je" else 3].get_text(strip=True).replace("%", "").replace(",", "")
                    try:
                        score = float(measure)
                    except ValueError:
                        failures.append({"stage": "rank_measure", "page_code": page_code, "measure": measure})
                        continue
                    rows.append({**identity, "name": anchor.get_text(strip=True), "rank_value": score})
        unique = {row["page_code"]: row for row in rows}
        board_ranks[label] = sorted(unique.values(), key=lambda row: row["rank_value"], reverse=True)[:100]
        if len(board_ranks[label]) != 100:
            raise ValueError(f"榜单样本不足100：{label}")

    pending = [row for row in by_page.values() if row["code"] is None]
    def resolve_oracle(row: dict) -> None:
        url = f"https://q.10jqka.com.cn/gn/detail/code/{row['page_code']}/"
        html = capture(f"concept-identity-{row['page_code']}", url)
        clid = BeautifulSoup(html, "html.parser").find("input", id="clid")
        if clid is None or not str(clid.get("value", "")).startswith(("884", "885", "886")):
            raise ValueError(f"独立板块行情身份缺失：{row['page_code']}")
        row["code"] = clid["value"]
    print(f"Resolving {len(pending)} independent page identities", flush=True)
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(resolve_oracle, pending))
    for label, rows in board_ranks.items():
        for row in rows:
            row["code"] = by_page[row["page_code"]]["code"]
    concepts = [row for row in by_page.values() if row["kind"] == "概念"]
    population = [*concepts, *industries.values(), *indexes.values(), *etfs]
    population = list({(row["kind"], row["code"]): row for row in population}.values())
    results, checks = {}, []
    stack = ExitStack()
    temporary = stack.enter_context(tempfile.TemporaryDirectory())
    client = stack.enter_context(ASGITestClient(create_app(Settings(database_path=Path(temporary) / "audit.sqlite3"))))
    start = time.monotonic()

    def verify(label: str, rows: list[dict]) -> dict:
        code_ok, name_ok, full_ok = 0, 0, 0
        for row in rows:
            for field in ("code", "name", "fund_name"):
                if not row.get(field):
                    continue
                from urllib.parse import quote
                response = client.get(f"/api/sector-board-search?q={quote(row[field])}")
                returned = response.json().get("items", []) if response.status_code == 200 else []
                match = next((item for item in returned if item.get("code") == row["code"] and item.get("kind") == row["kind"]), None)
                ok = match is not None
                if ok and field == "code":
                    ok = bool(returned and returned[0]["code"] == row["code"])
                code_ok += int(ok and field == "code")
                name_ok += int(ok and field == "name")
                full_ok += int(ok and field == "fund_name")
                check = {"group": label, "query": row[field], "query_kind": field, "expected_code": row["code"],
                         "expected_kind": row["kind"], "passed": ok, "http_status": response.status_code,
                         "returned": [{"code": item["code"], "name": item["name"], "kind": item["kind"]} for item in returned]}
                checks.append(check)
                if not ok:
                    failures.append(check)
        full_count = sum(bool(row.get("fund_name")) for row in rows)
        result = {"samples": len(rows), "code_passed": code_ok, "code_rate": round(100 * code_ok / len(rows), 2),
                  "name_passed": name_ok, "name_rate": round(100 * name_ok / len(rows), 2),
                  "fund_name_samples": full_count, "fund_name_passed": full_ok}
        print(label, json.dumps(result, ensure_ascii=False), flush=True)
        return result

    for label, rows in {**board_ranks, "etf_hot100": hot100}.items():
        results[label] = verify(label, rows)
    for round_number in range(1, 11):
        rng = random.Random(args.seed + round_number)
        results[f"random_{round_number}"] = verify(f"random_{round_number}", rng.sample(population, 100))
    report = {"generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
              "seed": args.seed, "population": {"total": len(population), "industry": len(industries),
              "concept": len(concepts), "index": len(indexes), "etf": len(etfs)}, "results": results,
              "elapsed_seconds": round(time.monotonic() - start, 2), "sources": sources, "failures": failures,
              "checks": checks, "scope": "actual_ASGI_search_API_identity_recognition_not_collection_success",
              "limitations": ["公开资金榜为资金净额，不是已核验的主力流入榜；主力流入前100尚未验收。2026-10-12问财主力榜独立请求返回HTTP 403。",
                              "ETF热度按同花顺24小时用户搜索访问热度，非真实购买量。",
                              "随机样本从独立抓取的行业、概念、指数和ETF池抽取；不宣称覆盖同花顺所有证券。",
                              "榜单在不同时间请求，不是同一瞬时快照；HTTP成功和相似名称均不足以通过验收。"]}
    path = args.output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "summary.md").write_text("# 同花顺搜索验收\n\n" +
        f"日期：{report['generated_at']}。样本池：{len(population)}。随机种子：{args.seed}。\n\n" +
        "| 样本 | 数量 | 代码通过率 | 名称通过率 |\n|---|---:|---:|---:|\n" +
        "\n".join(f"| {label} | {value['samples']} | {value['code_rate']}% | {value['name_rate']}% |" for label, value in results.items()) +
        "\n\n" + "\n\n".join(report["limitations"]), encoding="utf-8")
    print(f"Report: {path}", flush=True)
    stack.close()
    return int(any(row["code_rate"] < 98 or row["name_rate"] < 98 for row in results.values()) or bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
