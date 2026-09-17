"""Local point-in-time research CLI; no automatic order execution or AI calls."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.analysis.research_pipeline import run_snapshot, replay_run
from backend.analysis.validation import register_trial, register_forward, score_forward, validate_history
from backend.integrations.research_inputs import capture_current_inputs
from backend.storage.research import append_facts, freeze_snapshot, list_snapshots
from backend.storage.collection_runs import runs, finish_run


def document(path: str) -> dict:
    file = Path(path)
    if file.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("输入文件超过20MiB")
    result = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("输入必须为JSON对象")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="使用隔离数据库验收；省略时使用项目环境配置")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    backup = commands.add_parser("backup")
    backup.add_argument("--output", required=True)
    capture = commands.add_parser("capture", help="将当前本地资料作为今日可知事实保存，并计算固定研究")
    capture.add_argument("--subject", action="append")
    ingest = commands.add_parser("ingest", help="导入经复核的结构化批次和真实原始资料，不伪造首次可知时间")
    ingest.add_argument("--input", required=True)
    snap = commands.add_parser("snapshot")
    snap.add_argument("--subject", action="append", required=True)
    snap.add_argument("--cutoff")
    run = commands.add_parser("run")
    run.add_argument("--snapshot-id", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--run-id", required=True)
    trial = commands.add_parser("trial")
    trial.add_argument("--input", required=True)
    forward = commands.add_parser("forward")
    forward.add_argument("--trial-id", required=True)
    forward.add_argument("--run-id", required=True)
    for name in ("score", "history"):
        command = commands.add_parser(name)
        command.add_argument("--trial-id", required=True)
        command.add_argument("--outcome-snapshot-id", required=True)
    recover = commands.add_parser("recover-collection", help="确认旧服务和所有子进程停止后，记录中断并解除残留互斥")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--confirm-workers-stopped", action="store_true", required=True)
    args = parser.parse_args(argv)
    settings = Settings(database_path=Path(args.database)) if args.database else Settings.from_env()
    try:
        if args.command == "backup":
            from backend.storage.research_backup import backup_database
            result = backup_database(settings, Path(args.output))
        elif args.command == "status":
            result = {"collections": runs(settings), "snapshots": list_snapshots(settings), "investment_effectiveness": "not_established"}
        elif args.command == "capture":
            result = capture_current_inputs(settings, args.subject)
            result["run_id"] = run_snapshot(settings, result["snapshot_id"])["run_id"]
        elif args.command == "ingest":
            data = document(args.input)
            if set(data) - {"facts", "source_url", "raw_file", "media_type"} or not {"facts", "source_url", "raw_file"} <= set(data):
                raise ValueError("导入批次要求facts/source_url/raw_file，可选media_type")
            raw = Path(args.input).resolve().parent / data["raw_file"]
            if raw.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("原始资料超过20MiB")
            result = append_facts(settings, data["facts"], source_url=data["source_url"], raw_body=raw.read_bytes(), media_type=data.get("media_type", "application/octet-stream"))
        elif args.command == "snapshot":
            result = freeze_snapshot(settings, args.subject, cutoff=args.cutoff)
        elif args.command == "run":
            result = run_snapshot(settings, args.snapshot_id)
        elif args.command == "replay":
            result = replay_run(settings, args.run_id)
        elif args.command == "trial":
            result = register_trial(settings, document(args.input))
        elif args.command == "forward":
            result = register_forward(settings, args.trial_id, args.run_id)
        elif args.command in ("score", "history"):
            action = score_forward if args.command == "score" else validate_history
            result = action(settings, args.trial_id, args.outcome_snapshot_id)
        else:
            finish_run(settings, args.run_id, "interrupted", {"reason": "操作者明确确认旧服务及所有子进程已停止"})
            result = {"run_id": args.run_id, "status": "interrupted"}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (AppError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
