"""Spark stack control plane: download, start, stop, and report on four models over one or two DGX Sparks.

Also serves one OpenAI-compatible front door at /v1 that forwards to whichever model is running, so the harnesses
(DeepSeek harness, OpenCode) are configured once and follow the model picked in the UI.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .registry import MODELS
from .runner import Host

STATE_DIR = Path(os.environ.get("STACK_STATE_DIR", "/var/lib/spark-stack"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"

HEAD = os.environ.get("STACK_HEAD", "localhost")
HEAD_NAME = os.environ.get("STACK_HEAD_NAME", "head")
WORKER = os.environ.get("STACK_WORKER", "")                 # user@<worker CX7 address>; empty: one Spark
WORKER_NAME = os.environ.get("STACK_WORKER_NAME", "worker")
RECIPE_ROOT = os.environ.get("STACK_RECIPES", "~/spark-stack/vendor")
PROBE_HOST = os.environ.get("STACK_PROBE_HOST", "127.0.0.1")
PUBLIC_HOST = os.environ.get("STACK_PUBLIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("STACK_PORT", "8099"))
ALIAS = "spark"   # model id that always means "whatever is running"

# Folders that hold model weights, relative to $HOME on each Spark. Only these can be listed or removed.
HUBS = (".cache/huggingface/hub", ".cache/spark-stack-hf/hub")   # the second is DeepSeek's NFS-shared, weights-only cache
EXTRA_DIRS = (".cache/spark-stack-hf/tensorfold-dsv41", ".cache/dsv41-tensorfold", ".cache/huggingface/xet")


def resolve_token() -> str:
    """Remote /api calls need this token (the controller holds the host's ssh key); it lives only in the 0600 state dir."""
    path = STATE_DIR / "token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


TOKEN = resolve_token()
app = FastAPI(title="spark stack")
host = Host(HEAD, STATE_DIR)
_state_lock = threading.Lock()


@app.middleware("http")
async def require_token(request: Request, call_next):
    """The page on the Spark itself needs no key; every other tailnet peer must send the token from the state dir."""
    if request.url.path.startswith("/api"):
        client = request.client.host if request.client else ""
        if client in LOCAL_SOURCES:
            return await call_next(request)
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied, TOKEN):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


LOCAL_SOURCES = {"127.0.0.1", os.environ.get("STACK_BIND", "127.0.0.1")}   # a request from the Spark's own address


# ------------------------------------------------------------------ state
def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if isinstance(state.get("active"), str) or "models" not in state:   # pre-variant state file
            return {"active": None, "models": {}}
        return state
    return {"active": None, "models": {}}


def update_state(fn) -> dict:
    with _state_lock:
        state = load_state()
        fn(state)
        STATE_FILE.write_text(json.dumps(state, indent=2))
        return state


def set_model(key: str, **fields) -> None:
    update_state(lambda s: s["models"].setdefault(key, {}).update(fields))


def set_active(key: str | None, sparks: int | None = None) -> None:
    def apply(s):
        s["active"] = {"key": key, "sparks": sparks} if key else None
    update_state(apply)


def two_sparks() -> bool:
    return bool(WORKER)


def model_or_404(key: str):
    m = MODELS.get(key)
    if not m:
        raise HTTPException(404, "unknown model")
    return m


def recipe_or_400(m, sparks: int):
    if sparks not in m.recipes:
        raise HTTPException(400, f"{m.name} has no {sparks}-Spark recipe")
    if sparks == 2 and not two_sparks():
        raise HTTPException(400, "this stack was installed on one Spark; two-Spark recipes need a worker")
    return m.recipe(sparks)


# ------------------------------------------------------------------ probes
def http_json(url: str, timeout: float = 3.0) -> tuple[int, dict | None, str]:
    argv = ["curl", "-sS", "-m", str(timeout), "-o", "-", "-w", "\n%{http_code}", url]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 3)
    except subprocess.TimeoutExpired:
        return 0, None, "timeout"
    body, _, code = p.stdout.rpartition("\n")
    try:
        return int(code or 0), json.loads(body), body
    except json.JSONDecodeError:
        return int(code or 0), None, body


def endpoint_up(port: int) -> bool:
    code, body, _ = http_json(f"http://{PROBE_HOST}:{port}/v1/models", timeout=2)
    return code == 200 and body is not None


