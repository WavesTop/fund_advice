"""Official Tonghuashun ETF identities and reproducible public data snapshots."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import time
import unicodedata
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
ETF_POOL_URL = "https://fund.10jqka.com.cn/quotation/fund_pool/v2/query"
ETF_RANK_URL = "https://fund.10jqka.com.cn/quotation/temp/data_api/fetch/v1/specific_data"
ETF_POOL_KEY = "347c9f28-8a67-48a7-8c05-380ff8e595c7"


def read_identity_cache(group: str, *, max_age: float | None = 3600) -> list[dict]:
    for path in (ROOT / "runtime" / "cache" / f"ths-search-{group}.json",
                 ROOT / "config" / f"ths-search-{group}.json"):
        try:
            root = json.loads(path.read_text(encoding="utf-8"))
            if root.get("source") != "tonghuashun" or root.get("version") != 2:
                continue
            if max_age is not None and time.time() - root["fetched_at"] > max_age:
                continue
            if root.get("rows"):
                return root["rows"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return []


def save_identity_cache(group: str, rows: list[dict]) -> None:
    path = ROOT / "runtime" / "cache" / f"ths-search-{group}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 2, "source": "tonghuashun",
            "fetched_at": time.time(), "rows": rows}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def normalize_query(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def public_json_query(url: str, body: dict) -> dict:
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
        "Source-Id": "ths-hot-list", "Referer": "https://eq.10jqka.com.cn/",
    })
    with urlopen(request, timeout=12) as response:
        payload = response.read(10 * 1024 * 1024 + 1)
    if len(payload) > 10 * 1024 * 1024:
        raise ValueError("同花顺目录响应过大")
    result = json.loads(payload)
    if result.get("status_code") != 0:
        raise ValueError(f"同花顺查询失败：{result.get('status_msg')}")
    return result


def parse_etf_pool(root: dict) -> list[dict]:
    data = root["data"]
    indexes = {item["type"]: pos for pos, item in enumerate(data["indexes"])}
    result = []
    for values in data["itemList"]:
        code = str(values[indexes["tradeCode"]])
        market = str(values[indexes["subMarket"]])
        if len(code) != 6 or not code.isdigit() or market not in ("20", "36"):
            raise ValueError("同花顺ETF交易身份无效")
        name = str(values[indexes["etfName"]]).strip()
        aliases = [str(values[indexes[key]]).strip() for key in ("etfName", "simpleName") if key in indexes]
        if not name:
            raise ValueError("同花顺ETF名称缺失")
        prefix = "17" if market == "20" else "33"
        result.append({"code": code, "name": name, "aliases": aliases, "kind": "ETF",
                       "source_id": "sector_daily.ths", "ths_category": "etf",
                       "ths_page_code": code, "ths_market_code": code, "ths_prefix": prefix,
                       "history_source_id": "sector_daily.ths", "history_source_code": f"{prefix}_{code}",
                       "history_identity_match": "native_source_code", "pool_code": f"{market}:{code}"})
    if len(result) != data["total"] or len({item["code"] for item in result}) != len(result):
        raise ValueError("同花顺ETF目录不完整或代码重复")
    return result


@lru_cache(maxsize=1)
def _etf_catalog(hour: int) -> tuple[dict, ...]:
    cached = read_identity_cache("etfs")
    if cached:
        return tuple(cached)
    root = public_json_query(ETF_POOL_URL, {"businessPoolKey": ETF_POOL_KEY,
        "custom": {"fieldList": ["etfName", "subMarket", "tradeCode"], "offset": 0, "limit": 10000}})
    rows = parse_etf_pool(root)
    save_identity_cache("etfs", rows)
    return tuple(rows)


def fetch_ths_etf_directory() -> list[dict]:
    return [dict(item) for item in _etf_catalog(int(time.time() // 3600))]


def fetch_ths_etf_hot100() -> dict:
    codes = [item["pool_code"] for item in fetch_ths_etf_directory()]
    return public_json_query(ETF_RANK_URL, {
        "code_selectors": {"include": [{"type": "stock_code", "values": codes}]},
        "indexes": [{"index_id": "price_change_ratio_pct"}, {"index_id": "ths-hot-data-day-attention-rate"}],
        "sort": [{"idx": 1, "order": "desc"}], "page_info": {"page_begin": 0, "page_size": 100},
    })
