#!/usr/bin/env python3
"""Correct test API usage, then record actual verification in the existing plan."""
from __future__ import annotations
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def record_results() -> None:
    plan = ROOT / "docs/investment-analysis-plan.md"
    text = plan.read_text(encoding="utf-8")
    checks = [("锁定依赖安装", "INSTALL_RESULT", None), ("修改前端文件的 Prettier", "FORMAT_RESULT", None),
              ("后端全量 unittest", "BACKEND_RESULT", "backend"), ("前端 TypeScript／构建", "BUILD_RESULT", "build"),
              ("前端全量 Vitest", "FRONTEND_RESULT", "frontend"), ("定向浏览器夹具回归（1440／768／390px）", "BROWSER_RESULT", "browser")]
    rows, errors = [], []
    for label, key, log in checks:
        status = os.environ.get(key, "not_run")
        detail = ""
        if log:
            file = Path(f"/tmp/market-{log}.log")
            content = file.read_text(errors="replace") if file.exists() else ""
            content = re.sub(r"\x1b\[[0-9;]*m", "", content)
            pattern = r"Ran (\d+) tests" if log == "backend" else r"Tests\s+(\d+) passed" if log == "frontend" else r"(\d+) passed" if log == "browser" else None
            matches = re.findall(pattern, content) if pattern else []
            if matches:
                detail = f"；{matches[-1]} 项"
            if status != "success":
                candidates = [line for line in content.splitlines() if re.search(r"FAIL|Error:|error TS|failed|^\s*[>❯]", line)]
                errors.append(f"{label}：\n```text\n" + "\n".join(candidates[-15:])[:6000] + "\n```\n")
        rows.append(f"| {label} | {status}{detail} |")
    block = "| 实际检查 | 结果 |\n| --- | --- |\n" + "\n".join(rows)
    block += "\n| 本机真实数据库、全站浏览器与投资效果验收 | 未运行；定向夹具检查不替代真实数据验收 |\n\n"
    block += f"验证入口：GitHub Actions `Apply and verify market consistency`，运行 `{os.environ.get('GITHUB_RUN_ID', 'unknown')}`；固定输入提交 `{os.environ.get('EXPECTED_SHA', 'unknown')}`，工作区修改经测试后同批发布，含测试源码。\n\n"
    block += "\n".join(errors)
    start = text.index("| 实际检查 | 结果 |", text.index("### 1.5 市场一致性"))
    end = text.index("人工复查：", start)
    text = text[:start] + block + text[end:]
    if all(os.environ.get(key) == "success" for _, key, _ in checks):
        text = text.replace("已实现，验证见下表；待用户真实数据复查", "自动验证通过，待用户真实数据复查")
        text = text.replace("已实现，验证见下表；待浏览器与真实数据联调", "自动验证及定向浏览器夹具通过，待真实数据联调")
    plan.write_text(text, encoding="utf-8")
    Path(__file__).unlink()


if "--record-results" in sys.argv:
    record_results()
else:
    target = ROOT / "frontend/src/features/market/RealMarketConsistency.test.tsx"
    text = target.read_text(encoding="utf-8")
    if text.count(", exact: true") != 3:
        raise RuntimeError("Role-query patch anchor changed; refusing an unrelated edit")
    target.write_text(text.replace(", exact: true", ""), encoding="utf-8")
    print("Corrected three unsupported Testing Library role-query options; assertions unchanged.")
