"""Initialise the real Qwen tokenizers through vLLM's tokenizer code path, on CPU.

Needs a Python environment installed from vllm-env-pins.txt (CPU torch is enough):

    uv venv --python 3.12 /tmp/vllm-env
    uv pip install --python /tmp/vllm-env/bin/python --torch-backend cpu -r ml/gpu-feasibility/vllm-env-pins.txt
    EAIOS_VLLM_PYTHON=/tmp/vllm-env/bin/python python -m unittest discover -s ml/gpu-feasibility/tests -v

Without EAIOS_VLLM_PYTHON the test is skipped, unless EAIOS_REQUIRE_VLLM_TOKENIZER_TEST=1 (set in CI).
Only tokenizer and config files are downloaded (a few MB per model), at pinned revisions.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.request

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import bench_core  # noqa: E402

VLLM_PY = os.environ.get("EAIOS_VLLM_PYTHON")
REQUIRED = os.environ.get("EAIOS_REQUIRE_VLLM_TOKENIZER_TEST") == "1"
# Revisions resolved from "main" on 2026-11-20.
TOKENIZERS = {
    "Qwen/Qwen3-1.7B": "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e",      # small mode
    "Qwen/Qwen3-8B-AWQ": "4da05a8edb55c6046cce958586c33b61da07bb79",    # target mode
}
FILES = ["config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"]


def fetch_tokenizer(repo, revision, root):
    target = root / repo.replace("/", "--") / revision
    for name in FILES:
        path = target / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://huggingface.co/{repo}/resolve/{revision}/{name}"
            with urllib.request.urlopen(url, timeout=120) as r:
                path.write_bytes(r.read())
    return target


@unittest.skipUnless(VLLM_PY or REQUIRED, "set EAIOS_VLLM_PYTHON to a Python with the pinned vLLM environment")
class VllmTokenizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not VLLM_PY:
            raise RuntimeError("EAIOS_REQUIRE_VLLM_TOKENIZER_TEST=1 but EAIOS_VLLM_PYTHON is not set")
        cache = pathlib.Path(os.environ.get("EAIOS_TOKENIZER_CACHE") or tempfile.gettempdir()) / "eaios-qwen-tokenizers"
        cls.dirs = {repo: fetch_tokenizer(repo, rev, cache) for repo, rev in TOKENIZERS.items()}

    def test_environment_matches_the_pins(self):
        pins = bench_core.parse_pins((HERE / "vllm-env-pins.txt").read_text())
        code = ("import json, importlib.metadata as m\n"
                f"print(json.dumps({{p: m.version(p) for p in {sorted(pins)!r}}}))")
        out = subprocess.run([VLLM_PY, "-I", "-c", code], capture_output=True, text=True, check=True).stdout
        installed = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(bench_core.plan_vllm_env(installed, pins)["action"], "reuse", installed)

    def test_qwen_tokenizers_initialise_through_vllm(self):
        proc = subprocess.run([VLLM_PY, "-I", str(HERE / "vllm_tokenizer_probe.py"), *map(str, self.dirs.values())],
                              capture_output=True, text=True, timeout=900)
        self.assertTrue(proc.stdout.strip(), proc.stderr[-3000:])
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(proc.returncode, 0, json.dumps(result, indent=1)[-3000:])
        self.assertTrue(result["ok"])
        for repo, path in self.dirs.items():
            with self.subTest(repo=repo):
                tok = result["tokenizers"][str(path)]
                self.assertNotIn("error", tok)
                self.assertTrue(tok["class"].startswith("Cached"), "must go through vLLM's get_cached_tokenizer")
                self.assertGreater(tok["special_tokens_extended"], 0)
                self.assertEqual(tok["eos_token"], "<|im_end|>")
                self.assertTrue(tok["thinking_disabled_in_template"])
                self.assertTrue(tok["roundtrip_ok"])


if __name__ == "__main__":
    unittest.main()
