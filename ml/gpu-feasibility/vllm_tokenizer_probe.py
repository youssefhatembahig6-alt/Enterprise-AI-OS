"""Initialise chat-model tokenizers through vLLM's own tokenizer code path.

Runs inside the isolated vLLM environment (never the notebook kernel):

    <vllm-env>/bin/python vllm_tokenizer_probe.py <tokenizer dir> [<tokenizer dir> ...]

vLLM's get_tokenizer() wraps the Hugging Face tokenizer with get_cached_tokenizer(), which
reads attributes such as all_special_tokens_extended. A Transformers release that removed
those attributes makes `vllm serve` crash at startup; this probe reproduces that in seconds,
on CPU, without loading model weights. build_notebook.py inlines this file into the notebook,
and tests/test_vllm_tokenizer.py runs it in CI. Prints one JSON object; exit code 0 = ok.
"""

import importlib.metadata as md
import json
import sys
import traceback


def probe(path):
    from vllm.transformers_utils.tokenizer import get_tokenizer

    tok = get_tokenizer(path, tokenizer_mode="auto", trust_remote_code=False)
    messages = [{"role": "system", "content": "You answer from sources."},
                {"role": "user", "content": "How many days of annual leave do full-time staff in Egypt get?"}]
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    ids = tok.encode(prompt)
    text = "NileTech: 21 days (EG), 22 days (AE)."
    return {
        "class": type(tok).__name__,
        "vocab_size": len(tok),
        "special_tokens_extended": len(tok.all_special_tokens_extended),
        "eos_token": tok.eos_token,
        "prompt_tokens": len(ids),
        "thinking_disabled_in_template": "<think>\n\n</think>" in prompt,
        "roundtrip_ok": tok.decode(tok.encode(text, add_special_tokens=False)) == text,
    }


def main(paths):
    out = {"versions": {}, "tokenizers": {}, "ok": True}
    for pkg in ("vllm", "transformers", "tokenizers", "torch", "huggingface-hub"):
        try:
            out["versions"][pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out["versions"][pkg] = None
    for path in paths:
        try:
            out["tokenizers"][path] = probe(path)
            if not out["tokenizers"][path]["roundtrip_ok"]:
                out["ok"] = False
        except Exception as exc:
            out["ok"] = False
            out["tokenizers"][path] = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-3000:]}
    print(json.dumps(out))
    return 0 if out["ok"] and paths else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
