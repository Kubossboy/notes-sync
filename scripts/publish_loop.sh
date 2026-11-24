#!/usr/bin/env bash
# Re-export + deploy every INTERVAL seconds (default 300). NOT started automatically.
# Stop: touch var/STOP_PUBLISH. Prefer cron (see scripts/publish.sh header) over this loop.
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL="${INTERVAL:-300}"; mkdir -p logs var; echo $$ > var/publish.pid
while [ ! -f var/STOP_PUBLISH ]; do
  scripts/publish.sh >> logs/publish.log 2>> logs/publish.err || echo "$(date '+%F %T') publish failed ($?)" >> logs/publish.err
  sleep "$INTERVAL"
done
rm -f var/publish.pid
