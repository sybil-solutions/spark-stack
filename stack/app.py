"""Spark stack control plane: downloads, launches, stops, and reports on three models over two DGX Sparks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import secrets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .registry import MODELS
from .runner import Host

STATE_DIR = Path(os.environ.get("STACK_STATE_DIR", "/var/lib/spark-stack"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"

HEAD = os.environ.get("STACK_HEAD", "localhost")
WORKER = os.environ.get("STACK_WORKER", "valentine@10.10.10.13")
WORKER_NAME = os.environ.get("STACK_WORKER_NAME", "spark-557f")
WORKER_PROBE = os.environ.get("STACK_WORKER_PROBE", WORKER)  # keeps the worker's user name
RECIPE_ROOT = os.environ.get("STACK_RECIPES", "~/spark-stack/vendor")
API_KEY = os.environ.get("STACK_API_KEY", "")
PROBE_HOST = os.environ.get("STACK_PROBE_HOST", "127.0.0.1")

def resolve_token() -> str:
    """Every /api call needs this token: the controller holds the host's ssh keys, so it must not be open."""
    token = os.environ.get("STACK_TOKEN", "").strip()
    if token:
        return token
    path = STATE_DIR / "token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


TOKEN = resolve_token()

app = FastAPI(title="spark stack")


@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path.startswith("/api"):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        supplied = supplied or request.query_params.get("token", "")
        if not secrets.compare_digest(supplied, TOKEN):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)
host = Host(HEAD, STATE_DIR)


# ------------------------------------------------------------------ state
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"active": None, "models": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def model_state(key: str) -> dict:
    return load_state()["models"].get(key, {"status": "absent"})


def set_model_state(key: str, **fields) -> None:
    state = load_state()
    entry = state["models"].setdefault(key, {"status": "absent"})
    entry.update(fields)
    entry["updated"] = time.time()
    save_state(state)


def set_active(key: str | None) -> None:
    state = load_state()
    state["active"] = key
    save_state(state)


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
    code, body, _ = http_json(f"http://{PROBE_HOST}:{port}/v1/models")
    return code == 200 and body is not None


def node_facts(target: str) -> dict:
    cmd = (
        "printf 'mem_gib='; awk '/MemAvailable/ {printf \"%d\", $2/1048576}' /proc/meminfo; "
        "printf '\\nutil='; nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1; "
        "printf '\\ndisk='; df -B1 --output=avail / | tail -1 | awk '{printf \"%d\", $1/1e9}'; "
        "printf '\\nload='; cut -d' ' -f1 /proc/loadavg; echo"
    )
    rc, out = host.run(cmd, timeout=15, target=target)
    facts: dict[str, str] = {}
    if rc != 0:
        return {"reachable": False, "detail": out[-200:]}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            facts[k.strip()] = v.strip()
    return {"reachable": True, **facts}


# ------------------------------------------------------------------ actions
def derive_status(status: str, up: bool, job) -> str:
    """The endpoint is the truth: a finished load that answers is running, a finished stop that is down is ready."""
    if up:
        return "running"
    if status == "unloading" and (not job or job.done):
        return "ready"          # a stop that leaves nothing listening did its job, whatever its exit code
    if status == "preparing" and job and job.done:
        return "ready" if job.rc == 0 else "error"
    if status == "loading" and job and job.done and job.rc not in (0, None):
        return "error"
    return status


def guard_exclusive(key: str) -> None:
    state = load_state()
    active = state.get("active")
    if active and active != key:
        if endpoint_up(MODELS[active].port):
            raise HTTPException(409, f"{MODELS[active].name} is serving; free it first")
        set_active(None)


def recipe_path(key: str) -> str:
    return f"{RECIPE_ROOT}/{MODELS[key].recipe}"


