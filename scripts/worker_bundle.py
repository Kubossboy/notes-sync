"""Bundle an exported snapshot dir (var/site) into one Cloudflare Worker module + an MCP deploy snippet.

Fallback publish path used when only the Cloudflare MCP connection is available (no API token):
    .venv/bin/python -m app.main export --out var/site && .venv/bin/python scripts/worker_bundle.py var/site /tmp/deploy_worker.js
then run /tmp/deploy_worker.js through the Cloudflare connection's `execute` tool. Contains only the scrubbed export.
"""
import json
import sys
from pathlib import Path

CT = {".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
root, out = Path(sys.argv[1]), Path(sys.argv[2])
files = {"/" + str(p.relative_to(root)): {"ct": CT.get(p.suffix, "application/octet-stream"), "body": p.read_text()}
         for p in root.rglob("*") if p.is_file() and p.name != "_headers"}
name = json.loads(Path("var/publish.json").read_text())["project"]
worker = """const F = %s;
const H = {'X-Robots-Tag':'noindex, nofollow, noarchive','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Cache-Control':'no-store'};
export default { async fetch(req) {
  let path = new URL(req.url).pathname;
  if (path.endsWith('/')) path += 'index.html';
  if (req.method !== 'GET' && req.method !== 'HEAD') return new Response('read-only', {status: 405, headers: H});
  const f = F[path];
  if (!f) return new Response('Not found', {status: 404, headers: {...H, 'Content-Type': 'text/plain'}});
  return new Response(req.method === 'HEAD' ? null : f.body, {headers: {...H, 'Content-Type': f.ct}});
} };""" % json.dumps(files)
code = """async () => {
  const name = %s; const code = %s;
  const sub = await cloudflare.request({method:'GET', path:`/accounts/${accountId}/workers/subdomain`});
  const form = new FormData();
  form.append('metadata', new Blob([JSON.stringify({main_module:'worker.js', compatibility_date:'2026-11-01'})], {type:'application/json'}));
  form.append('worker.js', new Blob([code], {type:'application/javascript+module'}), 'worker.js');
  const put = await cloudflare.request({method:'PUT', path:`/accounts/${accountId}/workers/scripts/${name}`, body: form});
  const en = await cloudflare.request({method:'POST', path:`/accounts/${accountId}/workers/scripts/${name}/subdomain`, body:{enabled:true, previews_enabled:false}});
  return {ok: put.success && en.success, host: `${name}.${sub.result.subdomain}.workers.dev`};
}""" % (json.dumps(name), json.dumps(worker))
out.write_text(code)
print(f"{out}: {len(code)} bytes, {len(files)} files")
