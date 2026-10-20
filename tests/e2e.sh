#!/usr/bin/env bash
# End-to-end proof for the spark stack, run from the head Spark (or anywhere that reaches it).
#   ./tests/e2e.sh http://127.0.0.1:8099 glm
# Asserts: state reports both nodes, load brings the OpenAI endpoint up, a completion returns tokens,
# stats report the active model, and unload takes it down again.
set -euo pipefail
API=${1:-http://127.0.0.1:8099}
MODEL=${2:-glm}
ENDPOINT_HOST=${ENDPOINT_HOST:-127.0.0.1}
TOKEN=${TOKEN:?set TOKEN to the stack token}  # ~/spark-stack-state/token on the head
AUTH="Authorization: Bearer $TOKEN"

say() { printf '%s\n' "$*"; }
fail() { say "FAIL: $*"; exit 1; }

wait_http() { # url, seconds
  local url=$1 secs=$2 i=0
  while [ $i -lt "$secs" ]; do
    if curl -sf -m 2 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 5; i=$((i + 5))
    if [ $((i % 60)) -eq 0 ]; then say "  still waiting (${i}s)"; fi
  done
  return 1
}

say "== state"
curl -sf -m 10 -H "$AUTH" "$API/api/state" | python3 -c "
import json,sys
d=json.load(sys.stdin)
h,w=d['nodes']['head'],d['nodes']['worker']
print('  head  ', h['name'], 'reachable' if h['reachable'] else 'DOWN', h.get('mem_gib'), 'GiB')
print('  worker', w['name'], 'reachable' if w['reachable'] else 'DOWN', w.get('mem_gib'), 'GiB')
for m in d['models']: print('  model ', m['key'], m['status'], 'up' if m['up'] else '-')
assert h['reachable'] and w['reachable'], 'a node is unreachable'
"

say "== load $MODEL"
code=$(curl -s -o /tmp/load.out -w '%{http_code}' -m 20 -H "$AUTH" -X POST "$API/api/models/$MODEL/load")
case "$code" in
  200) ;;
  409) say "  already loading or serving: $(cat /tmp/load.out)";;
  *) fail "load request rejected ($code): $(cat /tmp/load.out)";;
esac

port=$(curl -sf -m 10 -H "$AUTH" "$API/api/state" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(next(m['port'] for m in d['models'] if m['key']=='$MODEL'))")
say "  waiting for :$port (up to 25 min)"
wait_http "http://$ENDPOINT_HOST:$port/v1/models" 1500 || { say "--- log"; curl -sf -m 10 -H "$AUTH" "$API/api/models/$MODEL/logs?lines=40" | python3 -c "import json,sys;print('\n'.join(json.load(sys.stdin)['lines']))"; fail "endpoint never came up"; }
say "  endpoint up"

say "== completion"
python3 - "$port" "$ENDPOINT_HOST" <<'PY'
import json, sys, urllib.request
port, host = sys.argv[1], sys.argv[2]
body = json.dumps({"model": None, "messages": [{"role": "user", "content": "Reply with exactly: stack ok"}],
                   "max_tokens": 256, "temperature": 0, "stream": False}).encode()
models = json.load(urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=10))
mid = models["data"][0]["id"]
body = json.loads(body); body["model"] = mid
req = urllib.request.Request(f"http://{host}:{port}/v1/chat/completions", data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
out = json.load(urllib.request.urlopen(req, timeout=180))
text = out["choices"][0]["message"].get("content") or ""
usage = out.get("usage", {})
print(f"  model={mid} completion_tokens={usage.get('completion_tokens')} text={text!r}")
assert usage.get("completion_tokens"), "no tokens returned"
PY

say "== stats"
curl -sf -m 15 -H "$AUTH" "$API/api/stats" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  active:', d['active'])
print('  head mem:', d['nodes']['head'].get('mem_gib'), 'GiB util', d['nodes']['head'].get('util'))
for k,v in d['models'].items():
    if v['up']: print('  up:', k, 'metrics' if v.get('metrics') else 'no metrics')
"

say "== unload $MODEL"
curl -sf -m 20 -H "$AUTH" -X POST "$API/api/models/$MODEL/unload" >/dev/null
for i in $(seq 1 24); do
  up=$(curl -sf -m 5 -H "$AUTH" "$API/api/state" | python3 -c "import json,sys;print([m['up'] for m in json.load(sys.stdin)['models'] if m['key']=='$MODEL'][0])")
  [ "$up" = "False" ] && { say "  down"; break; }
  sleep 5
done
[ "$up" = "False" ] || fail "still serving after unload"
say "PASS"
