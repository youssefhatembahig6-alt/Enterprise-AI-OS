"""CPU-only checks that the committed notebook matches its generator and its mode wiring.

Run: python -m unittest discover -s ml/gpu-feasibility/tests -v
"""

import ast
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import time
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
NOTEBOOK = HERE / "m1_gpu_feasibility.ipynb"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_notebook", HERE / "build_notebook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def code_cells():
    nb = json.loads(NOTEBOOK.read_text())
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def cell(title_number):
    return next(c for c in code_cells() if c.startswith(f"# @title {title_number} ·"))


def run_cell_2(mode):
    ns = {"RESULTS": {"configs": {}, "downloads": {}}, "save_results": lambda: None, "json": json}
    src = cell(2).replace('MODE = "small"  # @param', f'MODE = "{mode}"  # @param', 1)
    exec(compile(src, "<cell2>", "exec"), ns)
    return ns


class NotebookTests(unittest.TestCase):
    def test_notebook_is_regenerated(self):
        self.assertEqual(NOTEBOOK.read_text(), load_builder().render(),
                         "run: python ml/gpu-feasibility/build_notebook.py")

    def test_bench_core_is_inlined_verbatim(self):
        core = (HERE / "bench_core.py").read_text().rstrip("\n")
        self.assertIn(core, cell(7))
        self.assertNotIn("<<BENCH_CORE>>", NOTEBOOK.read_text())

    def test_every_code_cell_parses(self):
        for src in code_cells():
            ast.parse(src)

    def test_first_cell_detects_gpu_before_anything_else(self):
        first = code_cells()[0]
        self.assertTrue(first.startswith("# @title 1 · Detect the GPU"))
        for forbidden in ("snapshot_download", "pip", "import torch", "vllm serve"):
            self.assertNotIn(forbidden, first)

    def test_small_is_the_default_mode(self):
        ns = run_cell_2("small")
        self.assertEqual(ns["MODE"], "small")
        self.assertIn('MODE = "small"  # @param ["small", "target"]', cell(2))
        self.assertEqual(ns["MODE_MODELS"], ["chat_small", "embed", "checker"])
        self.assertEqual(ns["CONFIG"]["models"]["chat_small"]["repo"], "Qwen/Qwen3-1.7B")
        self.assertIn("development baseline", ns["CONFIG"]["models"]["checker"]["role"])
        self.assertEqual(ns["RESULTS"]["mode"], "small")

    def test_target_mode_keeps_the_original_benchmark(self):
        ns = run_cell_2("target")
        self.assertEqual(ns["MODE_MODELS"], ["chat_target", "embed", "checker"])
        self.assertEqual(ns["CONFIG"]["models"]["chat_target"]["repo"], "Qwen/Qwen3-8B-AWQ")
        self.assertEqual(ns["MODE_SPEC"]["quantization"], "awq")
        self.assertEqual(ns["MODE_SPEC"]["utilizations"], [0.85, 0.75])

    def test_mode_change_inside_one_run_is_refused(self):
        ns = {"RESULTS": {"configs": {}, "mode": "small"}, "save_results": lambda: None, "json": json}
        src = cell(2).replace('MODE = "small"  # @param', 'MODE = "target"  # @param', 1)
        with self.assertRaises(RuntimeError):
            exec(compile(src, "<cell2>", "exec"), ns)

    def test_downloads_only_the_selected_modes_models(self):
        for mode, expected in [("small", {"Qwen/Qwen3-1.7B", "BAAI/bge-m3", "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"}),
                               ("target", {"Qwen/Qwen3-8B-AWQ", "BAAI/bge-m3", "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"})]:
            with self.subTest(mode=mode):
                self.assertEqual(self._fake_downloads(mode), expected)

    def _fake_downloads(self, mode):
        requested = []
        hub = types.ModuleType("huggingface_hub")

        class HfApi:
            def model_info(self, repo, revision, files_metadata):
                files = ["config.json", "model.safetensors", "tokenizer.json", "onnx/model.onnx"]
                return types.SimpleNamespace(sha="0" * 40, card_data=None,
                                             siblings=[types.SimpleNamespace(rfilename=f, size=10) for f in files])

        def snapshot_download(repo, revision, local_dir, allow_patterns):
            requested.append(repo)
            self.assertNotIn("onnx/model.onnx", allow_patterns)
            return local_dir

        hub.HfApi, hub.snapshot_download = HfApi, snapshot_download
        ns = run_cell_2(mode)
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ns.update(BASE=tmp, shutil=shutil, pathlib=pathlib, time=time,
                  run_stage=lambda name, fn, requires=(): fn())
        saved = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = hub
        try:
            exec(compile(cell(5), "<cell5>", "exec"), ns)
        finally:
            if saved is None:
                sys.modules.pop("huggingface_hub", None)
            else:
                sys.modules["huggingface_hub"] = saved
        self.assertEqual(sorted(ns["RESULTS"]["downloads"]), sorted(ns["MODE_MODELS"]))
        self.assertEqual(ns["RESULTS"]["downloads"]["checker"]["role"], ns["CONFIG"]["models"]["checker"]["role"])
        return set(requested)


if __name__ == "__main__":
    unittest.main()
