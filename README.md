# Daily Market Chart Pipeline

Standalone post-close chart packet generator for the Daily Market & Portfolio Review.

## Purpose

This repository is intentionally separate from TradingDashboard. It accepts a current-session ticker manifest, retrieves adjusted daily OHLCV from Alpaca, validates data freshness, calculates technical metrics, generates daily and weekly charts, and produces a PDF/JSON chart packet for the scheduled ChatGPT market review.

## Required GitHub Actions secrets

- `ALPACA_API_KEY`
- `ALPACA_API_SECRET`

Optional email delivery secrets:

- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `CHART_PACKET_RECIPIENT` (defaults to `GMAIL_ADDRESS`)

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
export ALPACA_API_KEY=...
export ALPACA_API_SECRET=...
python -m market_chart_pipeline.cli --session-date 2026-08-04 --tickers AAPL,MSFT,NVDA --output-dir output
```

## Fail-closed behavior

A ticker is not chart verified when:

- fewer than 200 daily bars are available;
- the latest bar does not match the requested completed session;
- OHLCV values are invalid;
- the chart cannot be rendered;
- the packet manifest and generated artifacts do not reconcile.

The quantitative gate is a ranking/rejection aid only. It never declares a stock actionable.
