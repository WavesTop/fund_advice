"""Append-only source-exact constituent evidence, also included in research snapshots."""
from __future__ import annotations

import json
from hashlib import sha256

from backend.core.config import Settings
from backend.storage.database import connection_scope, migrate
from backend.storage.research import canonical, digest, subject_key, utc_now

VERSION = "sector-fundamentals-v1"


def membership_identity(board: dict) -> dict:
    membership = board.get("membership", {})
    members = membership.get("members", [])
    identities = sorted((str(row.get("market")), str(row.get("stock_code"))) for row in members)
    return {"hash": digest([board.get("source_id"), board.get("code"), board.get("kind"), identities]),
            "as_of": membership.get("as_of"), "count": len(identities),
            "valid": bool(identities) and len(set(identities)) == len(identities)
                     and membership.get("member_count") == len(identities)
                     and membership.get("status") == "ready"}


def archive_response(settings: Settings, url: str, body: bytes) -> str:
    """Store the original provider response, even when a downstream parser rejects it."""
    if not body or len(body) > 10 * 1024 * 1024:
        raise ValueError("来源响应为空或超过10MiB")
    media, body_hash = "application/json", sha256(body).hexdigest()
    asset_id = digest([url, media, body_hash])
    with connection_scope(settings) as connection:
        connection.execute("INSERT OR IGNORE INTO raw_asset VALUES(?,?,?,?,?,?)",
                           (asset_id, url, media, body, body_hash, utc_now()))
    return asset_id


def save_observation(settings: Settings, payload: dict) -> dict:
    key = subject_key(payload["subject_key"])
    if not key.startswith("hot_board:") or payload.get("version") != VERSION:
        raise ValueError("板块证据身份或版本不匹配")
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        observed = utc_now()
        last = connection.execute("SELECT MAX(observed_at) FROM sector_fundamental_observation").fetchone()[0]
        if last and observed < last:
            raise ValueError("系统时钟倒退，暂停发布板块证据")
        row_id = connection.execute(
            "INSERT INTO sector_fundamental_observation(subject_key,observed_at,payload_hash,payload_json) VALUES(?,?,?,?)",
            (key, observed, digest(payload), canonical(payload)),
        ).lastrowid
        connection.execute("COMMIT")
    return {"observation_id": row_id, "observed_at": observed, **payload}


def known_observations(connection, subjects: list[str], cutoff: str) -> dict[str, dict]:
    """Use the last *attempt*, not the last successful result. Never read beyond cutoff."""
    if not subjects:
        return {}
    placeholders = ",".join("?" for _ in subjects)
    rows = connection.execute(
        f"SELECT * FROM sector_fundamental_observation WHERE subject_key IN ({placeholders}) AND observed_at<=? ORDER BY id",
        (*subjects, cutoff),
    ).fetchall()
    result, histories = {}, {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        if digest(payload) != row["payload_hash"]:
            raise ValueError("板块证据哈希不匹配")
        key = row["subject_key"]
        result[key] = {**payload, "observation_id": row["id"], "observed_at": row["observed_at"]}
        valuation = payload.get("valuation", {})
        if valuation.get("status") == "available" and valuation.get("median_pe_ttm") is not None:
            # One actual observation per date; no backfilling today's members into old years.
            basis = (payload.get("membership_hash"), tuple(valuation.get("positive_codes", [])))
            histories.setdefault(key, {})[(basis, valuation["as_of"])] = {
                "as_of": valuation["as_of"], "value": valuation["median_pe_ttm"],
                "basis": basis, "observation_id": row["id"],
            }
    for key, item in result.items():
        current = item.get("valuation", {})
        basis = (item.get("membership_hash"), tuple(current.get("positive_codes", [])))
        item["valuation_history"] = [value for value in histories.get(key, {}).values() if value["basis"] == basis]
        for value in item["valuation_history"]:
            value.pop("basis")
        item["valuation_history"].sort(key=lambda value: value["as_of"])
    return result
