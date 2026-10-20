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

    def load_command(self, worker: str, port: int) -> str:
        env = " ".join(f"{k}={v}" for k, v in self.env.items())
        return f"WORKER={worker} {env} {self.start}".replace("{port}", str(port))


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
        name="DeepSeek-V4.1-Flash",
        recipe="deepseek",
        kind="vllm",
        checkpoint="0xSero/DeepSeek-V4.1-Flash-Spark",
        revision="main",
        port=8000,
        api_id="deepseek-v4.1-flash",
        context=262_144,
        vision=True,
        size_gib=462,  # 240 GB EXL3 banks + 222 GB base/Engram shards; ~665 GB peak while reassembling
        prepare="./scripts/download.sh && ./scripts/copy-to-peer.sh",
        start="./scripts/launch.sh",
        stop="./scripts/launch.sh stop",
        api_key_file="~/.dsv41-two-sparks/api_key",
        notes="0xSero two-Spark recipe, EXL3 2.77 bpw, vLLM + B12X kernels",
    ),
}
