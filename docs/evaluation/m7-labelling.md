# M7 gold checker labelling: blind sheets, two independent labels, kappa, adjudication

Applies to `backend/fixtures/evaluation/m7/pilot/generated/gold-checker-pairs.jsonl` (pilot: 37 pairs, all dev). The release gold set will use the same workflow.

## Roles

* **Coordinator.** Has the repository. Exports the sheets, keeps the keys, imports the results, and is not one of the two annotators.
* **Annotators A and B.** Receive **only** their own sheet (a CSV). They need no repository access and must not open `pilot/generated/`, `pilot/seeds.toml` or `pilot/labels/`. Those files contain pair ids that name the perturbation type, the AI's proposed labels, and any imported labels.
* **Adjudicator.** Preferably a third person. Resolves the recorded disagreements after both submissions are imported.

## What a sheet shows, and what it hides

Columns: `item_id, claim, evidence_title, evidence, label, note, annotator, dataset_revision`.

* `item_id` is a random opaque id (`item-` + 12 hex digits), different on each sheet. Rows are sorted by it, so each sheet has its own random order. The mapping from item id to pair lives only in the coordinator's key file.
* A sheet never contains the pair id, the perturbation type, the proposed label or the other annotator's work.
* `dataset_revision` is the SHA-256 of the gold-pair file. A sheet is valid only for that exact revision.

## Task

Label each row:
* `supported`: the evidence alone, read with its title, fully supports the claim. Every figure, unit, entity, scope word ("only", "all", "undisputed") and polarity must match.
* `unsupported`: anything else, including claims that are partly supported, contradicted or not stated. Do not use outside knowledge.

When unsure, still choose one label and explain in `note`. Do not change `item_id`, `annotator` or `dataset_revision`, and do not delete or add rows.

## Steps

1. **Coordinator exports** one sheet and one key per annotator:
   ```bash
   cd backend
   .venv/bin/python -m app.evaluation labels export --annotator ann-a
   .venv/bin/python -m app.evaluation labels export --annotator ann-b
   ```
   This writes `var/m7-labels/gold-ann-a.sheet.csv` and `gold-ann-a.key.json`, and the same pair for `ann-b`. `var/` is git-ignored.
2. **Keep submissions private until both are finished.**
   - Send each annotator only their `.sheet.csv`, privately: direct message or email, never a shared channel, shared drive folder, PR or the repository.
   - Annotators return the filled sheet **only to the coordinator**, and do not discuss items with each other until both have returned.
   - Never send or commit a `.key.json`.
   - The coordinator does not show either sheet to anyone before both have arrived.
3. **Coordinator imports both together**, once both sheets have arrived:
   ```bash
   .venv/bin/python -m app.evaluation labels import \
     --sheet ann-a.sheet.csv --key var/m7-labels/gold-ann-a.key.json \
     --sheet ann-b.sheet.csv --key var/m7-labels/gold-ann-b.key.json
   ```
   - Import maps the opaque ids back to pair ids and writes `pilot/labels/gold-ann-a.csv` and `gold-ann-b.csv`.
   - It publishes both files or neither. They are staged in a private directory and then moved into place. If any step fails, files already moved are removed and the staging directory is deleted, so no partial submission remains and the same command can be retried.
   - It refuses:
     - a sheet with any blank or missing label: every item on both sheets needs `supported` or `unsupported`;
     - any edit to `claim`, `evidence_title` or `evidence`, which are checked against the pair behind each opaque id (only `label` and `note` may change; CRLF line endings are tolerated);
     - fewer or more than two submissions, or two from the same annotator;
     - a sheet or key from another dataset revision (for example, after the pilot was regenerated);
     - unknown, duplicated or deleted items;
     - rows belonging to another annotator;
     - invalid labels;
     - re-importing an annotator who is already imported.
   - Commit both label files in one commit.
4. **Measure agreement:** `.venv/bin/python -m app.evaluation labels status` reports each pair's state (unlabelled / single-label / agreed / disagreement-pending / adjudicated), the disagreement list, Cohen's kappa over the double-labelled pairs, and `human_reviewed_pair_ids`. Record kappa **before** adjudication.
5. **Adjudicate** each disagreement in `pilot/labels/adjudications.csv` (`pair_id,final_label,adjudicator,reason`). Adjudicating a pair that is not a recorded disagreement fails the M7 check.
6. **Gold** = agreed pairs + adjudicated pairs. The AI's `proposed_label` never counts.

## Limits of the blinding

The repository contains the proposals, so blinding depends on annotators working only from their sheet. That is why they need no repository access. The claim text of a perturbed pair cannot be hidden: judging it is the task itself.

Cohen's kappa is `(p_o − p_e) / (1 − p_e)` over pairs both annotators labelled. It is `n/a` when no pair is double-labelled or when both annotators used one identical label (`p_e = 1`).
