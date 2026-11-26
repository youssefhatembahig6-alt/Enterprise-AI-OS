# HHEM-2.1-open baseline scorer

`hhem_score.py` scores the M7 checker runner's exported inputs with Vectara's HHEM-2.1-open
on the Colab GPU. It writes a predictions file for `checker-baselines --scorer predictions`.
The model revision, the reviewed custom code, the weights and the flan-t5-base foundation are
all pinned by SHA-256. The full write-up, including the code review, is
`m7-hhem-baseline.md` in the repository's evaluation docs. The scored comparison on the 37
human-agreed dev pairs (HHEM vs zero-shot DeBERTa vs the Qwen3-8B judge) is
`m7-baseline-run.md`, next to that write-up.

```bash
pip install -r requirements.txt            # torch comes from the Colab runtime
python hhem_score.py --inputs inputs.jsonl --out predictions.jsonl --cache-dir /content/hhem-pinned
# Requires a Colab GPU runtime with a Tesla T4; otherwise it exits (code 2) before downloading.
```

**Colab T4 smoke test passed.** The [report](../../docs/evidence/m7/hhem-smoke-report.json) records every score and latency, the GPU and the library versions. To reproduce the run, open `hhem_smoke_t4.ipynb` in Colab with a T4 GPU runtime, set `REF` to `4f06c2597628d376ae242bb3659aaabd63c25193`, and select *Run all*. All seven examples must match the model card on the warm-up and five timed passes. If the smoke cell fails and produces a verified failure report, run the last two cells to inspect and download it.
