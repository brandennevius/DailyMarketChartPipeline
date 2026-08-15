# Daily Market & Portfolio Review

This package keeps trading logic in deterministic Python and uses the review packet as the single source for Markdown and PDF outputs. The model/prompt layer should request a workflow run; it should not reinterpret sell rules or invent report facts.

## Core command

```bash
python -m market_chart_pipeline.daily_review --date YYYY-MM-DD --mode read-only
```

Optional inputs:

```bash
python -m market_chart_pipeline.daily_review \
  --date 2026-08-14 \
  --mode read-only \
  --market-data fixtures/market-2026-08-14.json \
  --portfolio fixtures/portfolio-2026-08-14.json \
  --candidates fixtures/candidates-2026-08-14.json \
  --shakeouts fixtures/shakeouts-2026-08-14.json \
  --chart-packet-dir output/2026-08-14 \
  --output-dir reports/market
```

The command writes:

- `reports/market/YYYY-MM-DD/YYYY-MM-DD-market-review.json`
- `reports/market/YYYY-MM-DD/YYYY-MM-DD-market-review.md`
- `reports/market/YYYY-MM-DD/YYYY-MM-DD-market-review.pdf`

## Replay Friday 2026-08-14

After generating or locating the verified chart packet for August 14, run:

```bash
python -m market_chart_pipeline.daily_review \
  --date 2026-08-14 \
  --session-date 2026-08-14 \
  --mode read-only \
  --chart-packet-dir output/2026-08-14 \
  --output-dir reports/market
```

Add `--portfolio`, `--candidates`, `--shakeouts`, and `--market-data` when those read-only JSON exports are available. Missing critical inputs remain marked `INSUFFICIENT_EVIDENCE`; they are not estimated.

## Policy

Tunable thresholds live in `config/trading_policy.json` and include max initial loss, 2 ATR stop policy, rapid advance, profit zone, peak drawdown, patience, portfolio risk, candidate scoring, and shakeout/re-entry thresholds.

## Delivery state

Processed receipts are terminal even when delivery fails. A failed delivery receipt records `delivery.status = FAILED` and the error so scheduled runs do not rebuild the same packet forever or claim success.
