# M7 gold checker labelling: two independent labels, kappa, adjudication

Applies to `backend/fixtures/evaluation/m7/pilot/generated/gold-checker-pairs.jsonl` (pilot: 37 pairs). The same workflow will be used for the release gold set.

## Task

Each pair contains a **claim** and one **evidence** span, plus the evidence's **source title**. Label one of:

* `supported`: the evidence alone, read with its title, fully supports the claim. Every figure, unit, entity, scope word ("only", "all", "undisputed") and polarity must match.
* `unsupported`: anything else, including partly supported, contradicted, or not stated. Do not use outside knowledge or other documents.

If you are unsure, still choose one and explain in `note`. Do not discuss pairs with the other annotator until both of you have submitted.

## Steps

1. **Export a blind sheet**, one per annotator. Each sheet has its own deterministic item order. It shows no AI proposal and no other labels.

   ```bash
   cd backend
   .venv/bin/python -m app.evaluation labels export --annotator <your-id>
   ```

   The sheet is written to `var/m7-labels/gold-<your-id>.csv` (git-ignored). Fill in the `label` column, and `note` where useful.
2. **Submit independently.** Copy the filled sheet to `backend/fixtures/evaluation/m7/pilot/labels/gold-<your-id>.csv` and commit it in a commit or PR of your own. The second annotator must not open the first annotator's file before submitting.
3. **Measure agreement.**

   ```bash
   .venv/bin/python -m app.evaluation labels status
   ```

   This reports the state of each pair, the disagreement list, Cohen's kappa over the double-labelled pairs, and `human_reviewed_pair_ids`.
4. **Adjudicate** every disagreement in `labels/adjudications.csv` with the columns `pair_id,final_label,adjudicator,reason`. Use a third person where possible; otherwise both annotators decide together and record the reason. Adjudicating a pair that is not a recorded disagreement fails the M7 check.
5. **Gold** = agreed pairs + adjudicated pairs. Report kappa **before** adjudication, the number of disagreements, and their error types.

## Rules the check enforces

* Labels must be `supported` or `unsupported`. Pair ids must exist and each may be labelled only once per annotator.
* At most two annotator files are allowed in the pilot.
* Adjudications need an adjudicator and a valid final label, and may only resolve recorded disagreements.
* The AI author's `proposed_label` is never a human label and never counts as gold.

Cohen's kappa is `(p_o − p_e) / (1 − p_e)` over the pairs both annotators labelled. It is reported as `n/a` when no pair is double-labelled, or when both annotators used a single identical label (`p_e = 1`).
