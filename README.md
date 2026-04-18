# FinancialJuice News Extractor

A Playwright-based FinancialJuice news extractor that captures at least 24 hours of feed data from the latest available headline and exports structured outputs for exhaustive AI-driven analysis.

## 1) Install

```bash
pip install playwright python-dateutil
playwright install chromium
```

## 2) Run

### Option A - normal login via environment variables

```bash
export FJ_EMAIL='your_email'
export FJ_PASSWORD='your_password'
python financialjuice_extractor.py --hours 24 --out-dir out
```

### Option B - manual login in a visible browser

```bash
python financialjuice_extractor.py --headed --manual-login --hours 24 --out-dir out
```

### Option C - reuse your existing Chrome profile

```bash
python financialjuice_extractor.py \
  --user-data-dir '/path/to/Chrome/User Data' \
  --profile-directory Default \
  --headed --hours 24 --out-dir out
```

## 3) Output files

- `news.jsonl`: best format for attaching to another AI.
- `news.json`: array version.
- `news.md`: human-readable and LLM-friendly.
- `summary.json`: run metadata.

## Recommended file for AI analysis

Use `news.jsonl` when you want structured ingestion.
Use `news.md` when you want the model to read the narrative directly.

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
8. Highlight what changed during the 24h window.

Return:
- Executive summary
- Chronological timeline
- Theme clusters
- Asset-impact matrix
- Open questions
- Key risks
```
