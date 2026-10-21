# Spark Stack

One command turns one or two NVIDIA DGX Sparks (or GB10 boxes such as the ASUS Ascent GX10) into a local AI lab:
a web app to download, start and stop models, two agent harnesses already pointed at them, and one API address that
always reaches whatever model is running.

```bash
curl -fsSL https://raw.githubusercontent.com/sybil-solutions/spark-stack/main/install.sh | bash
```

Run it on the Spark you sit at, as your normal desktop user; it asks for your password once. With a QSFP cable to a
second Spark (same username and password on both, both on the same Tailscale network) it finds that Spark and sets it up
too. Running the same command again repairs an install. `~/spark-stack/uninstall.sh` removes it (weights are kept unless
`--weights`).

## Models

The fastest public recipe for each model and Spark count, all [MiaAI-Lab](https://github.com/MiaAI-Lab) TensorFold
recipes, pinned by commit in [`stack/registry.py`](stack/registry.py). Speeds are the recipes' own single-user
measurements.

| Model | 1 Spark | 2 Sparks | Download | Port |
|---|---|---|---|---|
| Qwen3.8-Flash-Next (180B-A6B, vision) | [Single-DGX-Spark-TensorFold](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold): ~50 tok/s | [Dual-DGX-Sparks-TensorFold](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold): 67 tok/s | 96 / 124 GiB (NVIDIA NVFP4) | 8889 |
| Qwen3.8-27B (dense, vision, DFlash2 drafts) | [27B-DGX-Spark-TensorFold](https://github.com/MiaAI-Lab/Qwen3.8-27B-DGX-Spark-TensorFold): 58 prose, 137 code | not needed | 19 GiB | 8891 |
| GLM-5.3-Flash (320B-A18B, vision) | not offered (2-bit only, 10-25 tok/s) | [EXL3-2x-DGX-Sparks-TensorFold](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold): 60 prose, 115 structured | 166 GiB | 8888 |
| DeepSeek-V4.1-Flash-1017 | does not fit | [1017-2x-DGX-Sparks](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks): 75 prose, 110 code | 385 GiB | 8890 |

Install starts the Qwen3.8-Flash-Next download (two-Spark build, or the one-Spark build on a single Spark). Everything
else downloads when someone presses **Download** or **Start**. One model runs at a time.

## What the installer does

1. Finds the second Spark: on the QSFP cable, else the one tailnet machine with a live CX7 link that answers Tailscale
   SSH; `WORKER=user@host` overrides, `SINGLE=1` skips it.
2. On both Sparks ([`scripts/node-setup.sh`](scripts/node-setup.sh)): turns sleep off, adds the user to `docker`,
   addresses both logical twins of the cabled CX7 port (`192.168.100.10/11`, `192.168.101.10/11`), installs `rsync`
   and NFS, and shares DeepSeek's weights-only cache (`~/.cache/spark-stack-hf`) read-only to the worker's CX7
   addresses.
3. Installs the `hf` CLI (both Sparks), Node.js and the DeepSeek harness (`/opt/spark-stack-node`, service
   `spark-stack-dsh`, web UI on `127.0.0.1:3080`), and the OpenCode desktop app.
4. Clones each recipe at its pinned commit into `~/spark-stack/vendor/`.
5. Builds and starts the `spark-stack` container (restarts with the machine), bound to this Spark's Tailscale address
   only: `http://<tailscale-ip>:8099`.
6. Writes the models into the DeepSeek harness (`~/.dsh/profiles/web/cordis.patch.yml`, managed block) and OpenCode
   (`~/.config/opencode/opencode.json`, provider `spark`), and pins **Spark Stack**, **DeepSeek Harness** and
   **OpenCode** to the dock.

## The app

`http://<tailscale-ip>:8099`. On the Spark itself it opens without a key; from another tailnet machine paste the key
from `~/spark-stack-state/token` once. Each model card has a **1 Spark / 2 Sparks** choice, **Download** with a
progress bar, **Start/Stop**, and the live log. **Disk** lists the model folders on both Sparks with a **Remove** button.

## One API for every app

`http://<tailscale-ip>:8099/v1` is OpenAI-compatible and needs no key. Model id `spark` goes to whatever is running;
each model's own id (`Qwen3.8-Flash-Next`, `Qwen3.8-27B`, `GLM-5.3-Flash-EXL3`, `DeepSeek-V4.1-Flash-1017`) goes to
that model and gets a clear `503` when it is not running.

| method | path | purpose |
|---|---|---|
| GET | `/api/state` | machines, models, download progress, what is running |
| POST | `/api/models/{key}/prepare?sparks=1\|2` | download |
| POST | `/api/models/{key}/load?sparks=1\|2` | start (downloads what is missing first) |
| POST | `/api/models/{key}/unload` | stop |
| GET | `/api/models/{key}/logs` | the job log |
| GET | `/api/disk` | model folders on each Spark |
| POST | `/api/disk/remove` | `{"path": ...}` remove a model folder from both Sparks |
| GET | `/api/stats` | live tokens a second, power, memory |

Keys: `qwen-next`, `qwen-27b`, `glm`, `deepseek`.

## Recovery

- Something looks stuck: **Stop** the model in the app, then **Start** it again.
- The app does not open: `sudo docker restart spark-stack`.
- Anything else, or after an OS update: run the install command again.

## Test

On the head, with weights downloaded: `tests/e2e.sh qwen-next 2` starts the model, chats through `/v1` by id and by
`spark`, checks a stopped model returns 503, and stops it.

## Credits

Recipes, engines and images: [MiaAI-Lab](https://github.com/MiaAI-Lab) (TensorFold). Weights: NVIDIA, Vontra, z-lab,
Mia-AiLab, incoai, DeepSeek. Harnesses: [DeepSeek harness](https://www.npmjs.com/package/@deepseek-ai/dsh),
[OpenCode](https://github.com/anomalyco/opencode).
