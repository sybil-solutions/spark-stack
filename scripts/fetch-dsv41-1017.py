#!/usr/bin/env python3
"""Fetch the 2.7 bpw 1017 checkpoint plus DeepSeek's two Engram shards into one directory, then verify the manifest.

The checkpoint repo ships everything except the Engram tables (203 GB); the model card pairs them from
deepseek-ai/DeepSeek-V4.1-Flash-1017 at the pinned revision. Both downloads share a directory because the server
reads the Engram shards from beside the weights.
"""

import os
import subprocess
import sys
from pathlib import Path

DEST = Path(os.environ.get("MODEL_DIR", Path.home() / "models" / "dsv41-1017-exl3"))
CKPT = "0xSero/DeepSeek-V4.1-Flash-1017-EXL3-2.7bpw"
CKPT_REV = "e34ef55b"
ENGRAM = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
ENGRAM_REV = "1bae0e1088993b9b82de08f59de5be5a34e42170"
ENGRAM_FILES = ["model-00047-of-00048.safetensors", "model-00048-of-00048.safetensors"]


def main() -> int:
    from huggingface_hub import hf_hub_download, snapshot_download

    DEST.mkdir(parents=True, exist_ok=True)
    print(f"checkpoint {CKPT}@{CKPT_REV} -> {DEST}", flush=True)
    snapshot_download(CKPT, revision=CKPT_REV, local_dir=DEST, max_workers=8)

    for name in ENGRAM_FILES:
        print(f"engram {ENGRAM}/{name}", flush=True)
        hf_hub_download(ENGRAM, name, revision=ENGRAM_REV, local_dir=DEST)

    manifest = DEST / "MANIFEST.sha256"
    if not manifest.exists():
        print("MANIFEST.sha256 missing; cannot verify", file=sys.stderr)
        return 1
    print("verifying MANIFEST.sha256", flush=True)
    return subprocess.call(["sha256sum", "-c", "MANIFEST.sha256"], cwd=DEST)


if __name__ == "__main__":
    raise SystemExit(main())
