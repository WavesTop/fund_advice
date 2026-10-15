"""Archive public PBC LPR announcements and their displayed publication dates."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
INDEX = "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/3876551/index.html"
ANNOUNCEMENT = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日.*贷款市场报价利率.*公告")


def parse_announcement(body: bytes, url: str):
    soup = BeautifulSoup(body, "html.parser")
    title = soup.select_one("h2")
    content = soup.select_one(".content")
    if title is None or content is None:
        raise ValueError("Missing announcement title or body")
    match = ANNOUNCEMENT.fullmatch(title.get_text(strip=True))
    if match is None:
        raise ValueError("Not a dated LPR announcement")
    announced = date(*map(int, match.groups()))
    text = re.sub(r"\s+", "", content.get_text(" ", strip=True))
    quoted = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if not quoted or date(*map(int, quoted[0])) != announced:
        raise ValueError("Announcement body and title dates differ")
    rates = []
    for pattern in (r"1年期LPR(?:为|是)?\s*(\d+(?:\.\d+)?)[%％]", r"5年期以上LPR(?:为|是)?\s*(\d+(?:\.\d+)?)[%％]"):
        values = re.findall(pattern, text)
        if len(values) != 1 or not 0 < float(values[0]) < 20:
            raise ValueError("Missing or ambiguous LPR quote")
        rates.append(float(values[0]))
    stamps = re.findall(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", soup.get_text(" ", strip=True))
    if len(stamps) != 1:
        raise ValueError("Missing or ambiguous displayed publication timestamp")
    published = datetime.fromisoformat(stamps[0]).replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
    visible = max(announced, datetime.fromisoformat(published).date())
    return {"announcement_date": announced.isoformat(), "displayed_published_at": published,
            "visible_date": visible.isoformat(), "lpr_1y_pct": rates[0], "lpr_5y_pct": rates[1],
            "source_url": url, "publication_basis": "displayed_timestamp_not_independently_archived_first_publication"}


def collect(folder: Path, cutoff: date, *, interval=.5):
    folder.mkdir(parents=True, exist_ok=True)
    raw, last_fetch = {}, 0.

    def get(url):
        nonlocal last_fetch
        if urlparse(url).hostname != "www.pbc.gov.cn":
            raise ValueError("Collector only permits public PBC pages")
        key = sha256(url.encode()).hexdigest()
        path = folder / (key + ".html")
        metadata = folder / (key + ".json")
        if path.exists() and metadata.exists():
            body = path.read_bytes()
            entry = json.loads(metadata.read_text())
            if entry["url"] != url or entry["sha256"] != sha256(body).hexdigest():
                raise ValueError("Archive identity or checksum mismatch")
        else:
            time.sleep(max(0., interval - (time.monotonic() - last_fetch)))
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError("Unexpected public page response")
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("Public page exceeds archive size limit")
            last_fetch = time.monotonic()
            entry = {"url": url, "path": path.name, "sha256": sha256(body).hexdigest(),
                     "fetched_at": datetime.now(timezone.utc).isoformat(), "bytes": len(body)}
            path.write_bytes(body)
            metadata.write_text(json.dumps(entry, indent=2))
        raw[url] = entry
        return body

    pending, visited, articles = [INDEX], set(), set()
    while pending:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        soup = BeautifulSoup(get(url), "html.parser")
        for anchor in soup.select("a[href]"):
            if ANNOUNCEMENT.fullmatch(anchor.get("title") or anchor.get_text(strip=True)):
                articles.add(urljoin(url, anchor["href"]))
        for anchor in soup.select("a[tagname]"):
            target = anchor["tagname"]
            if target.startswith("/zhengcehuobisi/") and target.endswith(".html"):
                pending.append(urljoin(url, target))
    rows = []
    for url in sorted(articles):
        row = parse_announcement(get(url), url)
        if date.fromisoformat(row["announcement_date"]) >= date(2019, 8, 20) and date.fromisoformat(row["visible_date"]) <= cutoff:
            rows.append(row)
        print(json.dumps({"date": row["announcement_date"], "visible": row["visible_date"],
                          "rates": [row["lpr_1y_pct"], row["lpr_5y_pct"]]}), flush=True)
    rows.sort(key=lambda r: r["announcement_date"])
    months = [r["announcement_date"][:7] for r in rows]
    if len(months) != len(set(months)):
        raise ValueError("Duplicate monthly LPR announcements")
    wanted = []
    year, month = 2019, 8
    while (year, month) <= (cutoff.year, cutoff.month):
        if (year, month) == (cutoff.year, cutoff.month) and cutoff.day < 20:
            break
        wanted.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if months != wanted:
        raise ValueError(f"Monthly archive incomplete: {sorted(set(wanted) - set(months))}")
    return {"source": INDEX, "cutoff": cutoff.isoformat(), "rows": rows, "raw": list(raw.values()),
            "collected_at": datetime.now(timezone.utc).isoformat(), "collector_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "limitations": ["Displayed original timestamps are not independently verified first-publication vintages",
                            "LPR reform starts 2019-08-20; do not merge the former quoting mechanism",
                            "Announced interest rates are macro factors, not bond total returns or investment instructions"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runtime/verification/lpr-announcements-20261013")
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date(2026, 10, 12))
    args = parser.parse_args()
    result = collect(args.output_dir, args.cutoff)
    output = args.output_dir / "input.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "months": len(result["rows"])}))


if __name__ == "__main__":
    main()
