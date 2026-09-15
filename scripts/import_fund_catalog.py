"""Import the reviewed AKShare fund catalogue into the local SQLite projection."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from scripts.data_source_registry import RegistryError, load_registry, sources_for


def fetch_catalog():
    import akshare as ak
    frame = ak.fund_name_em()
    return frame.to_dict(orient="records")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None)
    parser.add_argument("--registry", default=None)
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry) if args.registry else load_registry()
        source = (sources_for("fund_catalog", "catalog_seed", path=args.registry)
                  if args.registry else sources_for("fund_catalog", "catalog_seed"))
        source = next(item for item in source if item["id"] == "fund_catalog.eastmoney" and item["automatic"])
        rows = fetch_catalog()
        settings = Settings.from_env() if args.database is None else Settings(database_path=Path(args.database))
        result = import_catalog(settings, rows, policy_version=registry["policy_version"], source_id=source["id"])
    except Exception as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