@app.get("/api/state")
def api_state() -> dict:
    state = load_state()
    active = state.get("active")
    models = []
    for key, m in MODELS.items():
        entry = state["models"].get(key, {})
        job = host.job(key)
        up = endpoint_up(m.port)
        models.append({
            "key": key,
            "name": m.name,
            "checkpoint": m.checkpoint,
            "kind": m.kind,
            "port": m.port,
            "api_id": m.api_id,
            "context": m.context,
            "vision": m.vision,
            "size_gib": m.size_gib,
            "notes": m.notes,
            "status": derive_status(entry.get("status", "absent"), up, job),
            "job": None if not job or job.done else {"action": job.action, "started": job.started},
            "last": entry.get("last"),
            "up": up,
        })
    with ThreadPoolExecutor(max_workers=2) as pool:
        head_facts = pool.submit(node_facts, HEAD)
        worker_facts = pool.submit(node_facts, WORKER_PROBE)
        nodes = {
            "head": {"name": os.environ.get("STACK_HEAD_NAME", "spark-2384"),
                     "fabric": os.environ.get("STACK_HEAD_FABRIC", "10.10.10.12"),
                     **head_facts.result()},
            "worker": {"name": WORKER_NAME, "fabric": WORKER.split("@")[-1],
                       **worker_facts.result()},
        }
    return {"active": active, "models": models, "nodes": nodes,
            "public_host": os.environ.get("STACK_PUBLIC_HOST", "127.0.0.1"),
            "harness": {"web": os.environ.get("STACK_HARNESS_URL", "http://127.0.0.1:3080"),
                        "providers": harness_routes()},
            "now": time.time()}


@app.post("/api/models/{key}/prepare")
def api_prepare(key: str) -> dict:
    m = MODELS.get(key)
    if not m:
        raise HTTPException(404, "unknown model")
    if host.job(key) and not host.job(key).done:
        raise HTTPException(409, f"{m.name}: {host.job(key).action} already running")
    if endpoint_up(m.port):
        raise HTTPException(409, f"{m.name} is already serving")
    cmd = f"cd {recipe_path(key)} && WORKER={WORKER} {m.prepare}"
    job = host.spawn(key, "prepare", cmd, timeout=6 * 3600)
    set_model_state(key, status="preparing", last=f"prepare started {time.strftime('%H:%M:%S')}")
    return {"ok": True, "job": job.log}


@app.post("/api/models/{key}/load")
def api_load(key: str) -> dict:
    m = MODELS.get(key)
    if not m:
        raise HTTPException(404, "unknown model")
    guard_exclusive(key)
    if host.job(key) and not host.job(key).done:
        raise HTTPException(409, f"{m.name}: {host.job(key).action} already running")
    if endpoint_up(m.port):
        set_active(key)
        set_model_state(key, status="running")
        return {"ok": True, "already": True}
    cmd = f"cd {recipe_path(key)} && WORKER={WORKER} {m.load_command(WORKER, m.port)}"
    job = host.spawn(key, "load", cmd, timeout=3 * 3600)
    set_active(key)
    set_model_state(key, status="loading", last=f"load started {time.strftime('%H:%M:%S')}")
    return {"ok": True, "job": job.log}


@app.post("/api/models/{key}/unload")
def api_unload(key: str) -> dict:
    m = MODELS.get(key)
    if not m:
        raise HTTPException(404, "unknown model")
    if host.job(key) and not host.job(key).done:
        raise HTTPException(409, f"{m.name}: {host.job(key).action} already running")
    cmd = f"cd {recipe_path(key)} && WORKER={WORKER} {m.stop}"
    job = host.spawn(key, "stop", cmd, timeout=900)
    set_model_state(key, status="unloading")
    if load_state().get("active") == key:
        set_active(None)
    return {"ok": True, "job": job.log}


@app.get("/api/models/{key}/logs")
def api_logs(key: str, lines: int = 80) -> dict:
    job = host.job(key)
    if not job:
        return {"lines": []}
    return {"lines": host.tail(job.log, lines), "done": job.done, "rc": job.rc}


