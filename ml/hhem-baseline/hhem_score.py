"""HHEM-2.1-open baseline scorer for the M7 checker runner (runs on the Colab GPU).

Input: the JSONL written by ``python -m app.evaluation checker-baselines --export-inputs``.
Output: a predictions JSONL that the runner's ``--scorer predictions`` accepts.

    python hhem_score.py --inputs inputs.jsonl --out predictions.jsonl --cache-dir /content/hhem

Pins and review (see the HHEM baseline write-up, m7-hhem-baseline.md under docs):
  * ``vectara/hallucination_evaluation_model`` at HHEM_REVISION. Its custom code
    (``configuration_hhem_v2.py``, ``modeling_hhem_v2.py``) was reviewed at that revision.
    It is loaded with ``trust_remote_code`` only after each file's SHA-256 matches the
    reviewed hash. The weights are verified by SHA-256 as well.
  * The reviewed code loads ``google/flan-t5-base`` with **no revision**. The scorer
    downloads flan-t5-base's config and tokenizer at FLAN_REVISION, verifies them, and
    points the HHEM config's ``foundation`` at that local copy. Loading then runs with
    ``HF_HUB_OFFLINE=1``, so no unpinned file can be fetched.
  * The input hashes from the export are verified and copied unchanged: the header's
    ``inputs_sha256`` and each row's ``input_sha256``.
  * Decision rule: the model's own two-label output, "supported" iff P(consistent) > 0.5
    (argmax; ties go to "hallucinated"). It is recorded as such; no threshold is selected.

All I/O and verification is standard library; torch and transformers are imported only
by the real backend, so the round trip is testable anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SCORER_VERSION = "1.0.0"
HHEM_REPO = "vectara/hallucination_evaluation_model"
HHEM_REVISION = "de9a5e6ba9ba49531a022dba17e35ffeb2a6fad8"
HHEM_FILES: dict[str, str] = {
    "config.json": "773139fe764fe20e146ab14e627b188ccafae35f93cf6f5258dc6c237016b870",
    "configuration_hhem_v2.py": "ec57fe344e3104d0d4a99b13d893529aac1e2bd69e83c2814235baf37cdcacc7",
    "modeling_hhem_v2.py": "f1fcfd46e85eef7b941efdcc854ad33e914d328126e28206ca965154e64da262",
    "model.safetensors": "634de18a38cf1e991c1acd0f7a9e0d30f7ea187fba42bb4798f862d3edd31e72",
}
REVIEWED_CODE = ("configuration_hhem_v2.py", "modeling_hhem_v2.py")
FLAN_REPO = "google/flan-t5-base"
FLAN_REVISION = "7bcac572ce56db69c1ea7c8af255c5d7c9672fc2"
FLAN_FILES: dict[str, str] = {
    "config.json": "7c1853dbfa0e4aac093eb109a358b6ab25fe86b7c15185a91322f0ed26f0f940",
    "special_tokens_map.json": "5c87151ef0f72a99d1f766a4c418bd2a1f90aaa30a8e22fe5eca9641daebb64f",
    "spiece.model": "d60acb128cf7b7f2536e8f38a5b18a05535c9e14c7a355904270e15b0945ea86",
    "tokenizer.json": "fe2ebbbbde2985be723e0ce18217853e4020c5e9d35bd07be2c27ab9d3ead57a",
    "tokenizer_config.json": "4c55124402e4ce48c7125d04b9af152a125eda9e7c80829f8f99f2ec69f3f68d",
}
OPERATING_POINT = {
    "rule": "supported iff P(consistent) > 0.5 (the model's own two-label argmax)",
    "threshold": 0.5,
    "provenance": "model output convention; not selected on any data",
}


class InputError(ValueError):
    """The inputs file is not an intact export from the M7 runner."""


class PinError(RuntimeError):
    """A downloaded or cached file does not match its pinned SHA-256."""


# ----------------------------------------------------------- digests (= the runner's)
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def input_digest(pair_id: str, claim: str, premise: str) -> str:
    """Identical to app.evaluation.checker_baselines.input_digest."""

    return _sha(json.dumps([pair_id, claim, premise], ensure_ascii=False).encode("utf-8"))


def inputs_digest(rows: Sequence[dict[str, str]]) -> str:
    """Identical to app.evaluation.checker_baselines.inputs_digest."""

    lines = sorted(
        f"{r['pair_id']}:{input_digest(r['pair_id'], r['claim'], r['premise'])}" for r in rows
    )
    return _sha("\n".join(lines).encode())


@dataclass(frozen=True)
class Inputs:
    header: dict[str, Any]
    rows: list[dict[str, str]]
    file_sha256: str


def read_inputs(path: Path) -> Inputs:
    raw = path.read_bytes()
    lines = [json.loads(x) for x in raw.decode("utf-8").splitlines() if x.strip()]
    if not lines or lines[0].get("kind") != "m7-checker-inputs":
        raise InputError(f"{path} is not an M7 checker-inputs export")
    header, rows = lines[0], lines[1:]
    if header.get("pairs") != len(rows) or not rows:
        raise InputError(f"{path} header announces {header.get('pairs')} pairs, has {len(rows)}")
    ids = [r["pair_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise InputError(f"{path} repeats pair ids")
    for r in rows:
        if r.get("input_sha256") != input_digest(r["pair_id"], r["claim"], r["premise"]):
            raise InputError(f"{r['pair_id']}: claim or premise changed after export")
    if header.get("inputs_sha256") != inputs_digest(rows):
        raise InputError(f"{path}: inputs_sha256 does not match the rows")
    return Inputs(header, rows, _sha(raw))


# ------------------------------------------------------------------ pinned files
def verify_file(path: Path, pinned: str) -> None:
    digest = _sha(path.read_bytes())
    if digest != pinned:
        raise PinError(f"{path.name}: sha256 {digest} != pinned {pinned}")


def fetch(cache_dir: Path) -> tuple[Path, Path]:
    """Download (if absent) and verify every pinned file. Returns (hhem_dir, flan_dir)."""

    dirs = []
    for repo, revision, files in (
        (HHEM_REPO, HHEM_REVISION, HHEM_FILES),
        (FLAN_REPO, FLAN_REVISION, FLAN_FILES),
    ):
        target = cache_dir / f"{repo.replace('/', '--')}--{revision}"
        target.mkdir(parents=True, exist_ok=True)
        for name, pinned in files.items():
            path = target / name
            if not path.is_file():
                url = f"https://huggingface.co/{repo}/resolve/{revision}/{name}"
                tmp = path.with_suffix(path.suffix + ".part")
                with urllib.request.urlopen(url, timeout=600) as resp, tmp.open("wb") as fh:  # noqa: S310
                    while chunk := resp.read(1 << 20):
                        fh.write(chunk)
                tmp.replace(path)
            verify_file(path, pinned)
        dirs.append(target)
    return dirs[0], dirs[1]


def verify_reviewed_code(hhem_dir: Path) -> None:
    """Run nothing from HHEM unless its code is byte-for-byte the reviewed code."""

    for name in REVIEWED_CODE:
        verify_file(hhem_dir / name, HHEM_FILES[name])
    extra = sorted(p.name for p in hhem_dir.glob("*.py") if p.name not in REVIEWED_CODE)
    if extra:
        raise PinError(f"unreviewed code in {hhem_dir}: {extra}")


# ---------------------------------------------------------------------- backends
class Backend(Protocol):
    inference: str

    def describe(self) -> dict[str, Any]: ...

    def score(self, premise: str, claim: str) -> float:
        """P(supported) for one pair."""
        ...


class HhemBackend:
    """The real model, on the GPU unless explicitly allowed otherwise."""

    inference = "hhem-2.1-open"

    def __init__(self, hhem_dir: Path, flan_dir: Path, device: str) -> None:
        verify_reviewed_code(hhem_dir)
        for name, pinned in FLAN_FILES.items():
            verify_file(flan_dir / name, pinned)
        verify_file(hhem_dir / "model.safetensors", HHEM_FILES["model.safetensors"])
        os.environ["HF_HUB_OFFLINE"] = "1"  # nothing unpinned can be fetched from here on
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import torch  # noqa: PLC0415 - GPU environment only
        import transformers  # noqa: PLC0415
        from transformers import AutoConfig, AutoModelForSequenceClassification  # noqa: PLC0415

        config = AutoConfig.from_pretrained(
            str(hhem_dir), trust_remote_code=True, local_files_only=True
        )
        # The reviewed code reads `foundation` (a class attribute) to load flan-t5-base
        # with no revision; point it at the verified local copy at FLAN_REVISION.
        type(config).foundation = str(flan_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            str(hhem_dir), config=config, trust_remote_code=True, local_files_only=True
        ).to(device)
        self.model.eval()
        self.torch, self.device = torch, device
        self.versions = {
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "cuda": torch.version.cuda,
            "device": device,
            "device_name": torch.cuda.get_device_name(0) if device.startswith("cuda") else "cpu",
        }

    def describe(self) -> dict[str, Any]:
        return self.versions

    def score(self, premise: str, claim: str) -> float:
        return float(self.model.predict([(premise, claim)])[0])


class SyntheticBackend:
    """Deterministic stand-in for tests: no model. Its files are marked synthetic and the
    M7 runner refuses them unless a test explicitly allows synthetic predictions."""

    inference = "synthetic"

    def describe(self) -> dict[str, Any]:
        return {"device": "none", "note": "synthetic backend: not a model result"}

    def score(self, premise: str, claim: str) -> float:
        return int(_sha(f"{premise}|{claim}".encode())[:4], 16) / 0xFFFF


# ------------------------------------------------------------------------ scoring
def _sync(backend: Backend) -> None:
    torch = getattr(backend, "torch", None)
    if torch is not None and str(getattr(backend, "device", "")).startswith("cuda"):
        torch.cuda.synchronize()


def score_inputs(inputs: Inputs, backend: Backend) -> str:
    """Score every pair once, in file order; return the predictions JSONL text."""

    rows = []
    for r in inputs.rows:
        _sync(backend)
        started = time.perf_counter()
        p = backend.score(r["premise"], r["claim"])
        _sync(backend)
        ms = (time.perf_counter() - started) * 1000
        if not 0.0 <= p <= 1.0:
            raise RuntimeError(f"{r['pair_id']}: score {p} outside [0, 1]")
        rows.append(
            {
                "pair_id": r["pair_id"],
                "input_sha256": r["input_sha256"],  # copied unchanged from the export
                "p_supported": p,
                "predicted": "supported" if p > OPERATING_POINT["threshold"] else "unsupported",
                "latency_ms": ms,
            }
        )
    header = {
        "baseline": "HHEM-2.1-open (Vectara)",
        "model": HHEM_REPO,
        "revision": HHEM_REVISION,
        "inference": backend.inference,
        "inputs_sha256": inputs.header["inputs_sha256"],  # copied unchanged
        "inputs_file_sha256": inputs.file_sha256,
        "operating_point": OPERATING_POINT,
        "p_supported": "P(consistent) from HHEM's two-label head (label 1)",
        "runtime": {
            "scorer": f"ml/hhem-baseline/hhem_score.py {SCORER_VERSION}",
            "scorer_sha256": _sha(Path(__file__).read_bytes()),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "reviewed_code_sha256": {n: HHEM_FILES[n] for n in REVIEWED_CODE},
            "weights_sha256": HHEM_FILES["model.safetensors"],
            "foundation": {"repo": FLAN_REPO, "revision": FLAN_REVISION},
            "latency": "wall time per pair around one predict() call (CUDA-synchronised)",
            **backend.describe(),
        },
    }
    lines = [header, *rows]
    return "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path("/content/hhem-pinned"))
    ap.add_argument("--allow-cpu", action="store_true",
                    help="permit CPU (not the T4 latency measurement)")  # fmt: skip
    args = ap.parse_args(argv)
    inputs = read_inputs(args.inputs)  # fail on stale or edited inputs before any download
    hhem_dir, flan_dir = fetch(args.cache_dir)
    import torch  # noqa: PLC0415 - GPU environment only

    if torch.cuda.is_available():
        device = "cuda"
    elif args.allow_cpu:
        device = "cpu"
    else:
        print("no CUDA device: run on the Colab GPU (or pass --allow-cpu)", file=sys.stderr)
        return 2
    text = score_inputs(inputs, HhemBackend(hhem_dir, flan_dir, device))
    args.out.write_text(text, "utf-8")
    print(f"scored {len(inputs.rows)} pairs on {device}; wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
