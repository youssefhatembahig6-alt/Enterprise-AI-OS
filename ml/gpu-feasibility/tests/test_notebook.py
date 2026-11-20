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
        self.assertIn(core, cell(4))  # cell 4 is the first cell that uses it
        self.assertNotIn("<<", NOTEBOOK.read_text().replace("<<<", ""))

    def test_vllm_pins_come_from_the_pins_file(self):
        sys.path.insert(0, str(HERE))
        try:
            from bench_core import parse_pins
        finally:
            sys.path.pop(0)
        pins = parse_pins((HERE / "vllm-env-pins.txt").read_text())
        self.assertEqual(run_cell_2("small")["CONFIG"]["vllm_pins"], pins)
        self.assertEqual(run_cell_2("target")["CONFIG"]["vllm_pins"], pins)

    def test_tokenizer_probe_is_embedded_verbatim_and_gates_vllm_start(self):
        tree = ast.parse(cell(5))
        src = next(ast.literal_eval(n.value) for n in tree.body
                   if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "VLLM_PROBE_SRC")
        self.assertEqual(src, (HERE / "vllm_tokenizer_probe.py").read_text())
        self.assertIn('run_stage("vllm_tokenizer_preflight", _tokenizer_preflight, requires=["install", f"download/{CHAT_KEY}"])', cell(5))
        self.assertIn('"vllm_tokenizer_preflight"', cell(8).split("READY = ", 1)[1].splitlines()[0])

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
                  run_stage=lambda name, fn, requires=(): fn() if name.startswith("download/") else None)
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


class FakeVenv:
    """Simulates the commands cell 4 runs (`sh`) against a vLLM venv with given package versions."""

    def __init__(self, base, installed, fail_first_install=False):
        self.base, self.installed, self.calls = base, installed, []
        self.fail_first_install = fail_first_install
        self.py = base / "vllm-env" / "bin" / "python"
        if installed is not None:
            self.py.parent.mkdir(parents=True)
            self.py.write_text("")

    def sh(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd[0] == str(self.py) and cmd[1] == "-c":
            code = cmd[2]
            if "torch.version.cuda" in code:
                return 0, json.dumps({**self.installed, "torch": "2.8.0", "torch_cuda": "12.8", "python": "3.12.12"})
            wanted = ast.literal_eval(code.split("for p in ", 1)[1].split(":", 1)[0])
            return 0, json.dumps({p: self.installed.get(p) for p in wanted})
        if cmd[1:3] == ["venv", "--python"]:
            self.py.parent.mkdir(parents=True, exist_ok=True)
            self.py.write_text("")
            self.installed = {}
            return 0, "created"
        if cmd[1:3] == ["pip", "install"] and "-r" in cmd:
            if self.fail_first_install:
                self.fail_first_install = False
                return 1, "resolver error"
            for line in pathlib.Path(cmd[cmd.index("-r") + 1]).read_text().splitlines():
                name, _, version = line.partition("==")
                self.installed[name] = version
            return 0, "installed"
        return 0, ""  # kernel pip installs, uv bootstrap, freeze

    def uv_installs(self):
        return [c for c in self.calls if c[1:3] == ["pip", "install"] and "-r" in c]

    def uv_venvs(self):
        return [c for c in self.calls if c[1:3] == ["venv", "--python"]]


class InstallCellTests(unittest.TestCase):
    """Executes the real cell 4 against a simulated venv: a rerun must repair the old pair."""

    def run_cell_4(self, installed, **kw):
        ns = run_cell_2("small")
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "out").mkdir()
        venv = FakeVenv(tmp, installed, **kw)
        ns["RESULTS"]["environment"] = {}
        captured = {}

        def run_stage(name, fn, requires=()):
            captured[name] = fn()

        ns.update(BASE=tmp, OUT=tmp / "out", sh=venv.sh, run_stage=run_stage, sys=sys, shutil=shutil,
                  pathlib=pathlib, time=time, importlib=importlib)
        exec(compile(cell(4), "<cell4>", "exec"), ns)
        return captured["install"], venv, ns["VLLM_PINS"]

    def test_rerun_repairs_old_vllm_transformers_pair(self):
        details, venv, pins = self.run_cell_4({"vllm": "0.11.0", "transformers": "5.23.0"})
        self.assertEqual(details["env_action"], "repair")
        self.assertEqual(details["mismatched_before"]["transformers"], {"installed": "5.23.0", "required": pins["transformers"]})
        self.assertEqual(len(venv.uv_installs()), 1)
        self.assertEqual(venv.uv_venvs(), [], "repair happens in place")
        self.assertEqual(details["installed_after"], pins)

    def test_matching_environment_is_reused_without_installs(self):
        details, venv, pins = self.run_cell_4(dict(load_pins()))
        self.assertEqual(details["env_action"], "reuse")
        self.assertEqual(venv.uv_installs(), [])

    def test_missing_environment_is_created_with_pins(self):
        details, venv, pins = self.run_cell_4(None)
        self.assertEqual(details["env_action"], "create")
        self.assertEqual(len(venv.uv_venvs()), 1)
        self.assertEqual(details["installed_after"], pins)

    def test_failed_in_place_repair_rebuilds_the_environment(self):
        details, venv, pins = self.run_cell_4({"vllm": "0.11.0", "transformers": "5.23.0"}, fail_first_install=True)
        self.assertEqual(details["env_action"], "recreate after failed repair")
        self.assertEqual(len(venv.uv_installs()), 2)
        self.assertEqual(len(venv.uv_venvs()), 1)
        self.assertEqual(details["installed_after"], pins)


def load_pins():
    sys.path.insert(0, str(HERE))
    try:
        from bench_core import parse_pins
    finally:
        sys.path.pop(0)
    return parse_pins((HERE / "vllm-env-pins.txt").read_text())


if __name__ == "__main__":
    unittest.main()
