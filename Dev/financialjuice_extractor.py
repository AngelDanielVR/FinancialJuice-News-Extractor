#!/usr/bin/env python3
"""
FinancialJuice news extractor.

Goal:
- Log into FinancialJuice (optionally via env vars or existing browser session)
- Load the main news feed
- Scroll backwards until at least N hours of history are collected
- Extract each visible news item into structured JSON
- Emit outputs that are convenient to attach to another AI:
  * news.jsonl  -> one JSON object per line
  * news.json   -> full array
  * news.md     -> LLM-friendly markdown bundle
  * summary.json -> metadata about the run

Notes:
- This script intentionally scrapes the rendered DOM instead of hard-coding private API
  endpoints. The provided HTML indicates the page hydrates #mainFeed dynamically and the
  PDF shows the delayed feed entries rendered in-page, so DOM extraction is the most
  resilient approach.
- Default behavior targets 24h of history from the newest collected item.

Install:
    pip install playwright python-dateutil
    playwright install chromium

Usage examples:
    python financialjuice_extractor.py \
        --email "$FJ_EMAIL" --password "$FJ_PASSWORD" \
        --hours 24 --out-dir out

    # Reuse an already logged-in profile for MFA / captcha resistant runs
    python financialjuice_extractor.py \
        --user-data-dir /path/to/chrome-profile \
        --profile-directory Default \
        --hours 24 --headed

Environment variables:
    FJ_EMAIL, FJ_PASSWORD
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from dateutil import parser as dtparser
from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeoutError, sync_playwright

BASE_URL = "https://www.financialjuice.com/home"
TIMESTAMP_RE = re.compile(r"^(?P<hour>\d{1,2}):(\d{2})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})$")
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
JUNK_PATTERNS = [
    re.compile(r"^Join us and Go Real-time", re.I),
    re.compile(r"^Don't like Ads\? GO PRO", re.I),
    re.compile(r"^THIS FEED IS DELAYED", re.I),
    re.compile(r"^GO REAL-TIME$", re.I),
]
TAG_TOKENS = {
    "Energy", "US", "Bonds", "Indexes", "USD", "Macro", "Forex", "Crypto",
    "Equities", "Commodities", "Market", "Moving", "Elite", "Risk"
}


@dataclass
class NewsItem:
    source_order: int
    headline: str
    body: Optional[str]
    timestamp_raw: str
    timestamp_iso: str
    age_hours_from_latest: float
    tags: List[str]
    url: Optional[str]
    feed: str = "mainfeed"


JS_EXTRACT = r"""
() => {
  const root = document.querySelector('#mainFeed');
  if (!root) {
    return {error: 'mainFeed not found'};
  }

  function isVisible(el) {
    const s = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s && s.display !== 'none' && s.visibility !== 'hidden' && r.height > 0 && r.width > 0;
  }

  function textOf(el) {
    return (el?.innerText || el?.textContent || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function absUrl(href) {
    try { return href ? new URL(href, location.href).href : null; } catch (e) { return href || null; }
  }

  const all = Array.from(root.querySelectorAll('*')).filter(isVisible);
  const candidates = [];

  for (const el of all) {
    const text = textOf(el);
    if (!text || text.length < 20 || text.length > 1500) continue;

    const lines = text.split(/\n+/).map(x => x.trim()).filter(Boolean);
    if (lines.length === 0) continue;

    const hasTime = lines.some(line => /^\d{1,2}:\d{2}\s+[A-Za-z]{3}\s+\d{1,2}$/.test(line));
    if (!hasTime) continue;

    const anchors = Array.from(el.querySelectorAll('a[href]')).map(a => absUrl(a.getAttribute('href'))).filter(Boolean);
    candidates.push({
      html_len: el.outerHTML.length,
      text,
      lines,
      urls: [...new Set(anchors)],
      tag: el.tagName,
      cls: el.className || ''
    });
  }

  candidates.sort((a, b) => a.html_len - b.html_len);

  const unique = [];
  const seen = new Set();
  for (const c of candidates) {
    const key = c.text;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(c);
  }

  return {items: unique};
}
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract FinancialJuice news items from the rendered feed")
    p.add_argument("--email", default=os.getenv("FJ_EMAIL"))
    p.add_argument("--password", default=os.getenv("FJ_PASSWORD"))
    p.add_argument("--hours", type=float, default=24.0, help="minimum history depth from newest item")
    p.add_argument("--out-dir", default="financialjuice_output")
    p.add_argument("--headed", action="store_true", help="show browser window")
    p.add_argument("--timeout-ms", type=int, default=30000)
    p.add_argument("--max-scrolls", type=int, default=250)
    p.add_argument("--scroll-pause", type=float, default=1.0)
    p.add_argument("--user-data-dir", help="reuse an existing Chrome/Chromium profile")
    p.add_argument("--profile-directory", default=None, help="Chrome profile directory name, e.g. Default")
    p.add_argument("--manual-login", action="store_true", help="pause for manual login instead of submitting credentials")
    return p.parse_args()


def create_context(playwright, args: argparse.Namespace) -> BrowserContext:
    if args.user_data_dir:
        launch_args = {
            "headless": not args.headed,
            "channel": "chrome" if sys.platform != "linux" else None,
        }
        if args.profile_directory:
            launch_args["args"] = [f"--profile-directory={args.profile_directory}"]
        launch_args = {k: v for k, v in launch_args.items() if v is not None}
        return playwright.chromium.launch_persistent_context(args.user_data_dir, **launch_args)

    browser = playwright.chromium.launch(headless=not args.headed)
    return browser.new_context()


def maybe_login(page: Page, args: argparse.Namespace) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    if page.locator("text=Hello,").count() > 0:
        return

    if args.manual_login:
        print("Please log in manually in the opened browser, then press Enter here...", flush=True)
        input()
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        return

    if not args.email or not args.password:
        raise RuntimeError(
            "Not logged in and no credentials supplied. Use --manual-login, --user-data-dir, or set FJ_EMAIL/FJ_PASSWORD."
        )

    # Open the sign-in modal if present.
    for selector in ["text=Login", "text=Sign In", "#signup", "#LoginTab"]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.click(timeout=2000)
                page.wait_for_timeout(1000)
                break
        except Exception:
            pass

    page.locator("#ctl00_SignInSignUp_loginForm1_inputEmail").fill(args.email)
    page.locator("#ctl00_SignInSignUp_loginForm1_inputPassword").fill(args.password)
    page.locator("#ctl00_SignInSignUp_loginForm1_btnLogin").click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(4000)

    if page.locator("text=Hello,").count() == 0:
        raise RuntimeError("Login did not appear to succeed. Try --headed --manual-login or a persistent profile.")


def wait_for_feed(page: Page, timeout_ms: int) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.wait_for_function(
        """
        () => {
          const root = document.querySelector('#mainFeed');
          return !!root;
        }
        """,
        timeout=timeout_ms,
    )
    # Give the dynamic feed a moment to populate.
    page.wait_for_timeout(5000)


def clean_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    t = re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()
    if not t:
        return None
    for pat in JUNK_PATTERNS:
        if pat.search(t):
            return None
    return t


def parse_timestamp(raw: str, now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now()
    m = TIMESTAMP_RE.match(raw.strip())
    if not m:
        return dtparser.parse(raw)
    hour_min = raw.split()[0]
    hour, minute = map(int, hour_min.split(":"))
    mon = MONTHS[m.group("mon")]
    day = int(m.group("day"))
    year = now.year
    dt = datetime(year, mon, day, hour, minute)
    # Handle year rollover near January.
    if dt > now + timedelta(days=2):
        dt = dt.replace(year=year - 1)
    return dt


def looks_like_tag_line(line: str) -> bool:
    toks = line.split()
    if not toks or len(toks) > 8:
        return False
    if sum(1 for tok in toks if tok in TAG_TOKENS or tok.isupper()) >= max(1, len(toks) - 1):
        return True
    return False


def candidate_to_item(c: dict, order_idx: int, latest_dt: Optional[datetime]) -> Optional[NewsItem]:
    lines = [clean_text(x) for x in c["lines"]]
    lines = [x for x in lines if x]
    if not lines:
        return None

    ts_idx = None
    for i, line in enumerate(lines):
        if TIMESTAMP_RE.match(line):
            ts_idx = i
            break
    if ts_idx is None:
        return None

    headline_lines = [ln for ln in lines[:ts_idx] if not looks_like_tag_line(ln)]
    if not headline_lines:
        return None
    headline = clean_text(" ".join(headline_lines))
    if not headline:
        return None

    after = [ln for ln in lines[ts_idx + 1:] if ln]
    tags = []
    body_lines = []
    for ln in after:
        if looks_like_tag_line(ln):
            tags.extend(ln.split())
        else:
            body_lines.append(ln)

    # Remove headline duplicated as first body line.
    if body_lines and body_lines[0] == headline:
        body_lines = body_lines[1:]

    body = clean_text(" ".join(body_lines))
    url = None
    for u in c.get("urls", []):
        if "/News/" in u or u.endswith(".aspx"):
            url = u
            break

    ts_raw = lines[ts_idx]
    dt = parse_timestamp(ts_raw)
    if latest_dt is None:
        age_hours = 0.0
    else:
        age_hours = round((latest_dt - dt).total_seconds() / 3600.0, 3)

    return NewsItem(
        source_order=order_idx,
        headline=headline,
        body=body,
        timestamp_raw=ts_raw,
        timestamp_iso=dt.isoformat(),
        age_hours_from_latest=age_hours,
        tags=sorted(set(tags)),
        url=url,
    )


def extract_candidates(page: Page) -> List[dict]:
    res = page.evaluate(JS_EXTRACT)
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(res["error"])
    return res["items"]


def normalize_items(candidates: List[dict]) -> List[NewsItem]:
    # First pass to get timestamps.
    temp = []
    for i, c in enumerate(candidates):
        item = candidate_to_item(c, i, None)
        if item:
            temp.append(item)
    if not temp:
        return []

    latest_dt = max(datetime.fromisoformat(x.timestamp_iso) for x in temp)
    items = []
    seen = set()
    for i, c in enumerate(candidates):
        item = candidate_to_item(c, i, latest_dt)
        if not item:
            continue
        key = (item.timestamp_raw, item.headline, item.body or "")
        if key in seen:
            continue
        seen.add(key)
        items.append(item)

    items.sort(key=lambda x: x.timestamp_iso, reverse=True)
    return items


def reached_target(items: List[NewsItem], hours: float) -> bool:
    if not items:
        return False
    ages = [it.age_hours_from_latest for it in items]
    return max(ages) >= hours


def scroll_until_depth(page: Page, args: argparse.Namespace) -> List[NewsItem]:
    best_items: List[NewsItem] = []
    stable_rounds = 0

    for i in range(args.max_scrolls):
        candidates = extract_candidates(page)
        items = normalize_items(candidates)
        if len(items) > len(best_items):
            best_items = items
            stable_rounds = 0
        else:
            stable_rounds += 1

        if reached_target(best_items, args.hours):
            print(f"Reached target depth after {i + 1} scrolls with {len(best_items)} items.")
            break

        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(int(args.scroll_pause * 1000))

        # Some infinite scroll implementations need explicit bottom jump.
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(int(args.scroll_pause * 1000))

        if stable_rounds >= 12:
            print("Feed stopped growing; ending early.")
            break

    return best_items


def write_outputs(items: List[NewsItem], out_dir: Path, hours: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    json_items = [asdict(x) for x in items]
    (out_dir / "news.json").write_text(json.dumps(json_items, ensure_ascii=False, indent=2), encoding="utf-8")

    with (out_dir / "news.jsonl").open("w", encoding="utf-8") as f:
        for row in json_items:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    md_lines = [
        "# FinancialJuice extracted news",
        "",
        f"- extracted_items: {len(items)}",
        f"- requested_history_hours: {hours}",
    ]
    if items:
        md_lines.extend([
            f"- newest_item: {items[0].timestamp_iso}",
            f"- oldest_item: {items[-1].timestamp_iso}",
            "",
            "## News items",
            "",
        ])
    for idx, item in enumerate(items, 1):
        md_lines.append(f"### {idx}. {item.headline}")
        md_lines.append(f"- timestamp_raw: {item.timestamp_raw}")
        md_lines.append(f"- timestamp_iso: {item.timestamp_iso}")
        md_lines.append(f"- age_hours_from_latest: {item.age_hours_from_latest}")
        md_lines.append(f"- tags: {', '.join(item.tags) if item.tags else '(none)'}")
        md_lines.append(f"- url: {item.url or '(none)'}")
        md_lines.append(f"- body: {item.body or '(none)'}")
        md_lines.append("")
    (out_dir / "news.md").write_text("\n".join(md_lines), encoding="utf-8")

    summary = {
        "count": len(items),
        "requested_history_hours": hours,
        "newest_item": items[0].timestamp_iso if items else None,
        "oldest_item": items[-1].timestamp_iso if items else None,
        "actual_history_hours": max((x.age_hours_from_latest for x in items), default=0),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)

    with sync_playwright() as pw:
        context = create_context(pw, args)
        try:
            page = context.new_page() if hasattr(context, 'new_page') else context.pages[0]
            maybe_login(page, args)
            wait_for_feed(page, args.timeout_ms)
            items = scroll_until_depth(page, args)
            if not items:
                raise RuntimeError("No news items extracted from #mainFeed. Run with --headed and inspect selectors.")
            write_outputs(items, out_dir, args.hours)
            print(json.dumps({
                "ok": True,
                "count": len(items),
                "out_dir": str(out_dir.resolve()),
                "newest": items[0].timestamp_iso,
                "oldest": items[-1].timestamp_iso,
                "history_hours": max(x.age_hours_from_latest for x in items),
            }, indent=2))
            return 0
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
