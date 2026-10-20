#!/usr/bin/env bash
# Prove the harness half: dsh is in the image, and a profile patched by the stack exposes the three routes.
#   ./tests/harness.sh spark-stack:dev [public-host]
set -euo pipefail
IMAGE=${1:-spark-stack:dev}
PUBLIC=${2:-127.0.0.1}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

say() { printf '%s\n' "$*"; }

say "== dsh in the image"
docker run --rm --entrypoint dsh "$IMAGE" --version 2>&1 | tail -1 || { say "FAIL: dsh missing"; exit 1; }

say "== stack writes the provider layer"
docker run --rm -i --network host \
  -v "$TMP:/root/.dsh" \
  -e DSH_HOME=/root/.dsh \
  -e DSH_PATCH=/root/.dsh/profiles/web/cordis.patch.yml \
  -e STACK_PUBLIC_HOST="$PUBLIC" \
  --entrypoint python "$IMAGE" - <<'PY'
import os, pathlib
os.environ.setdefault("STACK_STATE_DIR", "/tmp/state")
pathlib.Path("/tmp/state").mkdir(exist_ok=True)
pathlib.Path("/root/.dsh/profiles/web").mkdir(parents=True, exist_ok=True)
pathlib.Path("/root/.dsh/profiles/web/cordis.patch.yml").write_text(
    "- id: llm-pi-ai\n  name: \"@deepseek-ai/dsh-llm-pi-ai\"\n  config:\n    providers:\n      keep-me:\n        api: openai-completions\n")
from stack.app import api_harness_apply
print("applied:", api_harness_apply()["ok"])
PY

say "== the patch parses and keeps the user's routes"
docker run --rm -i -v "$TMP:/root/.dsh" --entrypoint python "$IMAGE" - <<'PY'
import yaml, pathlib
d = yaml.safe_load(pathlib.Path("/root/.dsh/profiles/web/cordis.patch.yml").read_text())
ids = [e["id"] for e in d]
assert ids == ["llm-pi-ai", "llm-pi-ai-sparks"], ids
keep = d[0]["config"]["providers"]
spark = d[1]["config"]["providers"]
assert "keep-me" in keep, keep
assert set(spark) == {"spark-glm", "spark-qwen", "spark-deepseek"}, list(spark)
print("routes:", ", ".join(spark))
PY

say "PASS"
