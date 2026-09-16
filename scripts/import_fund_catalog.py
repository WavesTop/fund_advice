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


def refresh_fund_catalog(settings: Settings, *, registry_path=None) -> dict:
    """One reviewed network collection path shared by the CLI and the web refresh worker."""
    registry = load_registry(registry_path) if registry_path else load_registry()
    sources = (sources_for("fund_catalog", "catalog_seed", path=registry_path)
               if registry_path else sources_for("fund_catalog", "catalog_seed"))
    source = next(item for item in sources if item["id"] == "fund_catalog.eastmoney" and item["automatic"])
    return import_catalog(settings, fetch_catalog(), policy_version=registry["policy_version"], source_id=source["id"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None)
    parser.add_argument("--registry", default=None)
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env() if args.database is None else Settings(database_path=Path(args.database))
        result = refresh_fund_catalog(settings, registry_path=args.registry)
    except Exception as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
