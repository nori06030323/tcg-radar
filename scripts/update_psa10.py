#!/usr/bin/env python3
"""Update TCG Radar from an *authorized* daily market-data source.

Offline by default. No scraping is performed without a separate affirmative
source-specific permission setting. Standard library only.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "index.html"
WATCHLIST_FILE = ROOT / "data" / "source-watchlist.json"
SITE_DATA = re.compile(r'(<script id="site-data" type="application/json">)(.*?)(</script>)', re.S)
TOKYO = ZoneInfo("Asia/Tokyo")
UA = "TCGRadarMarketUpdater/1.0 (+https://neon-yeot-65bc1f.netlify.app/; low-frequency, permission-gated)"
LIMIT = 2_000_000

class Error(Exception):
    pass

class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        elif tag in ("div", "p", "span", "h1", "h2", "h3", "li", "br", "strong", "section"):
            self.parts.append(" ")
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        else:
            self.parts.append(" ")
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def visible_text(content: str) -> str:
    p = TextParser()
    p.feed(content)
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()

def parse_pokecalook(html: str, card_no: str, today: datetime) -> dict:
    """Read an individual card page, NEVER conflate with other cards/grades."""
    s = visible_text(html)
    normalized = re.sub(r"\s+", "", s).lower()
    if re.sub(r"\s+", "", card_no).lower() not in normalized:
        raise Error(f"Card number mismatch for {card_no}")
    m = re.search(r"最終更新\s*[:：]\s*(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})(?:\s+\d{1,2}:\d{2})?", s)
    if not m:
        raise Error(f"Missing source update date: {card_no}")
    dt = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=TOKYO).date()
    days_old = (today.date() - dt).days
    if not 0 <= days_old <= 3:
        raise Error(f"Source stale/future date for {card_no}: {dt}")
    p = re.search(r"PSA\s*10\s*[（(]\s*直近\s*7日\s*中央値\s*[）)]\s*[¥￥]\s*([\d,]+)", s, re.I)
    if not p:
        raise Error(f"PSA10 current median not found: {card_no}")
    # Locate PSA10 change ONLY after the 'weekly change' heading.
    section = re.search(r"週間変動額(.*?)(?:1ヶ月変動額|1か月変動額|取引データ|取引件数|$)", s)
    if not section:
        raise Error(f"Weekly price-change section not found: {card_no}")
    delta = re.search(r"PSA\s*10\s*([+＋−-]?)\s*[¥￥]\s*([\d,]+)", section[1], re.I)
    if not delta:
        raise Error(f"PSA10 weekly delta not found: {card_no}")
    now = int(p[1].replace(",", ""))
    sign = -1 if delta[1] in ("-", "−") else 1
    diff = sign * int(delta[2].replace(",", ""))
    past = now - diff
    if not (100 <= now <= 100_000_000 and 100 <= past <= 100_000_000):
        raise Error(f"Unreasonable median prices for {card_no}")
    if abs(diff) > max(now, past) * 2:
        raise Error(f"Unreasonable delta for {card_no}")
    return {"priceNow": now, "pricePast": past, "asOfDate": dt.isoformat()}

def download(url: str) -> bytes:
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise Error("Data source must be an ordinary HTTPS URL")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/csv;q=0.9"})
    with urllib.request.urlopen(req, timeout=15) as response:
        if response.status != 200:
            raise Error(f"Unexpected status {response.status}")
        ct = response.headers.get("Content-Length")
        if ct and int(ct) > LIMIT:
            raise Error("Source file exceeds size limit")
        result = response.read(LIMIT + 1)
    if len(result) > LIMIT:
        raise Error("Source file exceeds size limit")
    return result

def check_robots(url: str) -> None:
    u = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((u.scheme, u.netloc, "/robots.txt", "", ""))
    try:
        robots = download(robots_url).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            raise Error(f"Robots access denied: {exc.code}") from exc
        if exc.code != 404:
            raise Error("Cannot establish robots rules") from exc
        return  # Conventional robots semantics: 404 = no rules.
    except Exception as exc:
        raise Error("Cannot verify robots rules: skip rather than guess") from exc
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots.splitlines())
    if not rp.can_fetch(UA, url):
        raise Error("Source disallows our robot; abort")

def fetch_approved_pokecalook(records: list, today: datetime) -> list:
    if os.getenv("POKECALOOK_REUSE_PERMISSION", "").strip() != "yes-i-have-permission":
        raise Error("Owner permission not confirmed; automatic reuse remains OFF")
    source = "https://pokecalook.com"
    results = []
    for c in records:
        url = c["source_url"]
        if not url.startswith(source + "/cards/") or not url.endswith(".html"):
            raise Error("Unexpected source domain/path")
        check_robots(url)
        parsed = parse_pokecalook(download(url).decode("utf-8", errors="replace"), c["number"], today)
        results.append({"number": c["number"], "grade": "PSA10", **parsed,
                        "sourceUrl": url, "source": "ポケカるっく（PSA10成約中央値・許諾取得済）"})
    return results

def fetch_authorized_csv(url: str, today: datetime) -> list:
    if not url:
        raise Error("AUTHORIZED_FEED_URL is missing")
    data = download(url)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Error("CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"number", "grade", "pricePast", "priceNow", "asOfDate", "source", "sourceUrl"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise Error("Source CSV missing required columns")
    rows = []
    for row in reader:
        try:
            dt = datetime.strptime(row["asOfDate"].strip(), "%Y-%m-%d").date()
            past, now = int(row["pricePast"]), int(row["priceNow"])
        except (ValueError, TypeError, KeyError) as exc:
            raise Error("Invalid CSV price or date") from exc
        if row["grade"] != "PSA10" or not (100 <= past <= 100_000_000 and 100 <= now <= 100_000_000):
            raise Error("Wrong grade or invalid price")
        if not 0 <= (today.date() - dt).days <= 3:
            raise Error("Authorized CSV does not contain fresh results")
        if not row["source"].strip() or not row["sourceUrl"].startswith("https://"):
            raise Error("Source name and source URL are mandatory")
        rows.append({"number": row["number"].strip(), "grade": "PSA10", "pricePast": past,
                     "priceNow": now, "asOfDate": dt.isoformat(), "sourceUrl": row["sourceUrl"], "source": row["source"]})
    if not rows:
        raise Error("Authorized source CSV is empty")
    return rows

def update_html(source_rows: list, today: datetime, *, html_path: Path = SITE) -> int:
    html = html_path.read_text("utf-8")
    match = SITE_DATA.search(html)
    if not match:
        raise Error("Site data JSON missing")
    state = json.loads(match.group(2))
    by_key = {(re.sub(r"\s+", "", x.get("number", "")).lower(), x.get("grade")): x for x in state["cards"]}
    seen = set()
    changed = 0
    updated_dates = []
    for row in source_rows:
        key = (re.sub(r"\s+", "", row["number"]).lower(), row["grade"])
        if key in seen:
            raise Error("Duplicate card in upstream feed")
        seen.add(key)
        c = by_key.get(key)
        if not c:
            print(f"SKIP unknown card (exact number/grade required): {row['number']}")
            continue
        keys = ("priceNow", "pricePast", "asOfDate", "source", "sourceUrl")
        old = tuple(c.get(k) for k in keys)
        new = tuple(row[k] for k in keys)
        if row["asOfDate"] < str(c.get("asOfDate", "")):
            print(f"SKIP older data: {c['name']}")
            continue
        if old != new:
            for k in keys:
                c[k] = row[k]
            c.pop("weeklyTrades", None)  # Weekly editorial report counts aren't daily.
            c.pop("period", None)
            changed += 1
        updated_dates.append(row["asOfDate"])
    if not changed:
        print("No fresher, changed prices to publish. Keeping current website unchanged.")
        return 0
    newest = max(updated_dates)
    as_of = datetime.strptime(newest, "%Y-%m-%d").date()
    start = (as_of - timedelta(days=6)).strftime("%Y/%m/%d")
    state["marketInfo"] = {
        "week": f"{start}〜{as_of.strftime('%Y/%m/%d')}",
        "snapshot": f"{newest} / source values checked; up to 3-day delay is possible",
        "method": "出典元のPSA10直近7日成約中央値と週間変動額による前週中央値の比較。",
        "weeklyUrl": "https://pokecalook.com/articles/weekly-rising.html",
        "lastSuccessfulSync": today.strftime("%Y-%m-%d %H:%M JST"),
        "updatedCards": changed,
        "sourceDate": newest,
    }
    payload = json.dumps(state, ensure_ascii=False, indent=2).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = html[:match.start(2)] + "\n" + payload + "\n" + html[match.end(2):]
    html_path.write_text(html, "utf-8")
    print(f"Updated {changed} price records. Last source date {newest}")
    return changed

def main() -> int:
    mode = os.getenv("DATA_SOURCE_MODE", "off").strip()
    today = datetime.now(TOKYO)
    if mode in ("", "off"):
        print("Daily automation installed; price updates are OFF until an authorized data source is configured.")
        return 0
    watch = json.loads(WATCHLIST_FILE.read_text("utf-8"))
    if mode == "approved-pokecalook":
        results = fetch_approved_pokecalook(watch, today)
    elif mode == "authorized-csv":
        results = fetch_authorized_csv(os.getenv("AUTHORIZED_FEED_URL", ""), today)
    else:
        raise Error("Unknown DATA_SOURCE_MODE")
    update_html(results, today)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"SOURCE NOT UPDATED: {exc}", file=sys.stderr)
        sys.exit(1)
