# spark stack

One container that prepares, serves, frees, and reports on three models across **two DGX Sparks**, and wires them
into the DeepSeek harness (`dsh`).

| model | recipe | checkpoint | port |
|---|---|---|---|
| GLM-5.3-Flash | `MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold` | `Mia-AiLab/GLM-5.3-Flash-EXL3-4bpw-TensorFold` (176 GB) | 8888 |
| Qwen3.8-Flash-Next | `MiaAI-Lab/Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold` | `nvidia/Qwen3.8-Flash-Next-NVFP4` (124 GB) | 8889 |
| DeepSeek-V4.1-Flash-1017 | `sudoingX/dgx-spark-deepseek-v41` @ `cc59c360` | `0xSero/DeepSeek-V4.1-Flash-1017-EXL3-2.7bpw` (199 GB) + DeepSeek Engram shards (203 GB) | 8000 |

Only one model is resident at a time: each needs ~98 GiB per Spark and both ranks own the whole fabric.

## How it works

The container is a control plane, not a serving runtime. It runs on the **head** Spark with the host network and the
host home mounted at the same path, so it uses the host's own ssh keys:

- head actions go through `ssh localhost` (the host's sshd),
- worker actions go through the recipe's own `ssh $WORKER docker …`,
- each recipe is vendored under `vendor/` and driven by its own `start.sh` / `stop.sh` / `scripts/prepare.sh`,
  unchanged, so the maintainers' preflight checks (RoCE rails, memory floor, port, image digest) still run.

Nothing needs the docker socket: `docker` runs on the host, where the recipes expect it.

## Run

```bash
# on the head Spark
git clone <this repo> ~/spark-stack && cd ~/spark-stack
mkdir -p vendor && (cd vendor && \
  git clone --depth 1 https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold.git glm && \
  git clone --depth 1 https://github.com/MiaAI-Lab/Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold.git qwen && \
  git clone --depth 1 https://github.com/0xSero/DeepSeek-V4.1-Flash-Two-Sparks.git deepseek)
./deploy.sh build
WORKER=valentine@10.10.10.13 ./deploy.sh run     # worker's fabric address
```

Open `http://<tailnet-ip>:8099` and paste the token from `~/spark-stack-state/token` (the controller holds the host's
ssh keys, so every `/api` call needs it). The UI has four panes, matching the wireframes: **connect** (the two nodes and the
fabric link), **hardware** (per-node facts), **models** (three cards: load / free / fetch / log), **running**
(endpoint, live counters, node memory).

The listener is bound to **one interface** — `tailscale ip -4` on the head, so `http://<tailnet-ip>:8099` only.
`deploy.sh` and the entrypoint fail closed: no tailnet address means no start (they never fall back to a LAN/WAN
interface), and `STACK_BIND` is accepted only for `100.64.0.0/10` (tailnet) or `127.0.0.0/8` (loopback). `0.0.0.0`
is refused. The LAN (`192.168.0.x`), the fabric (`10.10.10.x`) and the WAN never answer on 8099.

`wire harness` writes the three providers into `~/.dsh/profiles/web/cordis.patch.yml` and prints the YAML it wrote.

## API

| method | path | purpose |
|---|---|---|
| GET | `/api/state` | nodes, models, active, harness routes |
| GET | `/api/stats` | live node facts and per-model metrics |
| POST | `/api/models/{glm,qwen,deepseek}/prepare` | download weights and image on both Sparks |
| POST | `/api/models/{id}/load` | start both ranks, refuse if another model serves |
| POST | `/api/models/{id}/unload` | stop both ranks |
| GET | `/api/models/{id}/logs?lines=120` | tail of the running job |
| POST | `/api/harness/apply` | write the dsh provider routes into the profile layer |

All `/api` routes require `Authorization: Bearer $(cat ~/spark-stack-state/token)`; the page itself carries no token.

## Notes

- **Qwen has no Mia checkpoint**: "TensorFold by Mia" is the recipe and image publisher; the weights are NVIDIA's
  NVFP4 build. The registry pins `nvidia/Qwen3.8-Flash-Next-NVFP4@fc694b54`.
- **DeepSeek is the 1017 2.7 bpw pair**: `0xSero/DeepSeek-V4.1-Flash-1017-EXL3-2.7bpw` (199 GB, 2.70 bpw) served by
  sudoingX's launcher at `cc59c360` with the pinned `ghcr.io/sybil-solutions/deepseek-v4.1-flash-exl3-spark` image.
  The Engram tables are not in the checkpoint: `scripts/fetch-dsv41-1017.py` pulls DeepSeek's two shards at
  `1bae0e10` into the same directory and verifies `MANIFEST.sha256`. Nothing is fetched until the card's `fetch` is
  pressed.
- Weights are prepared by each recipe's own `prepare.sh`/`download.sh`, which verify hashes and copy to the worker
  over the fabric.
- The controller never writes into the recipe trees; state and job logs live in `~/spark-stack-state/`.

## Verified on spark-2384 (head) + spark-557f (worker)

```
$ TOKEN=$(cat ~/spark-stack-state/token) bash tests/e2e.sh http://$(tailscale ip -4 | head -1):8099 glm
== state
  head   spark-2384 reachable 11 GiB
  worker 10.10.10.13 reachable 14 GiB
== load glm
  endpoint up
== completion
  model=GLM-5.3-Flash-EXL3 completion_tokens=85 text='stack ok'
== stats
  active: glm
  up: glm metrics
== unload glm
  down
PASS
```

Qwen passes the same way (`tests/e2e.sh … qwen`: 31 tokens, `stack ok`, unload down).

`tests/harness.sh` passes too: `dsh 0.2.1-rc.2` is in the image, the patch keeps the user's existing routes and adds
`spark-glm`, `spark-qwen`, `spark-deepseek`.

## Weights

| model | on the head | on the worker |
|---|---|---|
| GLM | cached | cached |
| Qwen | downloading (`~/qwen-dl.log`) | cached |
| DeepSeek | not fetched (manual) | not fetched (manual) |

`scripts/chain-after-downloads.sh` runs on the head: when the Qwen download ends it calls `prepare` (verify + copy to
the worker over the fabric) and stops there. **DeepSeek is not fetched automatically** — press `fetch` on its card.
Its download is 462 GB resident: 240 GB of EXL3 banks plus 222 GB of base shards, ~203 GB of which arrives as
`.partNN` files that reassemble into the two Engram shards and are deleted; peak disk while reassembling is ~665 GB.

## A local image can be a trap

`prepare.sh` accepts any locally built image whose tag matches, and only checks the tag — it does not know whether
that build loads. On 2026-10-20 an image rebuilt that morning passed `prepare.sh` ("kernel set present") and then
died at load with `tensorfold-native … (LinkReadFailed)`, while the six-day-old build still served. Two ways out,
both wired into the UI:

```
POST /api/models/{id}/prepare?mode=pull      # PULL=1: take the published GHCR image instead of the local build
POST /api/models/{id}/prepare?mode=rebuild   # prepare.sh --rebuild: rebuild with the pinned patches
```
