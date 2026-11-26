# HHEM-2.1-open baseline scorer

`hhem_score.py` scores the M7 checker runner's exported inputs with Vectara's HHEM-2.1-open
on the Colab GPU. It writes a predictions file for `checker-baselines --scorer predictions`.
The model revision, the reviewed custom code, the weights and the flan-t5-base foundation are
all pinned by SHA-256. The full write-up, including the code review, is
`m7-hhem-baseline.md` in the repository's evaluation docs.

```bash
pip install -r requirements.txt            # torch comes from the Colab runtime
python hhem_score.py --inputs inputs.jsonl --out predictions.jsonl --cache-dir /content/hhem-pinned
# Requires a Colab GPU runtime with a Tesla T4; otherwise it exits (code 2) before downloading.
```

**Colab T4 smoke test (pending).** Open `hhem_smoke_t4.ipynb` in Colab with a T4 GPU runtime and select *Run all*. The notebook verifies the checked-out commit and checks that HHEM loads under transformers 4.56.2. All seven examples must match the model card on the warm-up and five timed passes. The current `hhem-smoke-report.json` records every score and latency, the GPU and the library versions. If the smoke cell fails and produces a verified failure report, run the last two cells to inspect and download it.
