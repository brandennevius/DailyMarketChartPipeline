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

The frozen JSON preserves the portfolio fields used in the report, including
exposure, P&L, working stops, stop risk, targets, setup grades, R multiples,
technical context, leadership breadth, and chart-asset hashes. Markdown and
PDF narrative is derived from that packet. The PDF presents audit hashes and
known evidence gaps in an appendix rather than mixing them into the decision
summary.

`market_breadth` is explicitly scoped to the MarketSurge-derived review
universe. It does not substitute for an O'Neil market-regime determination;
without index follow-through and distribution-day evidence, the regime remains
`INSUFFICIENT_EVIDENCE`.

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

The transcript-derived sell hierarchy is deterministic:

1. Hard capital protection uses the tighter of the purchase-price loss limit,
   structural stop, 2 ATR initial stop, activated gain-protection floor, and
   configured break-even protection.
2. After the highest close is at least 7% above entry, the loss floor tightens
   to 5% below the actual purchase price and an 11% highest-close trail begins
   ratcheting upward.
3. A 20%-25% advance from a verified pivot produces `REDUCE` only after the
   configured eight-week minimum hold.
4. Reaching 20% within 15 trading days activates the eight-week rapid-advance
   hold. It suspends soft profit/trailing actions, never hard capital
   protection.
5. At thirteen weeks, inadequate progress can produce `REDUCE` under the
   patience rule.

Future chart packets retain bounded daily OHLCV history so the review can
derive highest close, trading days held, and the first day a position reached
20%. Older packets without that history remain explicitly
`INSUFFICIENT_EVIDENCE` for those calculations.

## Sell-rule sandbox charts

The orchestrator creates one `assets/TICKER_sell_sandbox.png` for every open
long equity position before freezing the packet. Each asset and its calculated
levels are recorded with a SHA-256 hash in the corresponding position result.
Strict-core validation fails when any reported position lacks a verified
sandbox chart.

The chart overlays:

- actual purchase price and broker working stop;
- the configured 5%-8% loss zone from purchase price;
- the 2 ATR initial-stop reference using ATR measured at entry when history is
  available;
- the +7% activated protection floor;
- the stepped 11% highest-close trail;
- the 20%-25% profit zone only when the pivot is verified;
- the eight- and thirteen-week trading-day boundaries; and
- the rapid-advance hold window when its trigger is verified.

The policy intentionally remains hybrid. ATR adapts the initial stop to normal
volatility, while the fixed percentage cap limits portfolio damage and the
pivot/time rules preserve their CANSLIM meaning. The engine uses the tighter
applicable hard boundary; it does not replace the fixed rules with ATR or draw
an unverified pivot.

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
