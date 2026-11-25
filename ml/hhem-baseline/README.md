# HHEM-2.1-open baseline scorer

`hhem_score.py` scores the M7 checker runner's exported inputs with Vectara's HHEM-2.1-open
on the Colab GPU. It writes a predictions file for `checker-baselines --scorer predictions`.
The model revision, the reviewed custom code, the weights and the flan-t5-base foundation are
all pinned by SHA-256. The full write-up, including the code review, is
`m7-hhem-baseline.md` in the repository's evaluation docs.

```bash
pip install -r requirements.txt            # torch comes from the Colab runtime
python hhem_score.py --inputs inputs.jsonl --out predictions.jsonl --cache-dir /content/hhem-pinned
```
