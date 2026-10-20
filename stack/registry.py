"""Model registry for the Spark stack: what to download, how to serve it, where it answers."""

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["tensorfold", "vllm"]


@dataclass(frozen=True)
class Model:
    key: str
    name: str
    recipe: str                 # directory under vendor/ on the head
    kind: Kind
    checkpoint: str
    revision: str
    port: int
    api_id: str
    context: int
    vision: bool
    size_gib: float
    prepare: str                # shell command run inside the recipe dir, head-side
    start: str
    stop: str
    api_key_file: str | None = None
    notes: str = ""
    env: dict[str, str] = field(default_factory=dict)

    def command(self, script: str, worker: str, port: int) -> str:
        """WORKER plus this model's env, then the script: stop needs the same surroundings as start."""
        env = " ".join(f"{k}={v}" for k, v in self.env.items())
        return f"WORKER={worker} {env} {script}".replace("{port}", str(port))

    def load_command(self, worker: str, port: int) -> str:
        return self.command(self.start, worker, port)

    def stop_command(self, worker: str, port: int) -> str:
        return self.command(self.stop, worker, port)


MODELS: dict[str, Model] = {
    "glm": Model(
        key="glm",
        name="GLM-5.3-Flash",
        recipe="glm",
        kind="tensorfold",
        checkpoint="Mia-AiLab/GLM-5.3-Flash-EXL3-4bpw-TensorFold",
        revision="078455ffe6472f9a52fbc1139f58b9db2881b25c",
        port=8888,
        api_id="GLM-5.3-Flash-EXL3",
        context=1_048_576,
        vision=True,
        size_gib=176,
        prepare="./scripts/prepare.sh",
        start="PREPARE=0 PORT={port} ./start.sh",
        stop="./stop.sh",
        notes="MiaAI-Lab two-Spark recipe, TensorFold v0.6.0, DFlash2 drafter",
    ),
    "qwen": Model(
        key="qwen",
        name="Qwen3.8-Flash-Next",
        recipe="qwen",
        kind="tensorfold",
        checkpoint="nvidia/Qwen3.8-Flash-Next-NVFP4",
        revision="fc694b54",
        port=8889,
        api_id="Qwen3.8-Flash-Next",
        context=1_048_576,
        vision=True,
        size_gib=124,
        prepare="./scripts/prepare.sh",
        start="PREPARE=0 PORT={port} ./start.sh",
        stop="./stop.sh",
        notes="MiaAI-Lab two-Spark recipe, TensorFold zig-flashnext, NVFP4",
    ),
    "deepseek": Model(
        key="deepseek",
        name="DeepSeek-V4.1-Flash-1017",
        recipe="dsv41-1017",
        kind="vllm",
        checkpoint="0xSero/DeepSeek-V4.1-Flash-1017-EXL3-2.7bpw",
        revision="e34ef55b",
        port=8000,
        api_id="deepseek-v4.1-flash-1017",
        context=262_144,
        vision=False,           # the launcher is text-only by default (TEXT_ONLY=1)
        size_gib=402,           # 199 GB checkpoint + 203 GB of DeepSeek's own Engram shards
        prepare="MODEL_DIR=$HOME/models/dsv41-1017-exl3 python3 ~/spark-stack/scripts/fetch-dsv41-1017.py",
        start="scripts/launch/dsv41_tp2.sh up",
        stop="scripts/launch/dsv41_tp2.sh down",
        notes="sudoxing launcher cc59c360 + ghcr.io/sybil-solutions/deepseek-v4.1-flash-exl3-spark@sha256:5817414f",
        env={
            "IMAGE": "ghcr.io/sybil-solutions/deepseek-v4.1-flash-exl3-spark@sha256:5817414f92e9bec0406dfc4575e16b282209f0cb1ead16fe53d51bac7ac9e9e4",
            "MODEL_DIR": "$HOME/models/dsv41-1017-exl3",
            "SERVED": "deepseek-v4.1-flash-1017",
            "HEAD": "localhost",        # the launcher runs on the head and ssh's to both ranks
            "FAB_IF": "enp1s0f1np1",
            "FAB_HCA": "rocep1s0f1",
        },
    ),
}
