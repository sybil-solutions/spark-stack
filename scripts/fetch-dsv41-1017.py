#!/usr/bin/env python3
"""Fetch the 2.7 bpw 1017 checkpoint plus DeepSeek's two Engram shards, verify them, and put the same directory on
the worker.

The checkpoint repo ships everything except the Engram tables (203 GB); the model card pairs them from
deepseek-ai/DeepSeek-V4.1-Flash-1017 at the pinned revision. The pinned launcher reads MODEL_DIR on **both** ranks at
the same path, so the verified tree is rsynced to $WORKER over the fabric and checked there with the same manifest.

    MODEL_DIR=~/models/dsv41-1017-exl3 WORKER=user@fabric-ip python3 fetch-dsv41-1017.py
    ... --sync-only              # skip the download, only copy and verify on the worker
"""

import os
import subprocess
import sys
from pathlib import Path

DEST = Path(os.environ.get("MODEL_DIR", Path.home() / "models" / "dsv41-1017-exl3")).expanduser()
WORKER = os.environ.get("WORKER", "")
CKPT = "0xSero/DeepSeek-V4.1-Flash-1017-EXL3-2.7bpw"
CKPT_REV = "e34ef55b"
ENGRAM = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
ENGRAM_REV = "1bae0e1088993b9b82de08f59de5be5a34e42170"
ENGRAM_FILES = ["model-00047-of-00048.safetensors", "model-00048-of-00048.safetensors"]


def download() -> int:
    from huggingface_hub import hf_hub_download, snapshot_download

    DEST.mkdir(parents=True, exist_ok=True)
    print(f"checkpoint {CKPT}@{CKPT_REV} -> {DEST}", flush=True)
    snapshot_download(CKPT, revision=CKPT_REV, local_dir=DEST, max_workers=8)

    for name in ENGRAM_FILES:
        print(f"engram {ENGRAM}/{name}", flush=True)
        hf_hub_download(ENGRAM, name, revision=ENGRAM_REV, local_dir=DEST)

    if not (DEST / "MANIFEST.sha256").exists():
        print("MANIFEST.sha256 missing; cannot verify", file=sys.stderr)
        return 1
    print("verifying MANIFEST.sha256 here", flush=True)
    return subprocess.call(["sha256sum", "-c", "MANIFEST.sha256"], cwd=DEST)


def remote_dir() -> str:
    """Same directory on the worker; its home differs from the head's, so the home prefix becomes ~."""
    home = str(Path.home())
    text = str(DEST)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def sync_to_worker() -> int:
    if not WORKER:
        print("WORKER is not set: copying nowhere (the head is ready)", flush=True)
        return 0
    remote = remote_dir()
    print(f"copying {DEST} -> {WORKER}:{remote} over the fabric", flush=True)
    if subprocess.call(["ssh", "-o", "BatchMode=yes", WORKER, f"mkdir -p {remote}"]) != 0:
        return 1
    if subprocess.call(["rsync", "-a", "--info=progress2", f"{DEST}/", f"{WORKER}:{remote}/"]) != 0:
        return 1
    print(f"verifying MANIFEST.sha256 on {WORKER}", flush=True)
    return subprocess.call(["ssh", "-o", "BatchMode=yes", WORKER,
                            f"cd {remote} && sha256sum -c MANIFEST.sha256"])


def main() -> int:
    if "--sync-only" not in sys.argv:
        rc = download()
        if rc != 0:
            return rc
    return sync_to_worker()


if __name__ == "__main__":
    raise SystemExit(main())
