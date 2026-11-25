"""Count checker-pair lengths with the pinned DeBERTa tokenizer, as the gateway does.

Standalone script (stdlib + ``transformers``). It runs in an isolated environment
installed from ``ml/checker-tokenizer/requirements.txt``, not in the backend venv:

    python token_lengths.py --pairs PAIRS.jsonl --tokenizer-dir DIR --out REPORT.json
    python token_lengths.py --pairs PAIRS.jsonl --tokenizer-dir DIR --check REPORT.json

Counting matches ``ml/model-gateway/gateway.py`` (``NliChecker.score``):
``tokenizer(premise, hypothesis, truncation=False)["input_ids"]``, special tokens included,
loaded with ``AutoTokenizer.from_pretrained`` from the pinned revision. The limit is
``CHECKER_MAX_TOKENS = 512``; the gateway refuses longer inputs.

Only tokenizer files are downloaded (no weights), at the pinned revision, and each is
verified against the SHA-256 pinned below before use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"
REVISION = "6f5cf0a2b59cabb106aca4c287eed12e357e90eb"
LIMIT = 512
# SHA-256 of each tokenizer file at REVISION (verified on every run).
FILES: dict[str, str] = {
    "added_tokens.json": "dc046d04c9b0ada7ae6f1dc89c465801799acdf0c9a6aab8c15a1b2d5ca4e91f",
    "config.json": "a6c616d6dabeacf90fd0e776c741d3f0f30a05533ccf0bd3b5b62e94cfaa8d57",
    "special_tokens_map.json": "9463f61e1b109a8eb4688b829260d7c6b1e6dff04c98ff7269bb89e2b92369b9",
    "spm.model": "c679fbf93643d19aab7ee10c0b99e460bdbc02fedf34b92b05af343b4af586fd",
    "tokenizer.json": "05402ffae6dd382a8491b1d29bfc139bec5d332662e86a026f433ce54c25c202",
    "tokenizer_config.json": "557b3d33d3f41b81ad769244e506549e98a1857d41dd58160aacd4d98d710b5a",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(target: Path) -> dict[str, str]:
    """Download (if absent) and verify the pinned tokenizer files. Returns their hashes."""

    target.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for name, pinned in FILES.items():
        path = target / name
        if not path.is_file():
            url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"
            with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
                path.write_bytes(resp.read())
        digest = _sha(path.read_bytes())
        if pinned and digest != pinned:
            raise SystemExit(f"{name}: sha256 {digest} != pinned {pinned}")
        hashes[name] = digest
    return hashes


def count(pairs_path: Path, tokenizer_dir: Path) -> dict[str, Any]:
    # Lazy: transformers exists only in the isolated tokenizer environment, not the backend.
    import transformers  # noqa: PLC0415
    from transformers import AutoTokenizer  # noqa: PLC0415

    hashes = fetch(tokenizer_dir)
    tok = AutoTokenizer.from_pretrained(str(tokenizer_dir))
    raw = pairs_path.read_bytes()
    pairs = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    lengths: dict[str, int] = {}
    by_family: dict[str, list[int]] = defaultdict(list)
    for p in pairs:
        ids = tok(p["premise"], p["claim"], truncation=False)["input_ids"]
        lengths[p["id"]] = len(ids)
        by_family[p["family"]].append(len(ids))
    values = sorted(lengths.values())

    def summary(xs: list[int]) -> dict[str, Any]:
        xs = sorted(xs)
        return {
            "pairs": len(xs),
            "min": xs[0],
            "median": statistics.median(xs),
            "p95": xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))],
            "max": xs[-1],
            "over_limit": sum(x > LIMIT for x in xs),
        }

    return {
        "tokenizer": {
            "repository": REPO,
            "revision": REVISION,
            "files_sha256": hashes,
            "class": type(tok).__name__,
            "transformers": transformers.__version__,
        },
        "counting": "len(tokenizer(premise, claim, truncation=False)['input_ids'])",
        "limit": LIMIT,
        "pairs_sha256": _sha(raw),
        "overall": summary(values),
        "by_family": {f: summary(v) for f, v in sorted(by_family.items())},
        "over_limit_ids": sorted(i for i, n in lengths.items() if n > LIMIT),
        "lengths": dict(sorted(lengths.items())),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=Path, required=True)
    ap.add_argument("--tokenizer-dir", type=Path, required=True)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--check", type=Path)
    args = ap.parse_args(argv)
    report = count(args.pairs, args.tokenizer_dir)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, "utf-8")
        print(json.dumps(report["overall"]))
        return 0
    committed = args.check.read_text("utf-8")
    if committed != text:
        print("token-length report differs from a fresh count with the pinned tokenizer")
        return 1
    if report["overall"]["over_limit"]:
        print(f"{report['overall']['over_limit']} pairs exceed {LIMIT} tokens")
        return 1
    print(f"token lengths verified: {json.dumps(report['overall'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
