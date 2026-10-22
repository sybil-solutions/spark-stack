"""Model registry: four models, each with the fastest public recipe for one Spark and/or two Sparks.

Every recipe here follows the MiaAI-Lab TensorFold convention: `scripts/prepare.sh` downloads the weights and the image
(and copies them to the worker for two-Spark recipes), `PREPARE=0 PORT=<port> ./start.sh` serves, `./stop.sh` stops.
Two-Spark recipes read WORKER (ssh target of the second Spark over the CX7 link) from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Recipe:
    sparks: int                  # 1 or 2
    dir: str                     # directory under vendor/ on the head
    repo: str                    # git URL
    commit: str                  # pinned commit
    weights: tuple[str, ...]     # Hugging Face repos it downloads (disk accounting and progress)
    size_gib: float              # download size on the head
    decode: str                  # measured single-user speed, from the recipe's own README
    env: dict[str, str] = field(default_factory=dict)
    hf_cache: str = ".cache/huggingface"   # Hugging Face cache it uses, relative to $HOME
    prepare: str = "scripts/prepare.sh"
    start: str = "PORT={port} ./start.sh"   # PREPARE=auto: the recipe re-runs its own prepare unless its marker matches
    stop: str = "./stop.sh"

    def command(self, script: str, worker: str, port: int) -> str:
        # HOST=127.0.0.1: the model's own API answers on the Spark only; the tailnet reaches it through the stack's /v1.
        # HF_XET_HIGH_PERFORMANCE=1: more parallel Xet transfers. Xet stays on: the Hub serves files over 50 GB
        # (Qwen3.8-Flash-Next's 54 GB MTP file) only through Xet.
        env = {"HOST": "127.0.0.1", "HF_XET_HIGH_PERFORMANCE": "1", **self.env}
        if self.sparks == 2:
            env["WORKER"] = worker
        prefix = " ".join(f"{k}={v}" for k, v in env.items())
        return f"{prefix} {script}".strip().replace("{port}", str(port))


@dataclass(frozen=True)
class Model:
    key: str
    name: str
    blurb: str
    port: int
    api_id: str                  # the id the server reports in /v1/models
    context: int
    vision: bool
    recipes: dict[int, Recipe]   # sparks -> recipe

    def recipe(self, sparks: int) -> Recipe:
        return self.recipes[sparks]


MIA = "https://github.com/MiaAI-Lab"

MODELS: dict[str, Model] = {
    "qwen-next": Model(
        key="qwen-next",
        name="Qwen3.8-Flash-Next",
        blurb="Alibaba's 180B mixture-of-experts (6B active). Fast all-rounder with vision; NVIDIA's 4-bit build.",
        port=8889,
        api_id="Qwen3.8-Flash-Next",
        context=262_144,
        vision=True,
        recipes={
            1: Recipe(
                sparks=1,
                dir="qwen-next-1x",
                repo=f"{MIA}/Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold.git",
                commit="656879fd752749bbd11a6e1a065a326c00940911",
                weights=("nvidia/Qwen3.8-Flash-Next-FP4-Local",),
                size_gib=96,
                decode="~50 tok/s prose, ~68 code",
            ),
            2: Recipe(
                sparks=2,
                dir="qwen-next-2x",
                repo=f"{MIA}/Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold.git",
                commit="6730319ed948cc88b525bbbdc9d309451a863834",
                weights=("nvidia/Qwen3.8-Flash-Next-NVFP4",),
                size_gib=124,
                decode="67 tok/s prose",
                env={"WORKER_WEIGHTS": "copy", "CONTEXT": "262144"},
            ),
        },
    ),
    "qwen-27b": Model(
        key="qwen-27b",
        name="Qwen3.8-27B",
        blurb="Dense 27B with vision. Small and quick; speculative decoding with the DFlash2 drafter.",
        port=8891,
        api_id="Qwen3.8-27B",
        context=262_144,
        vision=True,
        recipes={
            1: Recipe(
                sparks=1,
                dir="qwen-27b-1x",
                repo=f"{MIA}/Qwen3.8-27B-DGX-Spark-TensorFold.git",
                commit="c11ff138871cf43d12898d79dbfa4dc952d17ca4",
                weights=("TensorFold/Qwen3.8-27B-MLX-4bit", "z-lab/Qwen3.8-27B-DFlash2"),
                env={"MODEL_ID": "TensorFold/Qwen3.8-27B-MLX-4bit"},   # the repo's current name (Vontra/... redirects)
                size_gib=19,
                decode="58 tok/s prose, 137 code",
            ),
        },
    ),
    "glm": Model(
        key="glm",
        name="GLM-5.3-Flash",
        blurb="Z.ai's 320B mixture-of-experts (18B active). Strong at code and agents; needs both Sparks.",
        port=8888,
        api_id="GLM-5.3-Flash-EXL3",
        context=262_144,
        vision=True,
        recipes={
            2: Recipe(
                sparks=2,
                dir="glm-2x",
                repo=f"{MIA}/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold.git",
                commit="3fd60dfc836fab0f1ed0fd4f0afaf2a1d62b2aab",
                weights=("Mia-AiLab/GLM-5.3-Flash-EXL3-4bpw-TensorFold", "incoai/GLM-5.3-Flash-DFlash2"),
                size_gib=166,
                decode="60 tok/s prose, 115 structured",
                env={"WORKER_WEIGHTS": "copy", "CONTEXT": "262144"},
            ),
        },
    ),
    "deepseek": Model(
        key="deepseek",
        name="DeepSeek-V4.1-Flash-1017",
        blurb="DeepSeek's newest Flash model. The smartest of the four; needs both Sparks and ~500 GB of disk.",
        port=8890,
        api_id="DeepSeek-V4.1-Flash-1017",
        context=262_144,
        vision=True,
        recipes={
            2: Recipe(
                sparks=2,
                dir="deepseek-2x",
                repo=f"{MIA}/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks.git",
                commit="370daf0cc38f348516a98ebab38f6053858eab1f",
                weights=("Mia-AiLab/DeepSeek-V4.1-Flash-1017-EXL3-EAQ-v1-2.9-bpw", "deepseek-ai/DeepSeek-V4.1-Flash-1017"),
                size_gib=385,
                decode="75 tok/s prose, 110 code",
                env={"CONTEXT": "262144", "HF_CACHE": "$HOME/.cache/spark-stack-hf"},
                hf_cache=".cache/spark-stack-hf",
            ),
        },
    ),
}

# Downloaded automatically by install.sh, so the boxes are useful before anyone clicks anything.
DEFAULT_MODEL = ("qwen-next", 2)
