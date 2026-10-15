"""Cross-check each available theme field; unavailable themes stay explicitly unknown."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "runtime/verification/theme-valuations-20261013/input.json"


def verify():
    from pypdf import PdfReader

    payload = json.loads(INPUT.read_text())
    for raw in payload["raw"]:
        if sha256((INPUT.parent / raw["path"]).read_bytes()).hexdigest() != raw["sha256"]:
            raise ValueError("Theme original archive checksum differs")
    checks = {}
    for item in payload["items"]:
        fund = item["fund_code"]
        code = item["relation"]["index_code"]
        check = {"index_code": code, "theme": item["theme"], "status": "missing_factsheet", "history_status": item["status"]}
        asset = item["assets"].get("factsheet")
        if asset is not None:
            text = PdfReader(INPUT.parent / asset["path"]).pages[0].extract_text()
            stamps = set(re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text))
            values = re.findall(r"滚动市盈率\s*(\d+(?:\.\d+)?)", text)
            if code not in text or ("2026", "9", "30") not in stamps or len(values) != 1:
                check.update(status="ambiguous_factsheet_identity_date_or_metric", source_url=asset["url"])
            else:
                quote = next((r for r in item["rows"] if r["date"] == "2026-09-30"), None)
                check.update(factsheet_date="2026-09-30", factsheet_metric="滚动市盈率", factsheet_value=float(values[0]), source_url=asset["url"])
                if quote is None or quote["peg"] is None:
                    check.update(status="no_same_date_positive_history_quote")
                else:
                    difference = abs(quote["peg"] - float(values[0]))
                    check.update(api_value=quote["peg"], rounding_difference=difference,
                                 status="provisional_dated_factsheet_match" if difference <= .005 else "field_mismatch")
        checks[fund] = check
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "checks": checks, "limitations": ["Provisional field identity from current dated documents, not a complete historical formula/version audit",
                                              "Missing or mismatched themes are not dropped from the investible universe",
                                              "Current accessibility and source verification create retrospective coverage selection; full-pool investment validity remains unproven"]}


if __name__ == "__main__":
    result = verify()
    path = INPUT.parent / "semantic-evidence.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(path), "checks": result["checks"]}, ensure_ascii=False))
