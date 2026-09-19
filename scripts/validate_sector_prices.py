"""Freeze local THS prices and run a reproducible retrospective signal diagnostic."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from backend.analysis.sector_price_validation import digest, run_diagnostic
from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar

ROOT = Path(__file__).resolve().parents[1]


def read_input(database: Path) -> dict:
    # Read-only SQLite transaction: no migrations or writes to the live database.
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        state = dict(connection.execute("SELECT * FROM sector_heat_snapshot WHERE id=1").fetchone())
        boards = []
        for row in connection.execute("SELECT * FROM sector_heat_member ORDER BY code"):
            board = dict(row)
            if (board["source_id"] != "sector_daily.ths"
                    or board["history_source_id"] != "sector_daily.ths"
                    or board["history_identity_match"] != "native_source_code"):
                raise ValueError("当前行业池含未经核验的原生身份")
            boards.append({key: board[key] for key in ("code", "name", "source_id", "updated_at",
                           "history_source_id", "history_source_code", "history_identity_match")}
                          | {"rows": json.loads(board["rows_json"])})
        if len(boards) != state["requested_count"] or state["universe_scope"] != "verified_industry_all":
            raise ValueError("必须冻结完整行业目录，不能选择部分成功行业")
    return {"exported_at": datetime.now(timezone.utc).isoformat(), "as_of": state["as_of"],
            "universe_snapshot": state, "calendar": get_calendar().document, "boards": boards}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def render_report(result: dict) -> str:
    names = {"short": "短期", "medium": "中期", "long": "长期"}
    def number(value, suffix="%"):
        return "—" if value is None else f"{value:+.2f}{suffix}"
    data = result["data"]
    lines = ["# 板块优势榜历史价格初步验证", "",
             f"数据范围：{data['start']} 至 {data['end']}；{data['boards']} 个同花顺行业，"
             f"{data['sessions']} 个交易日，{data['rows']:,} 条日线。", "",
             "本报告检验现有价格优势榜的后续表现；投资有效性仍为未确立。"
             "不能认证中长期经营、估值结论或实际基金收益。", "",
             "## 固定方法", "",
             "直接复用现有20／60／120日价格计算、收益前25%及走势条件，每期最多5行业。"
             "信号只读取截至当日收盘的价格，次日收盘为观察起点，分别观察20／60／120个交易日。"
             "候选按观察起点等权构造价格篮子，期间不再平衡，对照为同日全部冻结行业等权篮子。", "",
             "主表从全池最早数据日加回看窗口的交易日起，按持有期加1个交易日固定间隔取样；"
             "缺资料不移动起点，持有区间不重叠，"
             "但不声称批次统计独立。补充表逐日取样，区间重叠。未调参数、未挑最好日期。", "",
             "## 主结果：不重叠的完整观察批次", "",
             "数值为各批次价格变化的算术平均，非累计收益、年化收益或实际基金净收益。", "",
             "| 周期 | 完成批次 | 入选价格变化 | 全行业对照 | 相对差额 | 跑赢批次 | 最差批次 | 最深批次内回撤 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for key, values in result["periods"].items():
        s = values["primary"]
        lines.append(f"| {names[key]}（{values['forward_sessions']}日） | {s['matured_batches']} | "
                     f"{number(s['mean_selected_return_pct'])} | {number(s['mean_control_return_pct'])} | "
                     f"{number(s['mean_excess_return_pp'], '个百分点')} | "
                     f"{s['outperforming_batches']}/{s['matured_batches']} | {number(s['worst_batch_return_pct'])} | "
                     f"{number(s['worst_within_batch_drawdown_pct'])} |")
    lines += ["", "## 成本压力测试", "",
              "对每批次初始金额分别假设扣除0、20、50、100基点（0%、0.2%、0.5%、1%）往返成本。"
              "下表是入选篮子的平均价格变化减去该假设；未冒充真实基金费率或可成交收益。", "",
              "| 周期 | 0基点 | 20基点 | 50基点 | 100基点 |", "| --- | ---: | ---: | ---: | ---: |"]
    for key, values in result["periods"].items():
        cells = [number(row["mean_selected_after_haircut_pct"]) for row in values["primary"]["cost_stress"]]
        lines.append(f"| {names[key]} | " + " | ".join(cells) + " |")
    lines += ["", "## 逐日补充结果（窗口重叠）", "",
              "这些批次不是独立样本，不能据此计算普通胜率置信区间。", "",
              "| 周期 | 完成批次 | 入选价格变化 | 全行业对照 | 相对差额 | 跑赢批次 |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for key, values in result["periods"].items():
        s = values["overlapping_secondary"]
        lines.append(f"| {names[key]} | {s['matured_batches']} | {number(s['mean_selected_return_pct'])} | "
                     f"{number(s['mean_control_return_pct'])} | {number(s['mean_excess_return_pp'], '个百分点')} | "
                     f"{s['outperforming_batches']}/{s['matured_batches']} |")
    lines += ["", "## 按信号日期前后分组（仅描述稳定性）", "",
              f"固定分界日为数据交易日序列中点 {data['chronological_midpoint']}；两组使用逐日信号，"
              "未来观察区间可能交叉，不称训练集／测试集或未见样本外验证。", "",
              "| 周期 | 较早信号批次 | 较早平均相对差额 | 较晚信号批次 | 较晚平均相对差额 |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for key, values in result["periods"].items():
        a, b = values["earlier_signals"], values["later_signals"]
        lines.append(f"| {names[key]} | {a['matured_batches']} | {number(a['mean_excess_return_pp'], '个百分点')} | "
                     f"{b['matured_batches']} | {number(b['mean_excess_return_pp'], '个百分点')} |")
    lines += ["", "## 覆盖与未完成状态", ""]
    for key, values in result["periods"].items():
        lines.append(f"- {names[key]}主排程：{json.dumps(values['primary']['status_counts'], ensure_ascii=False)}；"
                     f"逐日排程：{json.dumps(values['overlapping_secondary']['status_counts'], ensure_ascii=False)}。")
    lines += ["", "## 主排程逐批明细", "",
              "| 周期 | 信号日 | 观察起点 | 观察终点 | 入选行业 | 状态 | 价格变化 | 相对差额 |",
              "| --- | --- | --- | --- | --- | --- | ---: | ---: |"]
    for key, values in result["periods"].items():
        for row in values["cases"]:
            if row["primary_schedule"]:
                lines.append(f"| {names[key]} | {row['signal_date']} | {row['entry_date'] or '未到'} | "
                             f"{row['exit_date'] or '未到'} | {'、'.join(row['selected_names']) or '无'} | "
                             f"{row['status']} | {number(row.get('selected_return_pct'))} | "
                             f"{number(row.get('excess_return_pp'), '个百分点')} |")
    lines += ["", "## 限制", ""] + [f"- {line}" for line in result["limitations"]]
    lines += ["", "## 复算标识", "", f"- 输入摘要：`{result['input_hash']}`",
              f"- 协议摘要：`{result['protocol_hash']}`",
              "- 同目录 input.json、protocol.json、report.json 保存完整输入、规则及逐日结果。", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--input", type=Path, help="复算已冻结的input.json；提供后不读取数据库")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    snapshot = json.loads(args.input.read_text()) if args.input else read_input(args.database)
    protocol = json.loads((ROOT / "config/sector-price-validation-v1.json").read_text())
    code_files = ["backend/analysis/sector_status.py", "backend/analysis/sector_strength.py", "backend/core/trading_calendar.py",
                  "backend/analysis/sector_price_validation.py", "scripts/validate_sector_prices.py"]
    protocol["implementation_files"] = {file: digest((ROOT / file).read_text()) for file in code_files}
    protocol["calendar_hash"] = digest(snapshot["calendar"])
    # Freeze both before any forward labels are calculated. Does not imply unseen history.
    write_json(args.output / "input.json", snapshot)
    write_json(args.output / "protocol.json", protocol)
    result = run_diagnostic(snapshot, protocol)
    write_json(args.output / "report.json", result)
    (args.output / "report.md").write_text(render_report(result), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "data": result["data"],
                      "periods": {k: {"primary": v["primary"], "overlapping_secondary": v["overlapping_secondary"]}
                                  for k, v in result["periods"].items()}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
