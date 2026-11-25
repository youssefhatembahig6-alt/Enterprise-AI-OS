# M7 checker baseline runner

Status: **runner built; scoring is BLOCKED.** No dev pair has human gold labels yet (0 of 37). No model was scored. Nothing was fine-tuned, no threshold was selected, and the blind release set was not touched (it does not exist yet, and the runner cannot read anything outside dev).

## Baselines (project specification)

The Member 5 report, section 4.3, "How the result is measured", names the comparisons:

| Baseline | Runner scorer | How it is run |
| --- | --- | --- |
| Zero-shot NLI checkpoint (no fine-tuning) | `deberta` | The **current pinned checker**, `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli@6f5cf0a2…`, through the model gateway's `/check`, exactly as the application calls it. P(supported) = P(entailment). |
| HHEM-2.1-open (Vectara) | `predictions` | Scored offline on Colab; the runner reads a predictions file whose header names model, revision and runtime. **Not yet produced** (see gaps). |
| Qwen3-8B as a judge (on Colab) | `qwen-judge` | Through the gateway chat route, temperature 0, thinking off, fixed prompt `m7-judge-1` (its SHA-256 is recorded), one-word answer. Hard labels; an unparseable answer is scored UNSUPPORTED (fail closed) and counted. |

The fine-tuned checker does not exist yet. When it does, it plugs in as another scorer.

## Metrics (`app/evaluation/checker_metrics.py`; "unsupported" is the positive class)

* **Unsupported precision, recall and F1.**
* **False-support rate (headline):** gold-unsupported claims predicted supported, divided by all gold-unsupported claims (= 1 − recall). Reported with a Wilson 95% interval.
* **Calibration** of P(supported): ECE over 10 equal-width bins, maximum bin error and Brier score. Hard-label scorers are flagged `hard_labels`.
* **Accuracy per change type:** original, number, unit, entity, scope, polarity, wrong evidence.
* **Latency per claim:** nearest-rank p50 and p95, mean and max, in ms. Measured around each gateway call from the runner, so it **includes the network round trip** to the Colab gateway, not only T4 compute. The gateway's own timings are a separate, later measurement.
* A zero denominator gives `None`, never 0 or 1, so an empty class cannot look perfect.

## Data policy (enforced)

* **Labels:** only human gold labels for the M7 gold checker pairs, from `labels.human_gold`. A pair counts only if two annotators agreed or it was adjudicated. The following are never used: single labels, open disagreements, the AI author's `proposed_label`, and silver labels (checker batch 001).
* **Split:**
  - Every evaluation pair must have `split = dev` and `release = false`.
  - Every source it cites must be a dev member of the active frozen manifest, selected from frozen metadata by `loader.eligible("model-selection")`.
  - Anything else raises `SplitViolation`. Tested with a train pair, a train source, a held-out source, and a newer manifest that moves a dev family to held-out.
* **No train, held-out or blind data:** an audit-hook test shows that building the evaluation set and running the runner opens no `corpus-v3` file, no checker-batch file, no held-out key and nothing "blind".
* **Blocked means blocked:** with no human-reviewed pairs, the runner reports `status: blocked` with the reason and exits with code 3. It does so **before creating the scorer**, so the gateway is never contacted (tested).

## What a report records

* **Dataset:** gold pair file SHA-256, labels dataset revision, active manifest name and SHA-256, SHA-256 of each human label file, candidate and human-reviewed counts, label-state counts, Cohen's kappa, and gold class counts.
* **Model:** the scorer's model and revision (from the client's pinned configuration), what the gateway reports it serves, and the operating point with its provenance. DeBERTa uses the configured `CHECKER_ENTAILMENT_THRESHOLD`, default 0.5: uncalibrated, and **not selected here**.
* **Runtime:** Python version, platform, code commit and timestamp.
* **Per-pair predictions:** P(supported), the decision and latency, plus extras such as the judge's raw answer or DeBERTa's neutral and contradiction scores.

## Actual result today

```
$ python -m app.evaluation checker-baselines --scorer deberta
{
  "status": "blocked",
  "reason": "0 of 37 dev gold checker pairs have human gold labels (two agreeing annotators or adjudication); scoring needs human-reviewed dev labels",
  "dataset": {"candidate_pairs": 37, "human_reviewed_pairs": 0}
}
exit code 3
```

The **synthetic self-test** (`--synthetic-selftest`) runs the whole pipeline on 12 made-up pairs with a deterministic fake scorer, and is labelled `not_a_result`. Its numbers are the hand-computed test oracle (TP 5, FP 1, FN 2, TN 4):
- unsupported precision 5/6, recall 5/7, F1 0.769;
- false-support rate 2/7, Wilson 95% [0.082, 0.641];
- ECE 3.4/12 = 0.283;
- latency p50 11 ms, p95 50 ms.

These are not model results.

## Reproduction

```bash
cd backend
.venv/bin/python -m app.evaluation checker-baselines --scorer deberta --out ../var/m7-baseline-deberta.json   # blocked today (exit 3)
.venv/bin/python -m app.evaluation checker-baselines --synthetic-selftest                                 # runner self-test, not a result
.venv/bin/pytest -q tests/unit/test_m7_evaluation.py   # 135 tests; 24 cover the metrics and the runner
# Once human labels exist and the Colab gateway is up (MODEL_MODE=gateway, LLM_BASE_URL, token):
.venv/bin/python -m app.evaluation checker-baselines --scorer deberta    --out ../var/m7-baseline-deberta.json
.venv/bin/python -m app.evaluation checker-baselines --scorer qwen-judge --out ../var/m7-baseline-qwen.json
.venv/bin/python -m app.evaluation checker-baselines --scorer predictions --predictions hhem.jsonl --out ../var/m7-baseline-hhem.json
```

## Gaps

1. **Human labels:** two annotators must label the 37 dev gold pairs through the blind workflow (`docs/evaluation/m7-labelling.md`). Until then, scoring is blocked.
2. **Dev set size:** 37 pairs give wide intervals: even 0 false passes out of ~19 unsupported pairs has a Wilson upper bound of about 17%. A larger human-labelled dev set is needed before comparisons mean much.
3. **HHEM-2.1-open predictions:** no Colab script yet. HHEM loads custom model code (`trust_remote_code`), which needs a pinned revision and a review of that code before it runs. The script must write the predictions-file header (model, revision, runtime).
4. **Judge bias:** Qwen3-8B also writes the answers in production, so as a judge it may favour its own phrasing (noted in the specification). Here it judges template claims, not its own answers.
5. **Latency** includes the laptop-to-Colab round trip. T4-only per-claim latency needs gateway-side timing.
6. **Out of scope by instruction:** fine-tuning, threshold selection, held-out and blind-release scoring. This runner refuses non-dev data by design; release scoring follows the coordinator procedure in `m7-v3-corpus.md`.
