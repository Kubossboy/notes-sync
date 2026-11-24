#!/usr/bin/env bash
# Offline unit tests, then live network tests against free public APIs.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -m "not live" -q
.venv/bin/python -m pytest -m live -q
