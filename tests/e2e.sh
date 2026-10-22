#!/usr/bin/env bash
# End-to-end proof, run on the head Spark (local requests need no key):
#   tests/e2e.sh <model key> <sparks>        e.g. tests/e2e.sh qwen-next 2
# Asserts: both nodes answer, Start brings the model up, the /v1 front door answers a chat by the model's own id and
# by the "spark" alias, a non-running model gets a clear 503, and Stop takes it down again.
set -euo pipefail
MODEL=${1:?model key}; SPARKS=${2:-2}
BASE=http://$(tailscale ip -4 | head -1):8099
say() { printf '%s\n' "$*"; }
fail() { say "FAIL: $*"; exit 1; }
api() { curl -sf -m 30 "$@"; }

say "== state"
api "$BASE/api/state" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for role, n in d["nodes"].items(): print("  %-6s %s %s %s GiB free" % (role, n.get("host", n["name"]), "up" if n["reachable"] else "DOWN", n.get("mem_gib")))
assert all(n["reachable"] for n in d["nodes"].values()), "a node is unreachable"
for m in d["models"]: print("  %-10s %s" % (m["key"], m["status"]))'

say "== start $MODEL on $SPARKS Spark(s)"
api -X POST "$BASE/api/models/$MODEL/load?sparks=$SPARKS" >/dev/null || fail "load refused"
for i in $(seq 1 360); do
  st=$(api "$BASE/api/state" | python3 -c "import json,sys; print(next(m['status'] for m in json.load(sys.stdin)['models'] if m['key']=='$MODEL'))")
  [ "$st" = running ] && break
  [ "$st" = error ] && { api "$BASE/api/models/$MODEL/logs?lines=40" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["lines"]))'; fail "start failed"; }
  [ $((i % 12)) -eq 0 ] && say "  $st ($((i * 5))s)"
  sleep 5
done
[ "$st" = running ] || fail "not running after 30 min"

ID=$(api "$BASE/api/state" | python3 -c "import json,sys; print(next(m['api_id'] for m in json.load(sys.stdin)['models'] if m['key']=='$MODEL'))")
for id in "$ID" spark; do
  say "== chat via /v1 as '$id'"
  api "$BASE/v1/chat/completions" -H 'content-type: application/json' \
    -d "{\"model\":\"$id\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: stack ok\"}],\"max_tokens\":400}" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); m=r["choices"][0]["message"]; t=(m.get("content") or "").strip(); print("  model=%s tokens=%s text=%r" % (r.get("model"), r["usage"]["completion_tokens"], t[:60])); assert t' \
    || fail "no completion"
done

say "== a model that is not running gets a 503"
other=$(api "$BASE/api/state" | python3 -c "import json,sys; print(next(m['api_id'] for m in json.load(sys.stdin)['models'] if m['key']!='$MODEL'))")
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$BASE/v1/chat/completions" -H 'content-type: application/json' \
  -d "{\"model\":\"$other\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}")
[ "$code" = 503 ] || fail "expected 503 for $other, got $code"
say "  $other -> 503"

say "== stop"
api -X POST "$BASE/api/models/$MODEL/unload" >/dev/null
for i in $(seq 1 60); do
  up=$(api "$BASE/api/state" | python3 -c "import json,sys; print(next(m['up'] for m in json.load(sys.stdin)['models'] if m['key']=='$MODEL'))")
  [ "$up" = False ] && break; sleep 5
done
[ "$up" = False ] || fail "still up after stop"
say "PASS"
