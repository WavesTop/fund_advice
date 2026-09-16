"""Combine verified industry context with price observations without inventing forecasts."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
import re
from calendar import monthrange
from pathlib import Path

from backend.core.errors import AppError

EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "config" / "sector-industry-evidence.json"
VALUATION_PATH = EVIDENCE_PATH.with_name("sector-valuation-evidence.json")
INDUSTRY_MAX_AGE_DAYS = 45


def evidence_matches(record: dict | None, subject: dict) -> bool:
    """An explicit researched identity, never a same-code/name cross-provider guess."""
    identity = record.get("subject") if isinstance(record, dict) else None
    return isinstance(identity, dict) and all(bool(identity.get(key)) and identity.get(key) == subject.get(key)
                                              for key in ("source_id", "universe_type"))


def load_industry_evidence() -> dict:
    try:
        evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict) or not isinstance(evidence.get("sectors"), dict):
            raise ValueError("missing sectors")
        if datetime.fromisoformat(evidence["retrieved_at"]).tzinfo is None:
            raise ValueError("missing retrieval timezone")
        if not isinstance(evidence.get("version"), str) or not evidence["version"]:
            raise ValueError("missing evidence version")
        for sector in evidence["sectors"].values():
            identifiers = set()
            for metric in sector["metrics"]:
                if metric["id"] in identifiers:
                    raise ValueError("duplicate industry metric")
                identifiers.add(metric["id"])
                periods = set()
                for row in metric["observations"]:
                    if row["period"] in periods or row["unit"] != "%":
                        raise ValueError("duplicate period or wrong unit")
                    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", row["period"]):
                        raise ValueError("invalid cumulative period")
                    periods.add(row["period"])
                    if not isinstance(row["value"], (int, float)) or isinstance(row["value"], bool) or not math.isfinite(row["value"]):
                        raise ValueError("invalid industry number")
                    published = datetime.fromisoformat(row["published_at"])
                    if published.tzinfo is None:
                        raise ValueError("missing publication timezone")
                    year, month = map(int, row["period"].split("-"))
                    if date(year, month, monthrange(year, month)[1]) > published.date():
                        raise ValueError("report period ends after publication")
                    if not isinstance(row["source_url"], str) or not row["source_url"].startswith("https://www.stats.gov.cn/"):
                        raise ValueError("unverified industry source")
        return evidence
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AppError("sector_evidence_unavailable", "行业证据文件缺失或格式无效，请核查后重试", 503) from exc


def industry_context(evidence: dict, code: str, now: datetime) -> dict:
    sector = evidence["sectors"].get(code)
    result = {"status": "missing", "label": "行业依据不足", "summary": "尚无经过核验的适用行业数据。",
              "scope": "未建立行业映射", "mapping_note": "不能用其他行业或基金的表现代填。", "metrics": []}
    if sector is None:
        return result
    result.update(scope=sector["scope"], mapping_note=sector["mapping_note"])
    # Current researched context must not leak into a putative historical run.
    if datetime.fromisoformat(evidence["retrieved_at"]) > now:
        result["summary"] = "该行业快照在所选时点尚未取得，不能用于当时判断。"
        return result
    expected = {"sales_yoy", "funding_yoy"} if code == "931775" else {"revenue_yoy", "profit_yoy"}
    complete = True
    stale = False
    for metric in sector["metrics"]:
        if metric["id"] not in expected:
            continue
        available = sorted(
            [row for row in metric["observations"] if datetime.fromisoformat(row["published_at"]) <= now],
            key=lambda row: row["period"],
        )
        if not available:
            complete = False
            continue
        latest = available[-1]
        previous = available[-2] if len(available) > 1 else None
        stale |= (now.date() - datetime.fromisoformat(latest["published_at"]).date()).days > INDUSTRY_MAX_AGE_DAYS
        complete &= previous is not None
        result["metrics"].append({"id": metric["id"], "label": metric["label"], "latest": latest,
                                  "previous": previous,
                                  "change_pp": round(latest["value"] - previous["value"], 2) if previous else None})
    complete &= {metric["id"] for metric in result["metrics"]} == expected
    if stale:
        result.update(status="stale", label="行业证据待更新", summary="行业数据发布已超过 45 个自然日；保留原文供核对，暂停使用旧数据形成当前经营判断。")
    elif not complete:
        result["summary"] = "关键指标或前一期数据缺失，暂不判断行业变化是否持续。"
    else:
        metrics = result["metrics"]
        if len({m["latest"]["period"] for m in metrics}) != 1 or len({m["previous"]["period"] for m in metrics}) != 1:
            result["summary"] = "指标报告期不一致，不能合并判断。"
        elif any(m["latest"]["period"][:4] != m["previous"]["period"][:4]
                 or int(m["latest"]["period"][5:]) != int(m["previous"]["period"][5:]) + 1 for m in metrics):
            result["summary"] = "累计指标不是同年度相邻报告期，不能据此判断持续改善。"
        elif all(m["latest"]["value"] < 0 for m in metrics):
            result.update(status="pressured", label="行业经营承压", summary="两项关键指标仍同比下降；降幅变化需分别核查，不能把价格反弹当作经营修复。")
        elif all(m["latest"]["value"] > 0 and m["previous"]["value"] > 0 for m in metrics):
            result.update(status="supportive", label="行业增长线索", summary="同年度相邻两期的关键累计指标均同比增长；这是行业背景支持，尚不能证明指数成分公司同步改善。")
        else:
            result.update(status="mixed", label="行业表现分化", summary="收入与利润或两期指标未同步向好，需核查利润增长质量和需求是否兑现。")
    return result


def valuation_context(code: str, now: datetime, *, subject: dict | None = None) -> dict:
    result = {"status": "missing", "summary": "尚缺同口径指数估值及历史位置，无法判断利好是否已充分计价。",
              "pe_ttm": None, "as_of": None, "source_url": None, "observed_at": None, "metrics": []}
    try:
        snapshot = json.loads(VALUATION_PATH.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("indexes"), dict):
            raise ValueError("invalid valuation snapshot")
        observed = datetime.fromisoformat(snapshot["retrieved_at"])
        record = snapshot["indexes"].get(code)
        if observed.tzinfo is None:
            raise ValueError("missing valuation retrieval timezone")
        if observed > now or record is None or (subject is not None and not evidence_matches(record, subject)):
            return result
        business_date = date.fromisoformat(record["as_of"]) if record["as_of"] is not None else None
        if business_date is not None and (business_date.isoformat() != record["as_of"]
                or business_date > min(now.date(), observed.astimezone(now.tzinfo).date())):
            raise ValueError("future valuation business date")
        result.update(as_of=record["as_of"], source_url=record["source_url"], observed_at=snapshot["retrieved_at"])
        result["metrics"] = [{"label": m["label"], "value": m["value"], "unit": m["unit"]} for m in record["metrics"]]
        if not result["metrics"] or any(isinstance(m["value"], bool)
                or not isinstance(m["value"], (int, float)) or not math.isfinite(m["value"]) for m in result["metrics"]):
            raise ValueError("invalid valuation number")
        result["status"] = "available"
        limitations = (
            "该市盈率的计算口径尚待官方方法核对；缺少同口径历史及亏损公司处理说明，不能据此判断便宜或贵。"
            if code == "931775" else
            "来源未披露估值业务日期，动态市盈率口径尚未确认；缺少同口径历史及亏损公司处理说明，不能据此判断便宜或贵。"
        )
        # These snapshots have no comparable history or confirmed methodology.
        # Display the observation but never label it cheap/expensive or as TTM.
        if ((now.date() - observed.astimezone(now.tzinfo).date()).days > 10
                or (business_date is not None and (now.date() - business_date).days > 10)):
            result["status"] = "not_applicable"
            limitations = "估值业务日期或观察快照已超过 10 个自然日，仅供核对旧资料。 " + limitations
        result["summary"] = limitations
        return result
    except (OSError, ValueError, KeyError, TypeError):
        result.update(status="missing", summary="估值快照缺失或无效，需核查来源后补充。", metrics=[],
                      as_of=None, source_url=None, observed_at=None)
        return result


def assess_opportunity(period: dict, industry: dict, valuation: dict) -> dict:
    """A condition-based research screen. Correlated measures never cast votes."""
    horizon = period["id"]
    fundamentals = industry["status"]
    price = period["status"]
    market_known = price in ("strong", "neutral", "weak")
    result = {"status": "insufficient", "label": "暂不能判断投资方向", "summary": "缺少该板块适用的经营和估值证据；走势排名只能说明过去的相对表现。",
              "supports": [], "challenges": [], "conditions": [], "missing": []}
    if fundamentals in ("supportive", "mixed", "pressured"):
        for metric in industry["metrics"]:
            row = metric["latest"]
            fact = f"{row['period']} {metric['label']} {row['value']:+g}%。"
            target = "supports" if row["value"] > 0 else "challenges"
            result[target].append(fact)
    else:
        result["missing"].append(industry["summary"])
    if market_known:
        fact = f"近 {period['lookback_sessions']} 个交易日指数价格变化 {period['return_pct']:+.2f}%，{period['label']}；仅为价格反应。"
        result["supports" if price == "strong" else "challenges"].append(fact)
        if period["risk"] == "elevated":
            result["challenges"].append(f"该窗口最大回撤为 {period['max_drawdown_pct']:.2f}%，需关注历史波动风险。")
    else:
        result["missing"].append(period["reason"])
    result["challenges"].append(industry["mapping_note"])
    result["missing"].append("尚缺指数成分公司的盈利、现金流与行业数据之间的核对。")
    if valuation["status"] == "missing":
        result["missing"].append(valuation["summary"])
    else:
        result["challenges"].append("估值观察尚缺完整口径与历史参照，无法确认利好是否已充分计价。")
        result["missing"].append("尚缺同口径估值历史及负盈利处理说明，不能判定利好是否已充分计价。")

    if horizon == "short":
        result["conditions"] = ["未来 1 周至 1 个月：核实将发生的事件、实际受益公司和市场是否已提前反映，再判断短期机会。"]
        result["missing"].append("尚无已核验的近期催化事件及市场预期数据。")
    elif horizon == "medium":
        result["conditions"] = ["未来 1—3 个月：用订单、销量及业绩披露确认收入和利润是否兑现，并核对该板块成分公司。"]
    else:
        result["conditions"] = ["未来 3—6 个月：验证盈利增长和现金流能否持续，再结合估值判断当前价格是否留有回报空间。"]
        result["missing"].append("相邻累计报告期大部分月份重叠，不足以证明未来 3—6 个月的持续性。")

    property_metrics = {metric["id"] for metric in industry["metrics"]} == {"sales_yoy", "funding_yoy"}
    if fundamentals == "pressured":
        result.update(status="conflict" if price == "strong" else "risk",
                      label="反弹与经营分歧" if price == "strong" else {
                          "short": "近期修复依据不足", "medium": "经营压力待缓解", "long": "修复持续性待证实",
                      }[horizon],
                      summary=("行业销售和到位资金仍同比下降，价格变化尚不足以证实修复逻辑。" if property_metrics else
                               "行业收入和利润仍同比下降，价格变化尚不足以证实经营修复。"))
        result["conditions"].append("继续核对销售回款与到位资金是否同步修复；资金压力加深将削弱修复假设。" if property_metrics else
                                    "继续核对收入、利润及经营现金流是否修复；经营恶化将削弱修复假设。")
    elif fundamentals == "mixed":
        result.update(status="conflict" if price == "strong" else "watch", label={
            "short": "近期缺少明确上涨依据", "medium": "利润增长尚未得到收入支持", "long": "持续增长证据不足",
        }[horizon],
                      summary="行业指标存在分化；即使价格走强，也需确认利润变化能否转化为持续的经营改善。")
        result["conditions"].append("核对需求及收入能否支撑利润，避免把成本收缩或一次性收益当成持续增长。")
    elif fundamentals == "supportive":
        if not market_known:
            result.update(label="经营有线索，行情待核查", summary="行业数据提供增长线索，当前价格佐证缺失或已过期。")
        elif price == "weak":
            result.update(status="conflict", label="行业与走势分歧", summary="行业增长尚未得到价格反应支持，需排查增长已被预期、估值压力或成分映射偏差。")
        elif horizon == "short":
            result.update(status="watch", label="近期缺少明确催化事件", summary="行业增长提供背景，但尚未核实近期事件及预期差，不能据此判断未来一个月会上涨。")
        elif horizon == "medium":
            result.update(status="watch", label="行业增长，成分公司业绩待确认", summary="行业收入和利润增长，尚需核对成分公司是否同步受益，以及当前估值是否已反映这一增长。")
        else:
            result.update(status="watch", label="持续增长与买入价格尚未确认", summary="已有行业增长线索，但现金流、后续盈利和估值参照尚不完整，不能判断未来 3—6 个月的回报空间。")
        result["conditions"].append("若后续需求、盈利或现金流恶化，应重新评估增长假设；涨价本身不能抵消反证。")
    return result
