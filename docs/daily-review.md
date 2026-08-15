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

The production replay acquires the exact-subject Gmail portfolio JSON and
MarketSurge PDF, downloads the newest unexpired chart artifact with the exact
session name, runs the strict-core audit, delivers the report, and records a
terminal receipt:

```bash
gh workflow run daily-review.yml --ref main -f session_date=2026-08-14
```

If a terminal failure occurred before any canonical packet was created, an
explicit repair replay may use
`-f retry_prepacket_failure=true`. This flag cannot rebuild a terminal run
that already has a packet hash; delivery retries for such a run must reuse its
frozen artifact.

Monitor it with:

```bash
gh run watch "$(gh run list --workflow daily-review.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
```

For an offline replay with already acquired read-only inputs, run:

```bash
python -m market_chart_pipeline.daily_review \
  --date 2026-08-14 \
  --session-date 2026-08-14 \
  --mode read-only \
  --portfolio source-input/portfolio.json \
  --chart-packet-dir output/2026-08-14 \
  --source-manifest source-input/source-manifest.json \
  --audit-profile strict-core \
  --output-dir reports/market
```

Add `--portfolio`, `--candidates`, `--shakeouts`, and `--market-data` when those read-only JSON exports are available. Missing critical inputs remain marked `INSUFFICIENT_EVIDENCE`; they are not estimated.

## Policy

Tunable thresholds live in `config/trading_policy.json` and include max initial loss, 2 ATR stop policy, rapid advance, profit zone, peak drawdown, patience, portfolio risk, candidate scoring, and shakeout/re-entry thresholds.

## Delivery state

The production workflow runs at 02:30 UTC Tuesday-Saturday, after the prior
U.S. session's source messages normally arrive. It requires `GMAIL_ADDRESS`
and `GMAIL_APP_PASSWORD`; `DAILY_REVIEW_RECIPIENT` falls back to
`CHART_PACKET_RECIPIENT` and then to the sender.

Processed receipts are terminal even when source acquisition, audit, or
delivery fails. A receipt records the packet hash when one exists, audit
status, explicit delivery status/error, and `terminal: true`, so the scheduler
does not rebuild an unchanged session forever or report delivery success when
SMTP failed.
