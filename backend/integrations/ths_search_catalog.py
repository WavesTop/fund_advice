"""Official Tonghuashun ETF identities and reproducible public data snapshots."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import time
from threading import Lock, Thread
import unicodedata
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
ETF_POOL_URL = "https://fund.10jqka.com.cn/quotation/fund_pool/v2/query"
ETF_RANK_URL = "https://fund.10jqka.com.cn/quotation/temp/data_api/fetch/v1/specific_data"
ETF_POOL_KEY = "347c9f28-8a67-48a7-8c05-380ff8e595c7"
_refresh_guard = Lock()
_refresh_tasks: dict[str, Thread] = {}
_refresh_after: dict[str, float] = {}


@lru_cache(maxsize=8)
def _identity_file(path: str, modified: int, size: int) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def refresh_identities_in_background(group: str, loader) -> None:
    with _refresh_guard:
        if (_refresh_tasks.get(group) is not None and _refresh_tasks[group].is_alive()) or time.monotonic() < _refresh_after.get(group, 0):
            return
        _refresh_after[group] = time.monotonic() + 3600
        def refresh() -> None:
            try:
                loader()
            except Exception:
                with _refresh_guard:
                    _refresh_after[group] = time.monotonic() + 60
        task = Thread(target=refresh, daemon=True, name=f"ths-identities-{group}")
        _refresh_tasks[group] = task
        task.start()


def identity_refresh_running() -> bool:
    with _refresh_guard:
        return any(task.is_alive() for task in _refresh_tasks.values())


def read_identity_cache(group: str, *, max_age: float | None = 3600) -> list[dict]:
    for path in (ROOT / "runtime" / "cache" / f"ths-search-{group}.json",
                 ROOT / "config" / f"ths-search-{group}.json"):
        try:
            stat = path.stat()
            root = _identity_file(str(path), stat.st_mtime_ns, stat.st_size)
            if not isinstance(root, dict):
                continue
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


@lru_cache(maxsize=2)
def _search_index(signatures: tuple) -> tuple:
    exact: dict[str, list[int]] = {}
    normalized = []
    for position, values in enumerate(signatures):
        fields = tuple(normalize_query(value) for value in values if value)
        normalized.append(fields)
        for value in set(fields):
            exact.setdefault(value, []).append(position)
    return normalized, exact


def match_ths_candidates(candidates: list[dict], term: str, relaxed_terms: set[str]) -> list[dict]:
    signatures = tuple(tuple(str(item.get(key) or "") for key in
        ("name", "code", "ths_page_code", "ths_market_code", "history_source_code")) +
        tuple(item.get("aliases", [])) for item in candidates)
    fields, exact = _search_index(signatures)
    normalized_term = normalize_query(term)
    if normalized_term in exact:
        positions = exact[normalized_term]
    else:
        terms = tuple(normalize_query(value) for value in relaxed_terms)
        positions = [index for index, values in enumerate(fields)
                     if any(value in field for value in terms for field in values)]
    terms = tuple(normalize_query(value) for value in relaxed_terms)
    def rank(position: int) -> tuple:
        item = candidates[position]
        starts = any(fields[position][0].startswith(value) or fields[position][1].startswith(value) for value in terms)
        return (0 if normalized_term in fields[position] else 1 if starts else 2,
                {"行业": 0, "概念": 1, "指数": 2, "ETF": 3}.get(item["kind"], 4), item["code"])
    return [candidates[position] for position in sorted(positions, key=rank)]


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
    cached = read_identity_cache("etfs", max_age=None)
    if cached:
        if not read_identity_cache("etfs"):
            refresh_identities_in_background("etfs", _refresh_etf_directory)
        return [dict(item) for item in cached]
    return [dict(item) for item in _etf_catalog(int(time.time() // 3600))]


def _refresh_etf_directory() -> tuple[dict, ...]:
    _etf_catalog.cache_clear()
    return _etf_catalog(int(time.time() // 3600))


def fetch_ths_etf_hot100() -> dict:
    codes = [item["pool_code"] for item in fetch_ths_etf_directory()]
    return public_json_query(ETF_RANK_URL, {
        "code_selectors": {"include": [{"type": "stock_code", "values": codes}]},
        "indexes": [{"index_id": "price_change_ratio_pct"}, {"index_id": "ths-hot-data-day-attention-rate"}],
        "sort": [{"idx": 1, "order": "desc"}], "page_info": {"page_begin": 0, "page_size": 100},
    })
