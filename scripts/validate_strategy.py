"""Run the integrated strategy on stored real history and validate its output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from backend.analysis.strategy_portfolio import historical_strategy_report
from backend.analysis.strategy_validation import validate_strategy_report
from backend.core.config import Settings
from backend.core.errors import AppError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="省略时读取项目环境配置的真实本地数据库")
    parser.add_argument("--initial-cash", default="100000")
    parser.add_argument("--buy-fee-bps", default="10")
    parser.add_argument("--sell-fee-bps", default="10")
    parser.add_argument("--output", help="可选JSON输出文件；为避免覆盖，目标必须不存在")
    args = parser.parse_args(argv)
    settings = Settings(database_path=Path(args.database)) if args.database else Settings.from_env()
    try:
        source = historical_strategy_report(
            settings,
            initial_cash=args.initial_cash,
            buy_fee_bps=args.buy_fee_bps,
            sell_fee_bps=args.sell_fee_bps,
        )
        result = validate_strategy_report(source)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        print(rendered)
        return {"passed": 0, "review_required": 1, "blocked": 2}[result["status"]]
    except (AppError, ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
        print(json.dumps({"status": "blocked", "error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