@app.get("/api/stats")
def api_stats() -> dict:
    state = load_state()
    active = state.get("active")
    with ThreadPoolExecutor(max_workers=2) as pool:
        head_facts = pool.submit(node_facts, HEAD)
        worker_facts = pool.submit(node_facts, WORKER_PROBE)
        out = {"active": active, "models": {}, "nodes": {
            "head": head_facts.result(), "worker": worker_facts.result()}}
    for key, m in MODELS.items():
        up = endpoint_up(m.port)
        entry = {"up": up}
        if up:
            code, metrics, raw = http_json(f"http://{PROBE_HOST}:{m.port}/metrics", timeout=2)
            if code == 200 and metrics is None:
                entry["metrics"] = parse_metrics(raw)
                entry["tok_per_sec"] = token_rate(key, entry["metrics"])
        out["models"][key] = entry
    return out


LAST_COUNTER: dict[str, tuple[float, float]] = {}


def token_rate(key: str, metrics: dict) -> float | None:
    """Tokens a second from the generation counter, as a delta between polls."""
    total = None
    for name in ("tensorfold:generation_tokens_total", "generation_tokens_total", "vllm:generation_tokens_total"):
        if name in metrics:
            total = metrics[name]
            break
    if total is None:
        return None
    now, last = time.time(), LAST_COUNTER.get(key)
    LAST_COUNTER[key] = (now, total)
    if not last or now <= last[0]:
        return None
    return round(max(0.0, (total - last[1]) / (now - last[0])), 1)


METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$")


def parse_metrics(text: str) -> dict:
    want = ("tok", "token", "request", "tokens_per_second", "decode", "prefill")
    picked = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        hit = METRIC_RE.match(line.strip())
        if not hit:
            continue
        name, value = hit.group(1), hit.group(2)
        if any(w in name.lower() for w in want):
            try:
                picked[name] = float(value)
            except ValueError:
                continue
    return dict(list(sorted(picked.items()))[:24])


# ------------------------------------------------------------------ harness
def harness_routes() -> list[dict]:
    head = os.environ.get("STACK_PUBLIC_HOST", "127.0.0.1")
    return [{
        "provider": f"spark-{key}",
        "baseURL": f"http://{head}:{m.port}/v1",
        "models": [{"id": m.api_id, "contextWindow": m.context,
                    "input": ["text", "image"] if m.vision else ["text"]}],
    } for key, m in MODELS.items()]


@app.post("/api/harness/apply")
def api_harness_apply() -> dict:
    """Write the three providers into the dsh profile layer and return the snippet that was written."""
    path = Path(os.environ.get("DSH_PATCH", "/root/.dsh/profiles/web/cordis.patch.yml"))
    snippet = harness_yaml()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Created by the spark stack: loader patch layers for this dsh profile.\n")
    text = path.read_text()
    marker = "# >>> spark-stack providers"
    if marker in text:
        text = text[: text.index(marker)].rstrip() + "\n"
    text = text.rstrip() + "\n\n" + snippet
    path.write_text(text)
    return {"ok": True, "path": str(path), "snippet": snippet}


def harness_yaml() -> str:
    head = os.environ.get("STACK_PUBLIC_HOST", "127.0.0.1")
    lines = ["# >>> spark-stack providers (managed; rewritten by the stack UI)",
             "- id: llm-pi-ai-sparks",
             '  name: "@deepseek-ai/dsh-llm-pi-ai"',
             "  config:",
             "    providers:"]
    for key, m in MODELS.items():
        lines += [
            f"      spark-{key}:",
            f"        displayName: {m.name} (spark stack)",
            "        api: openai-completions",
            f"        baseURL: http://{head}:{m.port}/v1",
            "        models:",
            f"          - id: {m.api_id}",
            f"            name: {m.name}",
            f"            contextWindow: {m.context}",
            f"            maxTokens: 32768",
            "            input:",
            "              - text",
        ]
        if m.vision:
            lines.append("              - image")
        if m.api_key_file:
            lines.append(f"        apiKeyEnv: SPARK_{key.upper()}_API_KEY")
    lines.append("# <<< spark-stack providers")
    return "\n".join(lines) + "\n"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("ui.html"))


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})