NODE_PROBE = (
    "printf 'mem_gib='; awk '/MemAvailable/ {printf \"%d\", $2/1048576}' /proc/meminfo; "
    "printf '\\nmem_total_gib='; awk '/MemTotal/ {printf \"%d\", $2/1048576}' /proc/meminfo; "
    "printf '\\nutil='; nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1; "
    "printf '\\npower_w='; nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null | head -1; "
    "printf '\\ntemp_c='; nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1; "
    "printf '\\ndisk_free_gb='; df -B1 --output=avail \"$HOME\" | tail -1 | awk '{printf \"%d\", $1/1e9}'; "
    "printf '\\ndisk_total_gb='; df -B1 --output=size \"$HOME\" | tail -1 | awk '{printf \"%d\", $1/1e9}'; "
    "printf '\\nload='; cut -d' ' -f1 /proc/loadavg; "
    "printf 'host='; hostname; echo"
)


def node_facts(target: str) -> dict:
    rc, out = host.run(NODE_PROBE, timeout=15, target=target)
    if rc != 0:
        return {"reachable": False, "detail": out[-200:]}
    facts = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"reachable": True, **{k.strip(): v.strip() for k, v in facts.items()}}


def nodes() -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:
        head = pool.submit(node_facts, HEAD)
        worker = pool.submit(node_facts, WORKER) if WORKER else None
        out = {"head": {"name": HEAD_NAME, "role": "head", **head.result()}}
        if worker:
            out["worker"] = {"name": WORKER_NAME, "role": "worker", "fabric": WORKER.split("@")[-1], **worker.result()}
    return out


# ------------------------------------------------------------------ disk
DISK_SCAN = (
    "cd \"$HOME\" || exit 1; "
    f"for d in {' '.join(h + '/models--*' for h in HUBS)} {' '.join(EXTRA_DIRS)}; do "
    "  [ -d \"$d\" ] || continue; "
    "  b=$(du -sb \"$d\" 2>/dev/null | cut -f1); "
    "  inc=$(find \"$d\" -name '*.incomplete' -print -quit 2>/dev/null); "
    "  printf '%s\\t%s\\t%s\\n' \"$d\" \"$b\" \"${inc:+partial}\"; "
    "done"
)
_disk_cache: dict[str, tuple[float, list[dict]]] = {}


