#!/usr/bin/env bash
# Create the virtualenv and install pinned dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null; then
  uv venv -q .venv --python 3.13 2>/dev/null || uv venv -q .venv
  uv pip install -q --python .venv/bin/python -r requirements.txt
else
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/python -m app.main init
