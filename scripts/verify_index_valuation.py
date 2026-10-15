"""Read-only cross-check of CSI history against dated PDF and XLS source labels."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "runtime/verification/index-valuation-20261013/input.json"


def verify():
    import xlrd
    from pypdf import PdfReader

    payload = json.loads(INPUT.read_text())
    for raw in payload["raw"]:
        if sha256((INPUT.parent / raw["path"]).read_bytes()).hexdigest() != raw["sha256"]:
            raise ValueError("Archived source checksum differs")
    evidence = {}
    for code, item in payload["indices"].items():
        pdf = INPUT.parent / item["assets"]["factsheet"]["path"]
        text = PdfReader(pdf).pages[0].extract_text()
        values = re.findall(r"滚动市盈率\s*(\d+(?:\.\d+)?)", text)
        dates = set(re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text))
        if len(values) != 1 or ("2026", "9", "30") not in dates or code not in text:
            raise ValueError("Dated factsheet valuation cannot be located unambiguously")
        day = "2026-09-30"
        quote = next(r for r in item["rows"] if r["date"] == day)
        if quote["peg"] != float(values[0]):
            raise ValueError("API candidate field differs from dated rolling P/E factsheet")
        book = xlrd.open_workbook(INPUT.parent / item["assets"]["indicator"]["path"])
        sheet = book.sheet_by_index(0)
        headers = sheet.row_values(0)
        expected = ("日期Date", "指数代码Index Code", "市盈率1（总股本）P/E1", "市盈率2（计算用股本）P/E2")
        if any(label not in headers for label in expected):
            raise ValueError("Source valuation workbook has different metric headings")
        source_rows = [sheet.row_values(i) for i in range(1, sheet.nrows)]
        if any(row[headers.index("指数代码Index Code")] != code for row in source_rows):
            raise ValueError("Valuation workbook index identity differs")
        current = next(row for row in source_rows if row[headers.index("日期Date")] == "20260930")
        evidence[code] = {"date": day, "api_field": "peg", "api_value": quote["peg"],
                          "factsheet_label": "滚动市盈率", "factsheet_value": float(values[0]),
                          "factsheet_url": item["assets"]["factsheet"]["url"],
                          "indicator_url": item["assets"]["indicator"]["url"],
                          "indicator_pe1": current[headers.index(expected[2])], "indicator_pe2": current[headers.index(expected[3])],
                          "status": "provisional_dated_factsheet_match_not_complete_historical_method_verification"}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "input_sha256": sha256(INPUT.read_bytes()).hexdigest(),
            "checks": evidence, "reader_version": {"xlrd": xlrd.__version__},
            "limitations": ["A same-date factsheet match is not evidence that all historical values used an unchanged formula",
                            "The two labelled XLS P/E columns differ from API rolling valuation and are not substituted",
                            "Industry V1.1 calculation documentation is not adopted as the index calculation definition",
                            "Historical first-release and revision vintages remain unverified; research only"]}


if __name__ == "__main__":
    result = verify()
    path = INPUT.parent / "semantic-evidence.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(path), "checks": result["checks"]}, ensure_ascii=False))
