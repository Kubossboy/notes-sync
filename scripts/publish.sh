#!/usr/bin/env bash
# One-shot: export a scrubbed read-only snapshot and deploy it to Cloudflare Pages (free plan).
# Needs in .env (git-ignored, never printed): CLOUDFLARE_API_TOKEN (scope: Account > Cloudflare Pages > Edit)
# and CLOUDFLARE_ACCOUNT_ID. Cron-able:  */5 * * * * cd /workspace/atlas-trading-ai && scripts/publish.sh >> logs/publish.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=var/site
if [ -f .env ]; then
  set -a; # shellcheck disable=SC1091
  . <(grep -E '^(CLOUDFLARE_API_TOKEN|CLOUDFLARE_ACCOUNT_ID)=' .env || true); set +a
fi
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] || [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  echo "publish: needs_credentials (CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID missing in .env)" >&2; exit 3
fi
rm -rf "$OUT"
INFO=$(.venv/bin/python -m app.main export --out "$OUT" 2>/dev/null | tail -1)
PROJECT=$(printf '%s' "$INFO" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["project"])')
SLUGPATH=$(printf '%s' "$INFO" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["path"])')
WR="npx -y wrangler@4"
$WR pages project list 2>/dev/null | grep -q "$PROJECT" || $WR pages project create "$PROJECT" --production-branch main >/dev/null
$WR pages deploy "$OUT" --project-name "$PROJECT" --branch main --commit-dirty=true >/dev/null
URL="https://$PROJECT.pages.dev$SLUGPATH"
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL" || true)
echo "$(date '+%F %T %Z') published $URL -> HTTP $code"
