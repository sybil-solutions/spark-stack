"""Write the stack's models into the DeepSeek harness (dsh) and OpenCode configs.

Both point at the stack's /v1 front door, so the model id `spark` always reaches whatever is running, and each model's
own id reaches that model when it runs.

    python3 -m stack.harness dsh <patch.yml> <base_url>        # managed block in a dsh cordis.patch.yml
    python3 -m stack.harness opencode <opencode.json> <base_url>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .registry import MODELS

BEGIN = "# >>> spark-stack providers (managed by spark-stack install.sh)"
END = "# <<< spark-stack providers"
ALIAS = "spark"
# dsh's OpenAI route refuses to send a request without a key ("No API key for provider"), though /v1 needs none.
# The provider names this variable; install.sh's spark-stack-dsh unit sets it to a placeholder.
KEY_ENV = "SPARK_STACK_API_KEY"


def models() -> list[dict]:
    out = [{"id": ALIAS, "name": "Spark: whatever is running", "context": 262_144, "vision": True}]
    out += [{"id": m.api_id, "name": f"{m.name} (Spark)", "context": m.context, "vision": m.vision}
            for m in MODELS.values()]
    return out


def dsh_block(base_url: str) -> str:
    lines = [BEGIN,
             "- id: llm-pi-ai",
             '  name: "@deepseek-ai/dsh-llm-pi-ai"',
             "  config:",
             "    providers:",
             "      spark-stack:",
             "        displayName: Spark Stack (local)",
             "        api: openai-completions",
             f"        baseURL: {base_url}",
             f"        apiKeyEnv: {KEY_ENV}",
             "        models:"]
    for m in models():
        lines += [f"          - id: {m['id']}",
                  f"            name: \"{m['name']}\"",
                  f"            contextWindow: {m['context']}",
                  "            maxTokens: 32768",
                  "            input:",
                  "              - text"]
        if m["vision"]:
            lines.append("              - image")
    lines.append(END)
    return "\n".join(lines) + "\n"


HEADER = ("# Your patch layer for this dsh profile, applied after every bundle layer:\n"
          "# a top-level YAML array of loader patch entries.\n")


def write_dsh(path: Path, base_url: str) -> None:
    text = path.read_text() if path.exists() else HEADER
    if BEGIN in text:
        head, _, rest = text.partition(BEGIN)
        _, _, tail = rest.partition(END)
        text = head.rstrip() + "\n" + tail.lstrip("\n")
    body = text.rstrip()
    if body.endswith("[]"):          # dsh's empty patch file is a literal empty array
        body = body[:-2].rstrip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((body + "\n\n" if body else "") + dsh_block(base_url))


def write_opencode(path: Path, base_url: str) -> None:
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("$schema", "https://opencode.ai/config.json")
    config.setdefault("provider", {})["spark"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Spark Stack (local)",
        "options": {"baseURL": base_url},
        "models": {m["id"]: {"name": m["name"], "limit": {"context": m["context"], "output": 32768},
                             **({"modalities": {"input": ["text", "image"], "output": ["text"]}} if m["vision"] else {})}
                   for m in models()},
    }
    config["model"] = f"spark/{ALIAS}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("dsh", "opencode"):
        print(__doc__, file=sys.stderr)
        return 2
    kind, path, base_url = argv[0], Path(argv[1]), argv[2]
    (write_dsh if kind == "dsh" else write_opencode)(path, base_url)
    print(f"{kind}: wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
