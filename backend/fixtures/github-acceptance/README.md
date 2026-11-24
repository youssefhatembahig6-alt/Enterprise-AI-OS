# M5 GitHub acceptance fixture

Commit `inventory/reservations.py` and `billing/refunds.py` (exactly these bytes) to the
default branch of the dedicated **private** test repository before running
`scripts/m5_github_acceptance.py`. The run renames the first file to
`inventory/holds.py` and deletes the second. To run again, restore both files
(`git revert` the two acceptance commits, or commit them again) and re-add the second
account as a collaborator.

Synthetic NileTech-style code; no real company data.
