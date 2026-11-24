#!/usr/bin/env bash
# One-shot: export a scrubbed read-only snapshot and deploy it to Cloudflare Pages (free plan).
# Needs env CLOUDFLARE_API_TOKEN (scope: Account > Cloudflare Pages > Edit) - taken from the process env only,
# never written to .env/repo, never printed. CLOUDFLARE_ACCOUNT_ID: env, else var/publish.json (git-ignored).
# Cron-able (cron must get the token via its env):  */5 * * * * cd /workspace/atlas-trading-ai && scripts/publish.sh >> logs/publish.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=var/site
if [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ] && [ -f var/publish.json ]; then
  CLOUDFLARE_ACCOUNT_ID=$(.venv/bin/python -c 'import json;print(json.load(open("var/publish.json")).get("account_id",""))')
  export CLOUDFLARE_ACCOUNT_ID
fi
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] || [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  echo "publish: needs_credentials (CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID missing in env)" >&2; exit 3
fi
rm -rf "$OUT"
INFO=$(.venv/bin/python -m app.main export --out "$OUT" 2>/dev/null | tail -1)
PROJECT=$(printf '%s' "$INFO" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["project"])')
SLUGPATH=$(printf '%s' "$INFO" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["path"])')
WR="npx -y wrangler@4"
$WR pages project list 2>/dev/null | grep -q "$PROJECT" || $WR pages project create "$PROJECT" --production-branch main >/dev/null
$WR pages deploy "$OUT" --project-name "$PROJECT" --branch main --commit-dirty=true > logs/publish_wrangler.last 2>&1 \
  || { echo "publish: wrangler deploy failed (see logs/publish_wrangler.last)" >&2; exit 4; }
URL="https://$PROJECT.pages.dev$SLUGPATH"
sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL" || true)
echo "$(date '+%F %T %Z') published $URL -> HTTP $code"
