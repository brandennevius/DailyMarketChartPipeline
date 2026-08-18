# Daily Market Chart Pipeline

Standalone post-close chart packet generator for the Daily Market & Portfolio Review.

## Purpose

This repository is intentionally separate from TradingDashboard. It accepts a current-session ticker manifest, retrieves bounded historical daily OHLCV from FMP, validates exact-session freshness, calculates technical metrics, generates daily and weekly charts, and produces a PDF/JSON chart packet for the scheduled market review.

## Required GitHub Actions secrets

- `FMP_API_KEY`

Optional email delivery secrets:

- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `CHART_PACKET_RECIPIENT` (defaults to `GMAIL_ADDRESS`)
- `DAILY_REVIEW_RECIPIENT` (defaults to `CHART_PACKET_RECIPIENT`)

`GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` are required by the production Daily
Market & Portfolio Review workflow because it acquires the exact-session
portfolio snapshot and MarketSurge scan from Gmail before running strict
validation.

## Manual run

Open **Actions → Build Market Chart Packet → Run workflow** and provide:

- session date, such as `2026-08-04`
- comma-separated tickers

The workflow uploads the packet as a GitHub Actions artifact. When the optional Gmail secrets are configured, it also emails the PDF and JSON packet.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export FMP_API_KEY=...
python -m market_chart_pipeline.cli --session-date 2026-08-04 --tickers AAPL,MSFT,NVDA --output-dir output
```

## Fail-closed behavior

A ticker is not chart verified when:

- fewer than 200 daily bars are available;
- the latest bar does not match the requested completed session;
- OHLCV values are invalid;
- the chart cannot be rendered;
- the packet manifest and generated artifacts do not reconcile.

FMP requests use the stable `historical-price-eod/full` endpoint with explicit
`from` and `to` dates. The client applies bounded concurrency, a shared request
cadence, and retry/backoff for 429 and transient 5xx responses. Historical
reviews never substitute a live/current quote. Strict `AAA/BBB` FX identifiers
are requested from FMP as `AAABBB`; the original display symbol remains frozen
in packet provenance. FX price-only evidence is retained when FMP does not
provide volume, while volume-dependent conclusions remain insufficient evidence.

The quantitative gate is a ranking/rejection aid only. It never declares a stock actionable.

## Deterministic daily review

See [docs/daily-review.md](docs/daily-review.md) for the versioned policy,
strict source/audit path, local command, and exact workflow replay command.
The review PDF is decision-first: portfolio exposure and risk, an action board,
per-position stop/target context with current charts, a non-actionable visual
review queue, selected candidate charts, and a compact evidence appendix.
Each open equity position also receives a hash-locked sell-rule sandbox chart
that overlays the configured percentage, ATR, trailing, profit-zone, and time
boundaries on current-session candles.
