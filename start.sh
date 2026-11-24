#!/usr/bin/env bash
# ATLAS launcher (Linux/macOS). Usage: ./start.sh [setup|test|status|probe|trade|loop|dashboard|stop|kill "reason"|resume]
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
cmd="${1:-help}"; shift || true
need() { [ -x "$PY" ] || { echo "run ./start.sh setup first"; exit 1; }; }
case "$cmd" in
  setup)
    command -v python3 >/dev/null || { echo "Python 3.12+ required"; exit 1; }
    python3 -m venv .venv && "$PY" -m pip install -q --upgrade pip && "$PY" -m pip install -q -r requirements.txt
    [ -f .env ] || { cp .env.example .env; echo "created .env from .env.example (fill in your keys)"; }
    "$PY" -m app.main init ;;
  test)      need; "$PY" -m pytest -m "not live" -q ;;
  status)    need; "$PY" -m app.main status ;;
  probe)     need; "$PY" -m app.main probe ;;
  trade)     need; "$PY" -m app.main trade ;;
  loop)      need; "$PY" scripts/run_loop.py ;;
  dashboard) need; "$PY" -m app.main dashboard ;;
  stop)      mkdir -p var && touch var/STOP && echo "STOP requested - loop exits after the current cycle" ;;
  kill)      need; "$PY" -m app.main kill "${*:-manual kill}" --flatten ;;
  resume)    need; "$PY" -m app.main resume --by operator ;;
  *) echo "usage: ./start.sh setup|test|status|probe|trade|loop|dashboard|stop|kill \"reason\"|resume" ;;
esac
