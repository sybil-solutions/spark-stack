#!/bin/bash
# After the Qwen weights finish downloading, hand them to the stack (prepare: verify + copy to the worker),
# then let the stack prepare DeepSeek. Runs on the head; the token stays on the host.
set -u
T=$(cat "$HOME/spark-stack-state/token")
API=http://127.0.0.1:8099
api() { curl -s -m 30 -H "Authorization: Bearer $T" "$@"; }
status_of() {
  api "$API/api/state" | python3 -c "import json,sys;print(next(m['status'] for m in json.load(sys.stdin)['models'] if m['key']=='$1'))"
}

while tmux has-session -t qwen-dl 2>/dev/null; do sleep 30; done
echo "$(date -u +%FT%TZ) qwen download finished: $(du -sh "$HOME/.cache/huggingface/hub/models--nvidia--Qwen3.8-Flash-Next-NVFP4" 2>/dev/null | cut -f1)"

api -X POST "$API/api/models/qwen/prepare" >/dev/null
for _ in $(seq 1 240); do
  st=$(status_of qwen)
  [ "$st" = "ready" ] && break
  sleep 30
done
echo "$(date -u +%FT%TZ) qwen prepare: $st"

# DeepSeek is not fetched automatically: 462 GB (240 GB EXL3 banks + 222 GB base shards, of which ~197 GB is
# transient Engram parts). Press "fetch" on its card when you want it.
echo "$(date -u +%FT%TZ) deepseek left un-started (manual fetch; ~462 GB download, ~265 GB resident)"
