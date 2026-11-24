#!/usr/bin/env bash
# Continuous PAPER trading loop: one `trade` cycle (manage -> scan -> risk -> simulated fills -> equity
# snapshot) every INTERVAL seconds. The internal $1,000 ledger only; Alpaca orders stay off by config.
# Stop: create var/STOP (checked every cycle) or kill the PID in var/loop.pid. Kill switch: `python -m app.main kill "reason" --flatten`.
set -uo pipefail
cd "$(dirname "$0")/.."
INTERVAL="${INTERVAL:-60}"
echo $$ > var/loop.pid
rm -f var/STOP
while [ ! -f var/STOP ]; do
  start=$(date +%s)
  .venv/bin/python -m app.main trade --json >> logs/loop.log 2>> logs/loop.err || echo "$(date -Is) cycle failed" >> logs/loop.err
  sleep $(( INTERVAL - ($(date +%s) - start) > 5 ? INTERVAL - ($(date +%s) - start) : 5 ))
done
rm -f var/loop.pid
