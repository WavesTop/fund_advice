"""Load and validate the reviewed data-source decisions used by later stages."""

import datetime as dt
import json
from pathlib import Path


DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "config/data-source-registry.json"
DECISIONS = {"conditional", "discovery_only", "authoritative_evidence", "rejected", "not_verified"}
ALLOWED_USES = {
    "discovery", "catalog_seed", "candidate_fact", "cross_check", "reference_calendar",
    "disclosed_snapshot", "primary_evidence", "metadata_index",
}


class RegistryError(ValueError):
    pass


def load_registry(path=DEFAULT_REGISTRY):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError("来源注册表无法读取或不是有效JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RegistryError("来源注册表必须使用schema_version=1")
    if not isinstance(value.get("policy_version"), str) or not value["policy_version"].strip():
        raise RegistryError("policy_version必须是非空字符串")
    try:
        if dt.date.fromisoformat(value["verified_at"]).isoformat() != value["verified_at"]:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryError("verified_at必须是YYYY-MM-DD") from exc
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RegistryError("sources必须是非空数组")
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            raise RegistryError("来源项必须是对象")
        required = {"id", "dataset", "provider", "adapter", "decision", "allowed_uses", "priority",
                    "automatic", "identity_requirement", "coverage", "limitations"}
        if set(source) != required:
            raise RegistryError("来源项字段不完整或包含未知字段")
        if not all(isinstance(source[key], str) and source[key].strip()
                   for key in ("id", "dataset", "provider", "identity_requirement", "coverage")):
            raise RegistryError("来源身份字段不能为空")
        if source["id"] in seen:
            raise RegistryError("来源id不能重复")
        seen.add(source["id"])
        if source["adapter"] is not None and (not isinstance(source["adapter"], str) or not source["adapter"].strip()):
            raise RegistryError("adapter必须为空或非空字符串")
        if source["decision"] not in DECISIONS:
            raise RegistryError("来源decision无效")
        if (not isinstance(source["allowed_uses"], list) or len(set(source["allowed_uses"])) != len(source["allowed_uses"])
                or any(use not in ALLOWED_USES for use in source["allowed_uses"])):
            raise RegistryError("来源allowed_uses无效")
        if type(source["automatic"]) is not bool:
            raise RegistryError("automatic必须是布尔值")
        if source["priority"] is not None and (type(source["priority"]) is not int or source["priority"] <= 0):
            raise RegistryError("priority必须为空或正整数")
        if source["automatic"] and (source["priority"] is None or source["adapter"] is None):
            raise RegistryError("自动来源必须提供适配器和优先级")
        if source["decision"] in {"rejected", "not_verified", "discovery_only"} and source["automatic"]:
            raise RegistryError("未准入或仅发现来源不能自动采集")
        if not isinstance(source["limitations"], list) or not source["limitations"] or any(
                not isinstance(item, str) or not item.strip() for item in source["limitations"]):
            raise RegistryError("limitations必须是非空字符串数组")
    return value


def sources_for(dataset, use=None, path=DEFAULT_REGISTRY):
    registry = load_registry(path)
    selected = [source for source in registry["sources"] if source["dataset"] == dataset]
    if use is not None:
        if use not in ALLOWED_USES:
            raise RegistryError("请求的来源用途无效")
        selected = [source for source in selected if use in source["allowed_uses"]]
    return sorted(selected, key=lambda source: (source["priority"] is None, source["priority"] or 0, source["id"]))
