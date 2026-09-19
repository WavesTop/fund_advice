"""Fund catalogue refresh entry point.

The former Eastmoney-backed collector is retired.  Existing catalogue rows keep
their original source id for audit, while this entry point fails before making a
network request until an approved replacement source is registered.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from scripts.data_source_registry import load_registry, sources_for


def fetch_catalog():
    raise RuntimeError("基金目录的东方财富采集入口已退役；尚未配置可自动采集的替代来源")


def refresh_fund_catalog(settings: Settings, *, registry_path=None) -> dict:
    """Reject refresh before I/O when no approved automatic catalogue source exists."""
    load_registry(registry_path) if registry_path else load_registry()
    sources = (sources_for("fund_catalog", "catalog_seed", path=registry_path)
               if registry_path else sources_for("fund_catalog", "catalog_seed"))
    approved = [item for item in sources if item["automatic"] and item["adapter"]]
    if not approved:
        raise RuntimeError("基金目录的东方财富采集入口已退役；尚未配置可自动采集的替代来源")
    raise RuntimeError("基金目录存在注册来源但尚未实现对应采集适配器")


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
