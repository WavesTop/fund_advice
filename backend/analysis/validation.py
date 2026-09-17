"""Frozen diagnostic protocols, chronological label isolation and forward registration.

These are signal/outcome diagnostics, not a second trading, cost or portfolio-NAV
engine. Historical replay is always labelled retrospective; only observations
registered before their entry can be called forward observations.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import json

from backend.analysis.fund_selection import _points
from backend.analysis.research_pipeline import get_run, implementation_hash
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import TradingCalendar, CalendarUnavailable, SHANGHAI, day, get_calendar
from backend.storage.database import connection_scope, migrate
from backend.storage.research import canonical, digest, get_snapshot, subject_key, text, utc_now

VERSION = "signal-validation-v1"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def register_trial(settings: Settings, specification: dict) -> dict:
    """All rules are saved before outcomes; modifying them creates another trial."""
    allowed = {"name", "hypothesis", "subjects", "benchmark_key", "signal_rule", "period", "horizon_sessions",
               "development", "validation", "test", "embargo_sessions", "minimum_observations"}
    if not isinstance(specification, dict) or set(specification) != allowed:
        raise ValueError("验证协议字段必须完整且不能指定创建时间、代码哈希或事后结果")
    rule, period = specification["signal_rule"], specification["period"]
    if rule not in ("price_strong", "evidence_watch") or period not in ("short", "medium", "long"):
        raise ValueError("仅开放价格强势基线和证据观察规则，必须明确周期")
    horizon, embargo, minimum = (specification[key] for key in ("horizon_sessions", "embargo_sessions", "minimum_observations"))
    if (not isinstance(horizon, int) or isinstance(horizon, bool) or horizon not in (20, 60, 120)
            or not isinstance(embargo, int) or isinstance(embargo, bool) or not 0 <= embargo <= 120
            or not isinstance(minimum, int) or isinstance(minimum, bool) or not 2 <= minimum <= 10000):
        raise ValueError("观察期限为20/60/120交易日；隔离期0至120；最少观测数2至10000")
    subjects = specification["subjects"]
    if not isinstance(subjects, list) or not 1 <= len(subjects) <= 200:
        raise ValueError("必须事先固定1至200个对象的研究池")
    subjects = sorted({subject_key(key) for key in subjects})
    if any(key.startswith("fund:") for key in subjects):
        raise ValueError("首版只验证板块/指数信号，基金交易及持仓绩效验证待共享执行服务")
    benchmark = subject_key(specification["benchmark_key"])
    if not benchmark.startswith("tracked_index:"):
        raise ValueError("基准必须为来源明确的指数")
    calendar = get_calendar()
    ranges = {}
    previous_end = None
    for name in ("development", "validation", "test"):
        interval = specification[name]
        if not isinstance(interval, dict) or set(interval) != {"start", "end"}:
            raise ValueError("每个时间区间必须提供start/end")
        start, end = day(interval["start"]), day(interval["end"])
        sessions = calendar.sessions(start, end)
        if not sessions:
            raise ValueError("验证区间没有交易日")
        if previous_end is not None:
            if start <= previous_end:
                raise ValueError("开发、验证、保留测试区间必须严格按时间先后且不重叠")
            between = calendar.sessions(previous_end, start)
            if sum(previous_end < value < start for value in between) < embargo:
                raise ValueError("切分间隔不足预先指定的embargo交易日数")
        ranges[name] = dict(interval)
        previous_end = end
    now = utc_now()
    protocol = {"version": VERSION, "name": text(specification["name"], "name"),
                "hypothesis": text(specification["hypothesis"], "hypothesis", maximum=3000),
                "subjects": subjects, "benchmark_key": benchmark, "signal_rule": rule, "period": period,
                "horizon_sessions": horizon, "embargo_sessions": embargo, "minimum_observations": minimum,
                **ranges, "calendar": calendar.document, "implementation_hash": implementation_hash(),
                "entry_policy": "next_session_close", "return_basis": "verified_reinvested_total_return",
                "execution_mode": "diagnostic_only", "parameter_search": "none",
                "schedule_stride_sessions": horizon + embargo + 1,
                "primary_metric": "selected_mean_excess_label_return_pp",
                "enable_investment_operations": False}
    trial_id = digest(protocol)
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("INSERT OR IGNORE INTO validation_trial VALUES(?,?,?,?)",
                           (trial_id, now, digest(protocol), canonical(protocol)))
    return get_trial(settings, trial_id)


def get_trial(settings: Settings, trial_id: str) -> dict:
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute("SELECT * FROM validation_trial WHERE trial_id=?", (trial_id,)).fetchone()
    if row is None:
        raise AppError("trial_not_found", "验证协议不存在", 404)
    result = dict(row)
    result["protocol"] = json.loads(result.pop("protocol_json"))
    if digest(result["protocol"]) != result["protocol_hash"]:
        raise AppError("trial_integrity_failed", "验证协议校验失败", 500)
    return result


def _selected(run: dict, subject: str, protocol: dict) -> bool:
    states = [item for item in run["output"]["subjects"] if item["subject_key"] == subject]
    if len(states) != 1:
        raise ValueError("固定运行没有该对象的唯一研究结果")
    period = next(item for item in states[0]["periods"] if item["id"] == protocol["period"])
    return period["price"].get("status") == "strong" if protocol["signal_rule"] == "price_strong" else period["state"] == "watch"


def _run_for_protocol(settings: Settings, run_id: str, protocol: dict) -> tuple[dict, dict]:
    run = get_run(settings, run_id)
    if run["implementation_hash"] != protocol["implementation_hash"]:
        raise ValueError("运行与验证协议的代码版本不同")
    snapshot = get_snapshot(settings, run["snapshot_id"])
    if snapshot["selection_mode"] != "system_as_of":
        raise ValueError("严格系统时点验证不接受latest或未验证public_as_of")
    if not set(protocol["subjects"] + [protocol["benchmark_key"]]) <= set(snapshot["manifest"]["subjects"]):
        raise ValueError("运行未覆盖协议预先固定的完整研究池及基准")
    return run, snapshot


def register_forward(settings: Settings, trial_id: str, run_id: str) -> dict:
    trial = get_trial(settings, trial_id)
    protocol = trial["protocol"]
    run, snapshot = _run_for_protocol(settings, run_id, protocol)
    now = utc_now()
    age = (_dt(now) - _dt(run["created_at"])).total_seconds()
    snapshot_age = (_dt(now) - _dt(snapshot["cutoff"])).total_seconds()
    if trial["created_at"] > run["created_at"] or not 0 <= age <= 300 or not 0 <= snapshot_age <= 300:
        raise ValueError("前瞻登记必须先建协议，再使用5分钟内实际生成的当时快照/运行；禁止补登记历史信号")
    calendar = TradingCalendar(protocol["calendar"])
    local_today = _dt(now).astimezone(SHANGHAI).date()
    end = calendar.latest_completed(_dt(snapshot["cutoff"]))
    entry = calendar.next_session(end)
    if entry <= local_today:
        raise ValueError("已进入入场日，不能事后登记为前瞻观察；在完整收盘资料可知后登记")
    phase = next((name for name in ("development", "validation", "test")
                  if day(protocol[name]["start"]) <= end <= day(protocol[name]["end"])), None)
    if phase is None:
        raise ValueError("信号日不在协议预先声明的验证区间")
    observations = []
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for subject in protocol["subjects"]:
                selected = _selected(run, subject, protocol)
                observation_id = digest([trial_id, run_id, subject, protocol["benchmark_key"], protocol["horizon_sessions"]])
                connection.execute("INSERT OR IGNORE INTO forward_observation VALUES(?,?,?,?,?,?,?,?)",
                    (observation_id, trial_id, run_id, now, subject, protocol["benchmark_key"], protocol["horizon_sessions"], entry.isoformat()))
                observations.append({"observation_id": observation_id, "subject_key": subject, "selected": selected})
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return {"trial_id": trial_id, "run_id": run_id, "entry_date": entry.isoformat(), "phase": phase,
            "observations": observations, "status": "registered_not_validated", "investment_operations_enabled": False}


def _outcome_calendar(protocol: dict, manifest: dict) -> TradingCalendar:
    old, current = protocol["calendar"], manifest["calendar"]
    if old["settlement_cutoff"] != current["settlement_cutoff"] or old["timezone"] != current["timezone"]:
        raise ValueError("结果日历改变了原截止口径")
    for year, details in old["years"].items():
        if current["years"].get(year) != details:
            raise ValueError("结果日历修改了既有年份，须单独复核而非静默复用原协议")
    return TradingCalendar(current)


def _label(manifest: dict, calendar: TradingCalendar, subject: str, benchmark: str, entry: str, horizon: int) -> dict:
    exit_date = None
    try:
        start = day(entry)
        end = calendar.shift(start, horizon)
        exit_date = end.isoformat()
        matured = calendar.latest_completed(_dt(manifest["cutoff"]))
        if end > matured:
            return {"status": "not_matured", "entry_date": entry, "exit_date": end.isoformat()}
        points = []
        for key in (subject, benchmark):
            rows = [row for row in _points(manifest["facts"], key, "total_return") if start <= day(row["effective_date"]) <= end]
            quality = calendar.validate_grid([row["effective_date"] for row in rows], end=end, minimum=horizon + 1)
            if quality["status"] != "ready" or rows[0]["effective_date"] != entry:
                raise ValueError(f"{key}缺少完整且已核验的区间总收益；不填零或用价格代替")
            points.append(rows)
        asset, base = (Decimal(rows[-1]["value"]) / Decimal(rows[0]["value"]) - 1 for rows in points)
        return {"status": "matured", "entry_date": entry, "exit_date": end.isoformat(),
                "asset_label_return_pct": str(asset * 100), "benchmark_label_return_pct": str(base * 100),
                "excess_label_return_pp": str((asset - base) * 100),
                "outcome_revision_ids": [row["revision_id"] for rows in points for row in rows]}
    except CalendarUnavailable as exc:
        return {"status": "calendar_unavailable", "entry_date": entry, "reason": str(exc)}
    except ValueError as exc:
        return {"status": "data_insufficient", "entry_date": entry, "exit_date": exit_date, "reason": str(exc)}


def _summarize(cases: list[dict], minimum: int) -> dict:
    mature = [case for case in cases if case["status"] == "matured"]
    selected = [Decimal(case["excess_label_return_pp"]) for case in mature if case["selected"]]
    controls = [Decimal(case["excess_label_return_pp"]) for case in mature if not case["selected"]]
    def mean(values):
        return str(sum(values) / len(values)) if values else None
    counts = {state: sum(case["status"] == state for case in cases) for state in sorted({case["status"] for case in cases})}
    return {"case_count": len(cases), "status_counts": counts, "matured_count": len(mature),
            "selected_count": len(selected), "control_count": len(controls),
            "selected_mean_excess_label_return_pp": mean(selected), "control_mean_excess_label_return_pp": mean(controls),
            "sample_status": "minimum_count_met_not_effectiveness_proven" if len(selected) >= minimum else "insufficient",
            "confidence_interval": None, "uncertainty": "横截面及时间相关未完成独立推断，不提供伪IID置信区间或胜率"}


def _save_report(settings: Settings, trial: dict, manifest_snapshot: dict, cases: list[dict], mode: str, run_exclusions: list[dict] | None = None) -> dict:
    protocol = trial["protocol"]
    output = {"version": VERSION, "trial_id": trial["trial_id"], "protocol_hash": trial["protocol_hash"],
        "outcome_snapshot_id": manifest_snapshot["snapshot_id"], "mode": mode,
        "cases": cases, "run_exclusions": run_exclusions or [], "summary": _summarize(cases, protocol["minimum_observations"]),
        "phases": {name: _summarize([case for case in cases if case.get("phase") == name], protocol["minimum_observations"])
                   for name in ("development", "validation", "test")},
        "return_interpretation": "总收益标签的区间对照，不是可成交策略净值；未模拟费用、滑点、延迟、仓位或现金",
        "investment_effectiveness": "not_established", "investment_operations_enabled": False,
        "future_calendar_policy": "只允许追加新年份，不能静默修改协议已有日历"}
    report_id = digest(output)
    with connection_scope(settings) as connection:
        connection.execute("INSERT OR IGNORE INTO validation_report VALUES(?,?,?,?,?)",
                           (report_id, trial["trial_id"], utc_now(), digest(output), canonical(output)))
    return {"report_id": report_id, **output}


def score_forward(settings: Settings, trial_id: str, outcome_snapshot_id: str) -> dict:
    trial = get_trial(settings, trial_id)
    protocol = trial["protocol"]
    snapshot = get_snapshot(settings, outcome_snapshot_id)
    manifest = snapshot["manifest"]
    calendar = _outcome_calendar(protocol, manifest)
    with connection_scope(settings) as connection:
        observations = [dict(row) for row in connection.execute("SELECT * FROM forward_observation WHERE trial_id=? ORDER BY created_at,rowid", (trial_id,))]
    cases, last_exit = [], {}
    for observation in observations:
        run, original = _run_for_protocol(settings, observation["run_id"], protocol)
        signal_day = calendar.latest_completed(_dt(original["cutoff"]))
        phase = next(name for name in ("development", "validation", "test") if day(protocol[name]["start"]) <= signal_day <= day(protocol[name]["end"]))
        subject = observation["subject_key"]
        case = {"observation_id": observation["observation_id"], "run_id": observation["run_id"],
                "subject_key": subject, "selected": _selected(run, subject, protocol), "phase": phase}
        if observation["created_at"] > manifest["cutoff"]:
            case.update(status="not_yet_registered_at_outcome_cutoff")
        else:
            label = _label(manifest, calendar, subject, protocol["benchmark_key"], observation["entry_date"], protocol["horizon_sessions"])
            case.update(label)
            if case.get("exit_date") and day(case["exit_date"]) > day(protocol[phase]["end"]):
                case.update(status="purged_split_boundary")
            elif subject in last_exit and day(observation["entry_date"]) <= last_exit[subject]:
                case.update(status="purged_overlapping_window")
            elif case.get("exit_date"):
                try:
                    last_exit[subject] = calendar.shift(day(case["exit_date"]), protocol["embargo_sessions"])
                except CalendarUnavailable:
                    case.update(status="calendar_unavailable")
        cases.append(case)
    return _save_report(settings, trial, snapshot, cases, "registered_forward_diagnostic")


def validate_history(settings: Settings, trial_id: str, outcome_snapshot_id: str) -> dict:
    """Use every stored matching run, fixed schedule and first run per decision day.

    We never declare past samples 'unseen' merely because a protocol was saved today.
    Parameters are fixed, not fitted on validation/test. Missing scheduled inputs,
    failed signals, boundary-purged windows and missing labels remain in reports.
    """
    trial = get_trial(settings, trial_id)
    protocol = trial["protocol"]
    snapshot = get_snapshot(settings, outcome_snapshot_id)
    manifest = snapshot["manifest"]
    calendar = _outcome_calendar(protocol, manifest)
    with connection_scope(settings) as connection:
        rows = connection.execute("SELECT run_id FROM analysis_run WHERE implementation_hash=? ORDER BY created_at,rowid LIMIT 5001", (protocol["implementation_hash"],)).fetchall()
    if len(rows) > 5000:
        raise ValueError("运行数超过单次验证上限，请先按研究范围拆分数据库，不截断样本")
    by_day, excluded_runs = {}, []
    for row in rows:
        try:
            run, original = _run_for_protocol(settings, row["run_id"], protocol)
            current_day = calendar.latest_completed(_dt(original["cutoff"]))
            by_day.setdefault(current_day, run)
        except ValueError as exc:
            excluded_runs.append({"run_id": row["run_id"], "reason": str(exc)})
    cases = []
    for phase in ("development", "validation", "test"):
        start, end = day(protocol[phase]["start"]), day(protocol[phase]["end"])
        schedule = calendar.sessions(start, end)[::protocol["schedule_stride_sessions"]]
        for signal_day in schedule:
            for subject in protocol["subjects"]:
                case = {"phase": phase, "signal_date": signal_day.isoformat(), "subject_key": subject,
                        "selected": False, "run_id": None}
                try:
                    entry = calendar.next_session(signal_day)
                    exit_day = calendar.shift(entry, protocol["horizon_sessions"])
                    if exit_day > end:
                        case.update(status="purged_split_boundary", entry_date=entry.isoformat(), exit_date=exit_day.isoformat())
                    elif signal_day not in by_day:
                        case.update(status="missing_system_known_input")
                    else:
                        run = by_day[signal_day]
                        case.update(run_id=run["run_id"], selected=_selected(run, subject, protocol))
                        case.update(_label(manifest, calendar, subject, protocol["benchmark_key"], entry.isoformat(), protocol["horizon_sessions"]))
                except CalendarUnavailable as exc:
                    case.update(status="calendar_unavailable", reason=str(exc))
                cases.append(case)
    return _save_report(settings, trial, snapshot, cases, "retrospective_chronological_diagnostic_not_unseen_oos", excluded_runs)
