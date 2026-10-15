"""Re-run a frozen sector rotation policy without selecting new parameters."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.analysis.sector_rotation import RotationPolicy, VERSION, simulate_rotation
from backend.core.config import Settings
from scripts.optimize_sector_rotation import compact, prepare, source_hashes

DEFAULT_POLICY = ROOT / "config/sector-rotation-experiment-v1.json"


def load_policy(path: Path) -> RotationPolicy:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document["version"] != VERSION:
        raise ValueError("Policy version does not match the simulation engine")
    policy = RotationPolicy(**document["policy"])
    policy.validate()
    return policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--fee-bps", type=Decimal, default=Decimal(10))
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-rotation-frozen-backtest.json")
    args = parser.parse_args()
    _, days, frames, data = prepare(args.database)
    policy = load_policy(args.policy)
    result = simulate_rotation(data["prices"], days, frames, policy=policy, fee_bps=args.fee_bps)
    report = {"version": VERSION, "policy": asdict(policy), "policy_sha256": sha256(args.policy.read_bytes()).hexdigest(),
              "source_hashes": source_hashes(),
              "data_audit": data["audit"], "result": result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "result": compact(result)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
