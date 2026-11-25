"""Real-tokenizer check: an extra added_tokens.json beside the pinned flan-t5-base files
changes tokenization if loaded directly, and cannot once the files are staged.

Runs in the isolated tokenizer environment (CI job ``checker-tokenizer``); downloads only
the five pinned flan-t5-base config/tokenizer files (~3 MB), verified by SHA-256.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hhem_score", HERE / "hhem_score.py")
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load hhem_score.py")
hhem = importlib.util.module_from_spec(_spec)
sys.modules["hhem_score"] = hhem
_spec.loader.exec_module(hhem)

PROMPT = (
    "<pad> Determine if the hypothesis is true given the premise?\n\n"
    "Premise: The cap is 5.\n\nHypothesis: The cap is 6."
)


class TokenizerIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from transformers import AutoTokenizer  # noqa: PLC0415 - isolated environment only

        cls.tok = AutoTokenizer
        cls.tmp = Path(tempfile.mkdtemp())
        cls.src = hhem.fetch_repo(cls.tmp, hhem.FLAN_REPO, hhem.FLAN_REVISION, hhem.FLAN_FILES)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp)

    def ids(self, directory: Path) -> list[int]:
        tok = self.tok.from_pretrained(str(directory), local_files_only=True)
        return list(tok(PROMPT)["input_ids"])

    def test_extra_added_tokens_cannot_affect_staged_tokenization(self) -> None:
        clean = self.ids(hhem.stage(self.src, hhem.FLAN_FILES, self.tmp / "load" / "clean"))
        polluted_src = self.tmp / "polluted"
        shutil.copytree(self.src, polluted_src)
        (polluted_src / "added_tokens.json").write_text(
            json.dumps({"Premise": 32100, "hypothesis": 32101})
        )
        # Sanity: loaded directly, the extra file really does change tokenization.
        self.assertNotEqual(self.ids(polluted_src), clean)
        # Staged, it cannot: only the pinned files reach the tokenizer.
        staged = hhem.stage(polluted_src, hhem.FLAN_FILES, self.tmp / "load" / "staged")
        self.assertFalse((staged / "added_tokens.json").exists())
        self.assertEqual(self.ids(staged), clean)
        with self.assertRaises(hhem.PinError):
            hhem.verify_exact_dir(polluted_src, hhem.FLAN_FILES)


if __name__ == "__main__":
    unittest.main()
