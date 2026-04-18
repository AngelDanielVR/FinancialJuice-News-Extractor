
# FinancialJuice News Extractor

A robust Playwright-based FinancialJuice news extractor that captures at least 24 hours of feed data from the latest available headline and exports structured outputs for exhaustive AI-driven analysis.

## Features

- Uses a real browser session with Playwright.
- Supports login via environment variables, manual login, or a persistent browser profile.
- Waits for rendered feed text in likely containers instead of relying only on `#mainFeed`.
- Scrolls dynamically until the feed stops growing.
- Uses timestamp-based parsing instead of brittle CSS-only extraction.
- Filters news to a configurable window measured from the latest extracted item.
- Exports AI-ready structured files.

## 1) Install

```bash
pip install playwright python-dateutil
playwright install chromium
```

## 2) Run

### Option A - login via environment variables

Linux/macOS:

```bash
export FJ_EMAIL='your_email'
export FJ_PASSWORD='your_password'
python financialjuice_extractor.py --hours 24 --out-dir out
```

Windows PowerShell:

```powershell
$env:FJ_EMAIL='your_email'
$env:FJ_PASSWORD='your_password'
python financialjuice_extractor.py --hours 24 --out-dir out
```

### Option B - manual login in a visible browser

```bash
python financialjuice_extractor.py --headed --manual-login --hours 24 --out-dir out
```

### Option C - reuse a persistent browser profile

```bash
python financialjuice_extractor.py --headed --user-data-dir ./user_data --hours 24 --out-dir out
```

## 3) Recommended debug run

If extraction fails or returns no items, run:

```bash
python financialjuice_extractor.py --headed --manual-login --debug --hours 24 --out-dir out
```

This writes additional debug artifacts to:

* `out/debug/debug_selector_report.txt`
* `out/debug/debug_*.txt`
* `out/debug/debug_*.html`
* screenshots of the page state

These files are the first place to inspect if the feed loaded visually but parsing still failed.

## 4) Output files

* `news.jsonl`: best format for attaching to another AI
* `news.json`: array version
* `news.md`: human-readable and LLM-friendly
* `summary.json`: run metadata
* `out/debug/*`: optional debug artifacts when `--debug` is used

## Recommended file for AI analysis

Use `news.jsonl` when you want structured ingestion.

Use `news.md` when you want the model to read the narrative directly.

## Extraction strategy

The extractor is designed for dynamic pages where the feed is injected after page load.

It works by:

1. Opening the FinancialJuice feed in a real browser session
2. Waiting for rendered text in the most likely feed container
3. Scrolling until feed growth stabilizes
4. Finding candidate feed blocks that contain timestamps such as `17:51 Apr 18`
5. Parsing each block into:

   * headline
   * body or detail text if present
   * timestamp
   * ISO timestamp
   * tags
   * URLs
6. Filtering items to the requested time window from the most recent extracted item

## Command-line options

```text
--hours                Hours back from the latest extracted item
--out-dir              Output directory
--headed               Run with visible browser
--manual-login         Pause for manual login in browser
--user-data-dir        Persistent browser profile directory
--wait-ms              Timeout waiting for rendered feed text
--max-scroll-rounds    Maximum scroll rounds
--scroll-pause-ms      Pause between scrolls
--debug                Write screenshots and feed dumps for debugging
```

## Example commands

Basic run:

```bash
python financialjuice_extractor.py --hours 24 --out-dir out
```

Headed manual login with debug:

```bash
python financialjuice_extractor.py --headed --manual-login --debug --hours 24 --out-dir out
```

Longer extraction window:

```bash
python financialjuice_extractor.py --hours 48 --out-dir out
```

Persistent profile:

```bash
python financialjuice_extractor.py --headed --user-data-dir ./user_data --debug --hours 24 --out-dir out
```

## Suggested prompt for the downstream AI

```text
Analyze the attached FinancialJuice news dataset exhaustively.

Tasks:
1. Cluster the news into themes.
2. Build a market narrative timeline.
3. Identify probable first-order and second-order impacts by asset class.
4. Flag contradictions, follow-ups, and story evolution.
5. Separate headline facts from interpretation.
6. Produce a trader-focused summary and a risk-manager-focused summary.
7. Extract all references to macro, rates, FX, equities, commodities, crypto, geopolitics, and shipping/energy.
8. Highlight what changed during the selected time window.

Return:
- Executive summary
- Chronological timeline
- Theme clusters
- Asset-impact matrix
- Open questions
- Key risks
```

## Troubleshooting

### `No news items extracted from rendered feed text`

Run with:

```bash
python financialjuice_extractor.py --headed --manual-login --debug --hours 24 --out-dir out
```

Then inspect:

* `out/debug/debug_selector_report.txt`
* `out/debug/debug_*.txt`
* `out/debug/debug_*.html`

If the debug text files contain real feed text, the issue is in parsing heuristics rather than login.

### `Timed out waiting for rendered feed text`

Possible causes:

* the page did not finish rendering the feed
* login was incomplete
* a modal or interstitial blocked rendering
* the site structure changed
* the feed content appeared in a different container than expected

Try:

* `--headed`
* `--manual-login`
* `--debug`
* `--user-data-dir ./user_data`

### Feed loaded visually but extraction is incomplete

Increase scroll and wait settings:

```bash
python financialjuice_extractor.py --headed --manual-login --debug --hours 24 --max-scroll-rounds 60 --scroll-pause-ms 2000 --out-dir out
```

## Notes

* This extractor is intended for dynamic rendered content, not static HTML snapshots.
* A persistent browser profile is often the most reliable setup for repeated authenticated runs.
* `news.jsonl` is the preferred export for downstream LLM workflows.
