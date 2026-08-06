#!/usr/bin/env bash
set -euo pipefail

REPO="brandennevius/DailyMarketChartPipeline"
WORKFLOW="chart-packet.yml"
REF="main"
MANIFEST_PATH="${1:-}"
FEED="${2:-iex}"
TIMEOUT_SECONDS="${DISPATCH_VERIFY_TIMEOUT_SECONDS:-90}"

if [[ -z "$MANIFEST_PATH" ]]; then
  echo "usage: $0 requests/YYYY-MM-DD-<run_id>.json [iex|sip]" >&2
  exit 2
fi
if [[ ! "$MANIFEST_PATH" =~ ^requests/20[0-9]{2}-[0-9]{2}-[0-9]{2}-.+\.json$ ]]; then
  echo "invalid manifest path: $MANIFEST_PATH" >&2
  exit 2
fi
if [[ "$FEED" != "iex" && "$FEED" != "sip" ]]; then
  echo "feed must be iex or sip" >&2
  exit 2
fi

command -v gh >/dev/null 2>&1 || { echo "GitHub CLI (gh) is required" >&2; exit 3; }
gh auth status >/dev/null

gh api "repos/$REPO/contents/$MANIFEST_PATH?ref=$REF" --jq '.path' | grep -Fx "$MANIFEST_PATH" >/dev/null

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH="$(date -u +%s)"

gh workflow run "$WORKFLOW" \
  --repo "$REPO" \
  --ref "$REF" \
  -f "manifest_path=$MANIFEST_PATH" \
  -f "feed=$FEED"

DEADLINE=$((START_EPOCH + TIMEOUT_SECONDS))
while (( $(date -u +%s) <= DEADLINE )); do
  RUN_JSON="$(gh run list \
    --repo "$REPO" \
    --workflow "$WORKFLOW" \
    --event workflow_dispatch \
    --branch "$REF" \
    --limit 20 \
    --json databaseId,createdAt,status,conclusion,url,displayTitle,headBranch)"

  MATCH="$(RUN_JSON="$RUN_JSON" python - "$STARTED_AT" <<'PY'
import json, os, sys
from datetime import datetime, timedelta

started = datetime.fromisoformat(sys.argv[1].replace('Z', '+00:00')) - timedelta(seconds=5)
runs = json.loads(os.environ['RUN_JSON'])
eligible = []
for run in runs:
    created = datetime.fromisoformat(run['createdAt'].replace('Z', '+00:00'))
    if created >= started and run.get('headBranch') == 'main':
        eligible.append(run)
if eligible:
    eligible.sort(key=lambda r: r['createdAt'], reverse=True)
    print(json.dumps(eligible[0], separators=(',', ':')))
PY
)"

  if [[ -n "$MATCH" ]]; then
    python - "$MANIFEST_PATH" "$FEED" "$STARTED_AT" "$MATCH" <<'PY'
import json, sys
manifest, feed, dispatched_at, run_raw = sys.argv[1:]
run = json.loads(run_raw)
print(json.dumps({
    'dispatch_status': 'STARTED',
    'manifest_path': manifest,
    'feed': feed,
    'dispatched_at': dispatched_at,
    'workflow_run_id': run['databaseId'],
    'workflow_run_created_at': run['createdAt'],
    'workflow_run_status': run['status'],
    'workflow_run_conclusion': run.get('conclusion'),
    'workflow_run_url': run['url'],
}, indent=2))
PY
    exit 0
  fi
  sleep 5
done

echo "workflow_dispatch was accepted but no new workflow run was observed within ${TIMEOUT_SECONDS}s" >&2
exit 4