def repo_of(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if not name.startswith("models--"):
        return None
    return name.removeprefix("models--").replace("--", "/", 1)


def owners(repo: str | None, path: str) -> list[str]:
    """The models (key:sparks) whose recipe downloads this folder."""
    hits = []
    for m in MODELS.values():
        for sparks, r in m.recipes.items():
            if (repo and repo.lower() in (w.lower() for w in r.weights)) or (not repo and "dsv41" in path and m.key == "deepseek"):
                hits.append(f"{m.key}:{sparks}")
    return hits


def scan_disk(target: str, max_age: float = 15.0) -> list[dict]:
    cached = _disk_cache.get(target)
    if cached and time.time() - cached[0] < max_age:
        return cached[1]
    rc, out = host.run(DISK_SCAN, timeout=60, target=target)
    entries = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            repo = repo_of(parts[0])
            entries.append({"path": parts[0], "repo": repo, "bytes": int(parts[1]),
                            "partial": len(parts) > 2 and parts[2] == "partial",
                            "models": owners(repo, parts[0])})
    _disk_cache[target] = (time.time(), entries)
    return entries


def downloaded_gib(recipe, entries: list[dict]) -> float:
    want = {w.lower() for w in recipe.weights}
    return sum(e["bytes"] for e in entries if e["repo"] and e["repo"].lower() in want) / 2**30


def complete(recipe, entries: list[dict]) -> bool:
    want = {w.lower() for w in recipe.weights}
    found = {e["repo"].lower(): e for e in entries if e["repo"]}
    return all(w in found and not found[w]["partial"] for w in want)


# ------------------------------------------------------------------ status
def derive_status(entry: dict, up: bool, job) -> str:
    """The endpoint is the truth: a finished load that answers is running, a finished stop that is down is stopped."""
    status = entry.get("status", "absent")
    if up:
        return "running"
    if job and not job.done:
        return {"prepare": "downloading", "load": "starting", "stop": "stopping"}.get(job.action, status)
    if job and job.done:
        if job.action == "prepare":
            return "ready" if job.rc == 0 else "error"
        if job.action == "load" and job.rc not in (0, None):
            return "error"
        if job.action == "stop":
            return "ready"
    return "ready" if status in ("running", "starting", "stopping") else status


def recipe_path(recipe) -> str:
    return f"{RECIPE_ROOT}/{recipe.dir}"


def busy(key: str) -> bool:
    job = host.job(key)
    return bool(job and not job.done)


def active_entry() -> dict | None:
    return load_state().get("active")


@app.get("/api/state")
def api_state() -> dict:
    state = load_state()
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_nodes = pool.submit(nodes)
        fut_disk = pool.submit(scan_disk, HEAD)
        ups = {k: pool.submit(endpoint_up, m.port) for k, m in MODELS.items()}
        node_info, disk = fut_nodes.result(), fut_disk.result()
        up = {k: f.result() for k, f in ups.items()}
    models = []
    for key, m in MODELS.items():
        entry = state["models"].get(key, {})
        job = host.job(key)
        variants = []
        for sparks, r in sorted(m.recipes.items()):
            variants.append({
                "sparks": sparks,
                "available": sparks == 1 or two_sparks(),
                "decode": r.decode,
                "size_gib": r.size_gib,
                "downloaded_gib": round(downloaded_gib(r, disk), 1),
                "complete": complete(r, disk),
                "repo": r.repo.removesuffix(".git"),
                "commit": r.commit[:8],
                "weights": list(r.weights),
            })
        models.append({
            "key": key, "name": m.name, "blurb": m.blurb, "port": m.port, "api_id": m.api_id,
            "context": m.context, "vision": m.vision,
            "status": derive_status(entry, up[key], job),
            "sparks": entry.get("sparks"),
            "job": None if not job else {"action": job.action, "started": job.started, "done": job.done, "rc": job.rc},
            "last": entry.get("last"),
            "up": up[key],
            "variants": variants,
        })
    active = state.get("active")
    if active and not up.get(active["key"]) and not busy(active["key"]):
        active = None
    return {"active": active, "models": models, "nodes": node_info, "two_sparks": two_sparks(),
            "endpoint": f"http://{PUBLIC_HOST}:{PORT}/v1", "alias": ALIAS, "now": time.time()}


@app.post("/api/models/{key}/prepare")
def api_prepare(key: str, sparks: int = 2) -> dict:
    m = model_or_404(key)
    r = recipe_or_400(m, sparks)
    if busy(key):
        raise HTTPException(409, f"{m.name}: {host.job(key).action} already running")
    cmd = f"cd {recipe_path(r)} && {r.command(r.prepare, WORKER, m.port)}"
    host.spawn(key, "prepare", cmd, timeout=12 * 3600)
    set_model(key, status="downloading", sparks=sparks, last=f"download started {time.strftime('%H:%M:%S')}")
    _disk_cache.clear()
    return {"ok": True}


@app.post("/api/models/{key}/load")
def api_load(key: str, sparks: int = 2) -> dict:
    m = model_or_404(key)
    r = recipe_or_400(m, sparks)
    active = active_entry()
    if active and active["key"] != key and (endpoint_up(MODELS[active["key"]].port) or busy(active["key"])):
        raise HTTPException(409, f"{MODELS[active['key']].name} is running; stop it first")
    if busy(key):
        raise HTTPException(409, f"{m.name}: {host.job(key).action} already running")
    if endpoint_up(m.port):
        set_active(key, load_state()["models"].get(key, {}).get("sparks", sparks))
        return {"ok": True, "already": True}
    # The recipe's start.sh downloads what is missing (PREPARE=auto) when the weights are not complete yet.
    start = r.start if complete(r, scan_disk(HEAD, max_age=0)) else r.start.replace("PREPARE=0 ", "")
    cmd = f"cd {recipe_path(r)} && {r.command(start, WORKER, m.port)}"
    host.spawn(key, "load", cmd, timeout=4 * 3600)
    set_active(key, sparks)
    set_model(key, status="starting", sparks=sparks, last=f"start {time.strftime('%H:%M:%S')} on {sparks} Spark(s)")
    return {"ok": True}


@app.post("/api/models/{key}/unload")
def api_unload(key: str) -> dict:
    m = model_or_404(key)
    sparks = load_state()["models"].get(key, {}).get("sparks") or max(m.recipes)
    r = m.recipe(sparks if sparks in m.recipes else max(m.recipes))
    job = host.job(key)
    if job and not job.done and job.action != "load":
        raise HTTPException(409, f"{m.name}: {job.action} already running")
    host.cancel(key)   # a start still waiting for the server: stop it, then run the recipe's stop
    cmd = f"cd {recipe_path(r)} && {r.command(r.stop, WORKER, m.port)}"
    host.spawn(key, "stop", cmd, timeout=900)
    set_model(key, status="stopping")
    active = active_entry()
    if active and active["key"] == key:
        set_active(None)
    return {"ok": True}


@app.get("/api/models/{key}/logs")
def api_logs(key: str, lines: int = 80) -> dict:
    job = host.job(key)
    if not job:
        return {"lines": []}
    return {"lines": host.tail(job.log, lines), "done": job.done, "rc": job.rc, "action": job.action}


# ------------------------------------------------------------------ disk API
@app.get("/api/disk")
def api_disk() -> dict:
    targets = {"head": HEAD, **({"worker": WORKER} if WORKER else {})}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {role: pool.submit(scan_disk, t, 5.0) for role, t in targets.items()}
        return {role: f.result() for role, f in futs.items()}


@app.post("/api/disk/remove")
async def api_disk_remove(request: Request) -> dict:
    body = await request.json()
    path = str(body.get("path", ""))
    in_hub = any(path.startswith(f"{h}/models--") and "/" not in path.removeprefix(f"{h}/") for h in HUBS)
    if ".." in path or not (in_hub or path in EXTRA_DIRS):
        raise HTTPException(400, "only model folders listed under Disk can be removed")
    repo = repo_of(path)
    for owner in owners(repo, path):
        key = owner.split(":")[0]
        if await run_in_threadpool(endpoint_up, MODELS[key].port) or busy(key):
            raise HTTPException(409, f"{MODELS[key].name} is using this folder; stop it first")
    roles = body.get("nodes") or ["head", "worker"]
    targets = [t for role, t in (("head", HEAD), ("worker", WORKER)) if t and role in roles]
    results = {}
    for t in targets:
        rc, out = await run_in_threadpool(host.run, f"rm -rf -- \"$HOME\"/{shlex.quote(path)}", 600, None, t)
        results[t] = "removed" if rc == 0 else out[-200:]
    _disk_cache.clear()
    return {"ok": True, "results": results}


# ------------------------------------------------------------------ stats
LAST_COUNTER: dict[str, tuple[float, float]] = {}
METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$")


def token_rate(key: str, text: str) -> float | None:
    total = None
    for line in text.splitlines():
        hit = METRIC_RE.match(line.strip())
        if hit and hit.group(1).endswith("generation_tokens_total"):
            total = (total or 0.0) + float(hit.group(2))
    if total is None:
        return None
    now, last = time.time(), LAST_COUNTER.get(key)
    LAST_COUNTER[key] = (now, total)
    if not last or now <= last[0]:
        return None
    return round(max(0.0, (total - last[1]) / (now - last[0])), 1)


@app.get("/api/stats")
def api_stats() -> dict:
    out = {"nodes": nodes(), "models": {}}
    for key, m in MODELS.items():
        up = endpoint_up(m.port)
        entry = {"up": up}
        if up:
            code, _, raw = http_json(f"http://{PROBE_HOST}:{m.port}/metrics", timeout=2)
            if code == 200:
                entry["tok_per_sec"] = token_rate(key, raw)
        out["models"][key] = entry
    return out


# ------------------------------------------------------------------ /v1 front door
def model_list() -> dict:
    data = [{"id": ALIAS, "object": "model", "owned_by": "spark-stack"}]
    data += [{"id": m.api_id, "object": "model", "owned_by": "spark-stack"} for m in MODELS.values()]
    return {"object": "list", "data": data}


def resolve_target(requested: str | None):
    """Pick the model a request goes to: the one it names, or the running one for the alias."""
    running = [m for m in MODELS.values() if endpoint_up(m.port)]
    if requested and requested != ALIAS:
        named = next((m for m in MODELS.values() if requested in (m.api_id, m.key, m.name)), None)
        if named:
            if named in running:
                return named, None
            hint = f" ({running[0].name} is running; pick it or '{ALIAS}')" if running else ""
            return None, f"{named.name} is not running. Start it in Spark Stack{hint}."
    if running:
        return running[0], None
    return None, "No model is running. Open Spark Stack and press Start on a model."


def openai_error(message: str, status: int = 503) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "model_not_running", "code": status}}, status_code=status)


@app.get("/v1/models")
async def v1_models() -> dict:
    return model_list()


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def v1_proxy(path: str, request: Request):
    raw = await request.body()
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
    requested = payload.get("model") if isinstance(payload, dict) else None
    target, error = await run_in_threadpool(resolve_target, requested)
    if not target:
        return openai_error(error)
    if isinstance(payload, dict):
        payload["model"] = target.api_id
        raw = json.dumps(payload).encode()
    url = f"http://{PROBE_HOST}:{target.port}/v1/{path}"
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))
    upstream = await client.send(client.build_request(request.method, url, content=raw, headers=headers,
                                                      params=request.query_params), stream=True)

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    passthrough = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "cache-control")}
    return StreamingResponse(body(), status_code=upstream.status_code, headers=passthrough)


# ------------------------------------------------------------------ page
@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("ui.html"))


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
