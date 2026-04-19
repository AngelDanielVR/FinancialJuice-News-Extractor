"""
financialjuice_extractor.py

Extractor robusto de FinancialJuice usando Playwright.

Características principales:
- Soporta login manual, credenciales directas o perfil persistente del navegador.
- Espera a que el feed renderice texto real y no depende de un selector único.
- Hace scroll hasta que el feed deja de crecer.
- Extrae bloques candidatos mediante heurísticas basadas en timestamp.
- Exporta resultados estructurados en JSONL / JSON / Markdown.
- Incluye una API reutilizable para que main.py pueda lanzar la extracción de forma programada.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

BASE_URL = "https://www.financialjuice.com/home"

TIME_RE = re.compile(r"\b(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\b")
URL_RE = re.compile(r"https?://\S+")
WHITESPACE_RE = re.compile(r"[ \t]+")
MULTI_NL_RE = re.compile(r"\n{3,}")

BAD_BLOCK_PATTERNS = [
    re.compile(r"\bjoin us\b", re.I),
    re.compile(r"\bgo real[- ]?time\b", re.I),
    re.compile(r"\bdon't like ads\b", re.I),
    re.compile(r"\bgo pro\b", re.I),
    re.compile(r"\bdiscord\b", re.I),
    re.compile(r"\btrack all markets on tradingview\b", re.I),
    re.compile(r"\bvoice news\b", re.I),
    re.compile(r"\bneed to know market risk\b", re.I),
]

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass
class NewsItem:
    headline: str
    body: Optional[str]
    timestamp_text: str
    timestamp_iso: Optional[str]
    tags: List[str]
    urls: List[str]
    source_block_tag: Optional[str] = None
    source_block_class: Optional[str] = None
    raw_text: Optional[str] = None
    age_from_latest_minutes: Optional[int] = None


@dataclass
class ExtractionResult:
    run_label: str
    out_dir: str
    jsonl_path: str
    json_path: str
    markdown_path: str
    summary_path: str
    exported_items: int
    latest_timestamp_iso: Optional[str]
    earliest_timestamp_iso: Optional[str]
    items: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_label": self.run_label,
            "out_dir": self.out_dir,
            "jsonl_path": self.jsonl_path,
            "json_path": self.json_path,
            "markdown_path": self.markdown_path,
            "summary_path": self.summary_path,
            "exported_items": self.exported_items,
            "latest_timestamp_iso": self.latest_timestamp_iso,
            "earliest_timestamp_iso": self.earliest_timestamp_iso,
            "items": self.items,
        }


@dataclass
class ExtractorConfig:
    repository_dir: str | Path
    hours: int = 6
    headed: bool = False
    manual_login: bool = False
    user_data_dir: Optional[str] = None
    wait_ms: int = 45000
    max_scroll_rounds: int = 40
    scroll_pause_ms: int = 1200
    debug: bool = False
    email: Optional[str] = None
    password: Optional[str] = None
    run_label: Optional[str] = None


def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [WHITESPACE_RE.sub(" ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def text_looks_like_noise(text: str) -> bool:
    if len(text) < 20:
        return True
    return any(rx.search(text) for rx in BAD_BLOCK_PATTERNS)


def parse_timestamp_text(ts_text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now()
    m = TIME_RE.search(ts_text)
    if not m:
        return None

    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    mon = MONTHS.get(m.group("mon"))
    day = int(m.group("day"))

    if mon is None:
        return None

    candidates: List[datetime] = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, mon, day, hour, minute))
        except ValueError:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda dt: abs((dt - now).total_seconds()))
    chosen = candidates[0]

    if chosen - now > timedelta(days=2):
        for cand in candidates:
            if cand <= now + timedelta(days=2):
                chosen = cand
                break

    return chosen


def split_headline_body(before_ts: str) -> Tuple[Optional[str], Optional[str]]:
    lines = [ln.strip() for ln in before_ts.split("\n") if ln.strip()]
    if not lines:
        return None, None

    headline = lines[0]
    body = "\n".join(lines[1:]).strip() or None
    return headline, body


def parse_tags(after_ts: str) -> List[str]:
    after_ts = after_ts.strip()
    if not after_ts:
        return []

    tokens = [tok.strip(" ,|") for tok in after_ts.split() if tok.strip(" ,|")]
    if not tokens:
        return []

    if len(tokens) > 16:
        return []

    clean: List[str] = []
    for tok in tokens:
        if len(tok) > 30:
            continue
        if URL_RE.search(tok):
            continue
        clean.append(tok)

    return clean


def parse_block_text(block_text: str, now: Optional[datetime] = None) -> Optional[NewsItem]:
    text = normalize_text(block_text)
    if text_looks_like_noise(text):
        return None

    m = TIME_RE.search(text)
    if not m:
        return None

    ts_text = m.group(0)
    before = text[:m.start()].strip()
    after = text[m.end():].strip()

    if not before:
        return None

    headline, body = split_headline_body(before)
    if not headline:
        return None

    if text_looks_like_noise(headline):
        return None

    dt = parse_timestamp_text(ts_text, now=now)
    urls = URL_RE.findall(text)
    tags = parse_tags(after)

    return NewsItem(
        headline=headline,
        body=body,
        timestamp_text=ts_text,
        timestamp_iso=dt.isoformat() if dt else None,
        tags=tags,
        urls=urls,
        raw_text=text,
    )


def dedupe_items(items: List[NewsItem]) -> List[NewsItem]:
    seen = set()
    deduped: List[NewsItem] = []

    for item in items:
        key = (
            item.headline.strip().lower(),
            item.timestamp_text.strip().lower(),
            (item.body or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def sort_items_desc(items: List[NewsItem]) -> List[NewsItem]:
    def key(item: NewsItem) -> Tuple[int, str]:
        if item.timestamp_iso:
            return (1, item.timestamp_iso)
        return (0, item.timestamp_text)

    return sorted(items, key=key, reverse=True)


def filter_items_by_hours_from_latest(items: List[NewsItem], hours: int) -> List[NewsItem]:
    if not items:
        return items

    items = sort_items_desc(items)
    valid = [x for x in items if x.timestamp_iso]
    if not valid:
        return items

    latest = datetime.fromisoformat(valid[0].timestamp_iso)
    cutoff = latest - timedelta(hours=hours)

    filtered: List[NewsItem] = []
    for item in items:
        if not item.timestamp_iso:
            continue
        dt = datetime.fromisoformat(item.timestamp_iso)
        if dt >= cutoff:
            item.age_from_latest_minutes = int((latest - dt).total_seconds() // 60)
            filtered.append(item)

    return filtered


def build_markdown(items: List[NewsItem], hours: int) -> str:
    lines: List[str] = []
    lines.append(f"# FinancialJuice News Export ({hours}h window)")
    lines.append("")
    lines.append(f"Total items: **{len(items)}**")
    lines.append("")

    for idx, item in enumerate(items, start=1):
        lines.append(f"## {idx}. {item.headline}")
        lines.append("")
        lines.append(f"- Timestamp: `{item.timestamp_text}`")
        if item.timestamp_iso:
            lines.append(f"- ISO time: `{item.timestamp_iso}`")
        if item.age_from_latest_minutes is not None:
            lines.append(f"- Age from latest: `{item.age_from_latest_minutes} min`")
        if item.tags:
            lines.append(f"- Tags: {', '.join(item.tags)}")
        if item.urls:
            lines.append(f"- URLs: {', '.join(item.urls)}")
        lines.append("")
        if item.body:
            lines.append(item.body)
            lines.append("")
        lines.append("```text")
        lines.append(item.raw_text or "")
        lines.append("```")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def write_outputs(items: List[NewsItem], out_dir: Path, hours: int) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = out_dir / "news.jsonl"
    json_path = out_dir / "news.json"
    md_path = out_dir / "news.md"
    summary_path = out_dir / "summary.json"

    with jsonl_path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    with json_path.open("w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in items], f, ensure_ascii=False, indent=2)

    md_path.write_text(build_markdown(items, hours=hours), encoding="utf-8")

    latest_iso = items[0].timestamp_iso if items else None
    earliest_iso = items[-1].timestamp_iso if items else None

    summary = {
        "exported_items": len(items),
        "window_hours": hours,
        "latest_timestamp_iso": latest_iso,
        "earliest_timestamp_iso": earliest_iso,
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "files": {
            "jsonl": str(jsonl_path),
            "json": str(json_path),
            "markdown": str(md_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"[ok] wrote {jsonl_path}")
    log(f"[ok] wrote {json_path}")
    log(f"[ok] wrote {md_path}")
    log(f"[ok] wrote {summary_path}")

    return {
        "jsonl": jsonl_path,
        "json": json_path,
        "markdown": md_path,
        "summary": summary_path,
    }


async def screenshot_debug(page: Page, out_dir: Path, name: str) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        log(f"[debug] screenshot: {path}")
    except Exception as exc:
        log(f"[debug] screenshot failed: {exc}")


def selector_to_filename_part(selector: str) -> str:
    return (
        selector.replace("#", "id_")
        .replace(".", "class_")
        .replace("*", "all")
        .replace("/", "_")
        .replace(" ", "_")
        .replace("[", "_")
        .replace("]", "_")
        .replace("'", "")
        .replace('"', "")
        .replace("=", "_")
    )


async def dump_mainfeed_debug(page: Page, out_dir: Path) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        selectors = ["#mainFeed", ".infinite-container", ".waypoint", "main", "body"]
        debug_report = []

        for sel in selectors:
            try:
                loc = page.locator(sel).first
                count = await loc.count()
                if count == 0:
                    debug_report.append(f"=== SELECTOR: {sel} ===\nNOT FOUND\n\n")
                    continue

                txt = await loc.inner_text(timeout=1500)
                html = await loc.evaluate("el => el.outerHTML")

                txt = normalize_text(txt)
                base = selector_to_filename_part(sel)
                (out_dir / f"debug_{base}.txt").write_text(txt, encoding="utf-8")
                (out_dir / f"debug_{base}.html").write_text(html, encoding="utf-8")
                debug_report.append(
                    f"=== SELECTOR: {sel} ===\ntext_len={len(txt)}\npreview:\n{txt[:2000]}\n\n"
                )
            except Exception as exc:
                debug_report.append(f"=== SELECTOR: {sel} ===\nERROR: {exc}\n\n")

        (out_dir / "debug_selector_report.txt").write_text("".join(debug_report), encoding="utf-8")
        log(f"[debug] wrote {out_dir / 'debug_selector_report.txt'}")
    except Exception as exc:
        log(f"[debug] feed dump failed: {exc}")


async def create_context(pw, headed: bool, user_data_dir: Optional[str]) -> Tuple[BrowserContext, Optional[Any]]:
    if user_data_dir:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=not headed,
            viewport={"width": 1440, "height": 1100},
        )
        return context, None

    browser = await pw.chromium.launch(headless=not headed)
    context = await browser.new_context(viewport={"width": 1440, "height": 1100})
    return context, browser


async def dismiss_cookie_or_modal_noise(page: Page) -> None:
    selectors = [
        "button:has-text('Accept')",
        "button:has-text('I Agree')",
        "button:has-text('Got it')",
        ".modal .close",
        ".btn-close",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=600):
                await loc.click(timeout=600)
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def maybe_login(page: Page, email: Optional[str], password: Optional[str], manual_login: bool) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    await dismiss_cookie_or_modal_noise(page)

    if manual_login:
        log("[info] manual login mode enabled.")
        log("[info] log in in the opened browser, then press Enter here to continue...")
        await asyncio.to_thread(input)
        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        return

    try:
        hello = page.locator("text=Hello,").first
        if await hello.is_visible(timeout=1500):
            log("[info] already logged in.")
            return
    except Exception:
        pass

    if not email or not password:
        log("[warn] No FinancialJuice credentials provided. Proceeding without scripted login.")
        return

    tried_modal = False
    candidate_buttons = ["text=Login", "text=Sign In", "#liSignIn", "a[href*='login']"]
    for sel in candidate_buttons:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.click(timeout=1500)
                tried_modal = True
                await page.wait_for_timeout(1200)
                break
        except Exception:
            pass

    if tried_modal:
        try:
            sign_in_tab = page.locator("#liSignIn").first
            if await sign_in_tab.is_visible(timeout=1000):
                await sign_in_tab.click(timeout=1500)
                await page.wait_for_timeout(800)
        except Exception:
            pass

    email_selectors = ["#ctl00_SignInSignUp_loginForm1_inputEmail", "input[type='email']"]
    pass_selectors = ["#ctl00_SignInSignUp_loginForm1_inputPassword", "input[type='password']"]
    submit_selectors = [
        "#ctl00_SignInSignUp_loginForm1_btnLogin",
        "input[type='submit'][value='Login']",
        "button:has-text('Login')",
    ]

    email_filled = False
    for sel in email_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.fill(email, timeout=3000)
                email_filled = True
                break
        except Exception:
            pass

    pass_filled = False
    for sel in pass_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.fill(password, timeout=3000)
                pass_filled = True
                break
        except Exception:
            pass

    if email_filled and pass_filled:
        submitted = False
        for sel in submit_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1200):
                    await loc.click(timeout=3000)
                    submitted = True
                    break
            except Exception:
                pass

        if submitted:
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)
    await dismiss_cookie_or_modal_noise(page)

    try:
        hello = page.locator("text=Hello,").first
        if await hello.is_visible(timeout=2000):
            log("[info] login appears successful.")
        else:
            log("[warn] login could not be confirmed. Continuing anyway.")
    except Exception:
        log("[warn] login could not be confirmed. Continuing anyway.")


async def wait_for_mainfeed_text(page: Page, timeout_ms: int = 45000) -> Tuple[str, str]:
    candidate_selectors = [
        "#mainFeed",
        "#mainFeed *",
        "div[id*='Feed']",
        "div[id*='feed']",
        ".infinite-container",
        ".waypoint",
        "main",
        "body",
    ]

    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(2500)

    deadline = max(1, timeout_ms // 1000)
    best_selector: Optional[str] = None
    best_text = ""

    for _ in range(deadline):
        for sel in candidate_selectors:
            try:
                loc = page.locator(sel).first
                count = await loc.count()
                if count == 0:
                    continue

                txt = await loc.inner_text(timeout=1000)
                txt = normalize_text(txt)

                if len(txt) > len(best_text):
                    best_text = txt
                    best_selector = sel

                if len(txt) > 300 and TIME_RE.search(txt):
                    return sel, txt
            except Exception:
                pass

        await page.wait_for_timeout(1000)

    raise RuntimeError(
        f"Timed out waiting for rendered feed text. Best selector={best_selector!r}, best_text_len={len(best_text)}"
    )


async def scroll_until_stable(page: Page, root_selector: str = "#mainFeed", max_rounds: int = 40, pause_ms: int = 1200) -> None:
    previous_len = -1
    stable_rounds = 0

    for i in range(max_rounds):
        try:
            text = normalize_text(await page.locator(root_selector).first.inner_text())
        except Exception:
            text = normalize_text(await page.locator("body").inner_text())

        current_len = len(text)
        log(f"[scroll {i + 1}] {root_selector} text length = {current_len}")

        if current_len == previous_len:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 3:
            log("Feed stopped growing; ending early.")
            break

        previous_len = current_len
        await page.mouse.wheel(0, 6000)
        await page.wait_for_timeout(pause_ms)


async def extract_candidate_blocks(page: Page, root_selector: str = "#mainFeed") -> List[Dict[str, Any]]:
    js = r"""
    (rootSelector) => {
        const roots = [];
        const preferred = document.querySelector(rootSelector);
        if (preferred) roots.push(preferred);

        for (const sel of [".infinite-container", ".waypoint", "main", "body"]) {
            const el = document.querySelector(sel);
            if (el && !roots.includes(el)) roots.push(el);
        }

        const badPatterns = [
            /\bjoin us\b/i,
            /\bgo real[- ]?time\b/i,
            /\bdon't like ads\b/i,
            /\bgo pro\b/i,
            /\bdiscord\b/i,
            /\btrack all markets on tradingview\b/i,
            /\bvoice news\b/i,
            /\bneed to know market risk\b/i
        ];

        const timeRe = /\b\d{1,2}:\d{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\b/;
        const blocks = [];
        const seen = new Set();

        for (const root of roots) {
            const nodes = Array.from(root.querySelectorAll("div, li, article, p, span, a"));
            for (const el of nodes) {
                let txt = (el.innerText || "").trim();
                if (!txt) continue;
                if (txt.length < 20) continue;
                if (!timeRe.test(txt)) continue;
                if (badPatterns.some(rx => rx.test(txt))) continue;
                txt = txt.replace(/\n{3,}/g, "\n\n");
                if (seen.has(txt)) continue;
                seen.add(txt);
                blocks.push({
                    rootTag: root.tagName,
                    rootId: root.id || "",
                    rootClass: root.className || "",
                    tag: el.tagName,
                    className: el.className || "",
                    text: txt,
                    html: el.outerHTML
                });
            }
        }

        return blocks;
    }
    """
    result = await page.evaluate(js, root_selector)
    return list(result)


async def extract_news_items(page: Page, root_selector: str = "#mainFeed") -> List[NewsItem]:
    raw_blocks = await extract_candidate_blocks(page, root_selector=root_selector)
    log(f"[info] raw candidate blocks found: {len(raw_blocks)}")

    now = datetime.now()
    items: List[NewsItem] = []

    for block in raw_blocks:
        parsed = parse_block_text(block.get("text", ""), now=now)
        if not parsed:
            continue
        parsed.source_block_tag = block.get("tag")
        parsed.source_block_class = block.get("className")
        items.append(parsed)

    items = dedupe_items(items)
    items = sort_items_desc(items)
    log(f"[info] parsed items: {len(items)}")
    return items


async def run_extraction(
    args: argparse.Namespace,
    email: Optional[str] = None,
    password: Optional[str] = None,
    run_label: Optional[str] = None,
) -> ExtractionResult:
    out_dir = Path(args.out_dir)
    debug_dir = out_dir / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    run_label = run_label or out_dir.name

    async with async_playwright() as pw:
        context, browser = await create_context(
            pw,
            headed=args.headed,
            user_data_dir=args.user_data_dir,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            await maybe_login(page, email=email, password=password, manual_login=args.manual_login)

            log(f"[info] current URL: {page.url}")
            if args.debug:
                await screenshot_debug(page, debug_dir, "after_login")

            try:
                root_selector, preview = await wait_for_mainfeed_text(page, timeout_ms=args.wait_ms)
                log(f"[info] feed selector used: {root_selector}")
                log("[info] feed text preview:")
                log(preview[:2000])
            except (PlaywrightTimeoutError, RuntimeError):
                await screenshot_debug(page, debug_dir, "mainfeed_timeout")
                await dump_mainfeed_debug(page, debug_dir)
                raise RuntimeError(
                    "Timed out waiting for rendered feed text. "
                    "Run with --headed --debug and inspect debug_selector_report.txt plus the debug_*.txt files."
                )

            await scroll_until_stable(
                page,
                root_selector=root_selector,
                max_rounds=args.max_scroll_rounds,
                pause_ms=args.scroll_pause_ms,
            )

            if args.debug:
                await screenshot_debug(page, debug_dir, "after_scroll")
                await dump_mainfeed_debug(page, debug_dir)

            items = await extract_news_items(page, root_selector=root_selector)

            if not items:
                raise RuntimeError(
                    "No news items extracted from rendered feed text. "
                    "Run with --headed --debug and inspect debug_selector_report.txt plus the debug_*.txt files."
                )

            filtered = filter_items_by_hours_from_latest(items, hours=args.hours)
            if not filtered:
                raise RuntimeError(
                    f"Extracted {len(items)} items, but none remained after filtering to {args.hours} hours."
                )

            artifacts = write_outputs(filtered, out_dir=out_dir, hours=args.hours)

            log("")
            log("[done] extraction complete")
            log(f"[done] total parsed items: {len(items)}")
            log(f"[done] items in {args.hours}h window: {len(filtered)}")

            latest_iso = filtered[0].timestamp_iso if filtered else None
            earliest_iso = filtered[-1].timestamp_iso if filtered else None

            return ExtractionResult(
                run_label=run_label,
                out_dir=str(out_dir),
                jsonl_path=str(artifacts["jsonl"]),
                json_path=str(artifacts["json"]),
                markdown_path=str(artifacts["markdown"]),
                summary_path=str(artifacts["summary"]),
                exported_items=len(filtered),
                latest_timestamp_iso=latest_iso,
                earliest_timestamp_iso=earliest_iso,
                items=[asdict(item) for item in filtered],
            )

        finally:
            await context.close()
            if browser is not None:
                await browser.close()


def load_jsonl_items(jsonl_path: str | Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    path = Path(jsonl_path)
    if not path.exists():
        return items

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def build_runtime_args(out_dir: str | Path, hours: int, headed: bool, manual_login: bool, user_data_dir: Optional[str], wait_ms: int, max_scroll_rounds: int, scroll_pause_ms: int, debug: bool) -> argparse.Namespace:
    return argparse.Namespace(
        hours=hours,
        out_dir=str(out_dir),
        headed=headed,
        manual_login=manual_login,
        user_data_dir=user_data_dir,
        wait_ms=wait_ms,
        max_scroll_rounds=max_scroll_rounds,
        scroll_pause_ms=scroll_pause_ms,
        debug=debug,
    )


def extract_to_repository(config: ExtractorConfig) -> ExtractionResult:
    repository_dir = Path(config.repository_dir)
    run_label = config.run_label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = repository_dir / "financialjuice" / run_label
    out_dir.mkdir(parents=True, exist_ok=True)

    args = build_runtime_args(
        out_dir=out_dir,
        hours=config.hours,
        headed=config.headed,
        manual_login=config.manual_login,
        user_data_dir=config.user_data_dir,
        wait_ms=config.wait_ms,
        max_scroll_rounds=config.max_scroll_rounds,
        scroll_pause_ms=config.scroll_pause_ms,
        debug=config.debug,
    )

    return asyncio.run(
        run_extraction(
            args,
            email=config.email,
            password=config.password,
            run_label=run_label,
        )
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract FinancialJuice news feed data into AI-ready structured files."
    )
    parser.add_argument("--hours", type=int, default=24, help="Hours back from the latest extracted item.")
    parser.add_argument("--out-dir", default="out", help="Output directory.")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser.")
    parser.add_argument("--manual-login", action="store_true", help="Pause for manual login in browser.")
    parser.add_argument(
        "--user-data-dir",
        default=None,
        help="Persistent browser profile directory. Useful for reusing an authenticated session.",
    )
    parser.add_argument("--email", default=None, help="FinancialJuice email (optional).")
    parser.add_argument("--password", default=None, help="FinancialJuice password (optional).")
    parser.add_argument("--wait-ms", type=int, default=45000, help="Timeout waiting for rendered feed text.")
    parser.add_argument("--max-scroll-rounds", type=int, default=40, help="Maximum scroll rounds.")
    parser.add_argument("--scroll-pause-ms", type=int, default=1200, help="Pause between scrolls.")
    parser.add_argument("--debug", action="store_true", help="Write screenshots and feed dumps for debugging.")
    return parser.parse_args(argv)


def cli_main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(
            run_extraction(
                args,
                email=args.email,
                password=args.password,
            )
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        log("\n[abort] interrupted by user")
        return 130
    except Exception as exc:
        log(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
