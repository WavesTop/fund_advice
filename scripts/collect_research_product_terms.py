"""Bounded archival of identified public fund documents, never investor accounts."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    {"id": "000217-20260317-overview", "code": "000217", "expected_send_date": "2026-03-17",
     "source_kind": "fund_manager_document", "url": "https://www.huaan.com.cn/upload2010/2026/03/17/001307059_4a6f3209-cccf-3e75-b325-92c1d34a4300.pdf"},
    {"id": "000217-20260317-prospectus", "code": "000217", "expected_send_date": "2026-03-17",
     "source_kind": "fund_manager_document", "url": "https://www.huaan.com.cn/upload2010/2026/03/17/001306578_0b4c25c3-d593-37df-a2e1-b68e36017751.pdf"},
    {"id": "007301-20250516-overview", "code": "007301", "expected_send_date": "2025-05-16",
     "source_kind": "issuer_public_disclosure", "url": "https://static.cninfo.com.cn/finalpage/2025-05-16/1223562098.PDF"},
    {"id": "007301-20220518-overview", "code": "007301", "expected_send_date": "2022-05-18",
     "source_kind": "issuer_public_disclosure", "url": "https://static.cninfo.com.cn/finalpage/2022-05-18/1213392822.PDF"},
)
MAX_BYTES = 20*1024*1024


def validate_pdf_bytes(body):
    if len(body) > MAX_BYTES or not body.startswith(b"%PDF-"):
        raise ValueError("Oversized response or non-PDF document")


def fetch_pdf(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("www.huaan.com.cn", "static.cninfo.com.cn"):
        raise ValueError("Only the identified public HTTPS disclosure hosts are permitted")
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("Unexpected disclosure response or redirect")
        body = response.read(MAX_BYTES+1)
        headers = {key: response.headers[key] for key in ("Content-Type", "Last-Modified", "ETag")
                   if key in response.headers}
    validate_pdf_bytes(body)
    return body, headers


def collect(folder, *, fetch=fetch_pdf):
    folder.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).read_text()
    entries = []
    for document in DOCUMENTS:
        entry = dict(document, request_started_at=datetime.now(timezone.utc).isoformat())
        try:
            body, headers = fetch(document["url"])
            validate_pdf_bytes(body)
            path = folder/(document["id"]+".pdf")
            with path.open("xb") as stream:
                stream.write(body)
            entry.update(status="downloaded_pending_identity_and_terms_review", fetched_at=datetime.now(timezone.utc).isoformat(), raw_file=path.name,
                         sha256=sha256(body).hexdigest(), bytes=len(body), response_headers=headers)
        except (OSError, ValueError) as error:
            entry.update(status="failed", reason=f"{type(error).__name__}: {error}")
        entries.append(entry)
        print(json.dumps({"id": document["id"], "status": entry["status"]}), flush=True)
    if Path(__file__).read_text() != source:
        raise ValueError("Collector source changed during retrieval")
    result = {"collected_at": datetime.now(timezone.utc).isoformat(), "documents": entries,
              "collector_source": source, "collector_sha256": sha256(source.encode()).hexdigest(),
              "scope": "Original bytes only; download is not identity/rate verification, effective-history proof, publication-time evidence, or investment certification. No production database, orders or account access"}
    with (folder/"manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT/"runtime/verification/product-terms-20261014")
    args = parser.parse_args()
    report = collect(args.output_dir)
    print(json.dumps({"downloaded": sum(d["status"] != "failed" for d in report["documents"]),
                      "manifest": str(args.output_dir/"manifest.json")}), flush=True)
