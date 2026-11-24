# M5 open items: exact setup and commands

All three acceptance runs passed on 24 November 2026. Live GitHub acceptance passed 22 checks with the fake model, native Windows passed 13 real-backend smoke checks, and Colab passed 22 real-model checks. The sections below document reproduction. See the [evidence README](README.md) for the tested commits and working-tree provenance. Never paste a token or password into a chat, an issue, a commit or an evidence file; the runners never print or record them.

## 1. Dedicated private GitHub test repository

Completed by `2026-11-24-github-acceptance-live-fake-model.json` (22/22 checks). Reproduction closes rows G0–G3 and G4 live in [README.md](README.md). Needs: your GitHub account (owner), a **second** GitHub account you control, about 20 minutes.

### 1.1 Create the repository with the fixture files

From the application repository root, signed in with `gh` as the owner:

```bash
gh repo create <owner>/eaios-m5-acceptance --private --description "EAIOS M5 acceptance fixture (synthetic)"
git clone https://github.com/<owner>/eaios-m5-acceptance.git /tmp/eaios-m5-acceptance
mkdir -p /tmp/eaios-m5-acceptance/inventory /tmp/eaios-m5-acceptance/billing
cp backend/fixtures/github-acceptance/inventory/reservations.py /tmp/eaios-m5-acceptance/inventory/
cp backend/fixtures/github-acceptance/billing/refunds.py /tmp/eaios-m5-acceptance/billing/
cp backend/fixtures/github-acceptance/README.md /tmp/eaios-m5-acceptance/
(cd /tmp/eaios-m5-acceptance && sha256sum -c "$OLDPWD/backend/fixtures/github-acceptance/SHA256SUMS")
(cd /tmp/eaios-m5-acceptance && git add -A && git commit -m "M5 acceptance fixture" && git push)
```

The checksum line must print `OK` for both files; the runner's default questions depend on these exact bytes.

### 1.2 Add the second account as a read collaborator

```bash
gh api -X PUT repos/<owner>/eaios-m5-acceptance/collaborators/<second-account> -f permission=pull
```

Sign in to GitHub as `<second-account>` and accept the invitation (github.com/notifications, or the e-mail link). Check from the owner account (prints `204`-style success with no output; a 404 means not yet accepted):

```bash
gh api repos/<owner>/eaios-m5-acceptance/collaborators/<second-account> && echo collaborator
```

### 1.3 Scoped connector token (owner account)

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token:

| Field | Value |
| --- | --- |
| Token name | `eaios-m5-acceptance-connector` |
| Expiration | 7 days |
| Resource owner | `<owner>` |
| Repository access | **Only select repositories** → `<owner>/eaios-m5-acceptance` |
| Repository permissions | **Contents: Read-only**, **Metadata: Read-only** (mandatory); leave everything else at *No access* |

Listing collaborators is a Metadata permission; GitHub also requires the token owner to have write or admin access to the repository, which the owner has. Save the token without echoing it:

```bash
install -m 600 /dev/null var/connectors/github-token
read -rs GH_CONNECTOR && printf '%s' "$GH_CONNECTOR" > var/connectors/github-token && unset GH_CONNECTOR
```

(paste the token at the silent prompt, then Enter). Write the operator file and point the stack at it:

```bash
cat > var/connectors/github.json <<'EOF'
{"tenants": {"niletech": {
  "auth": {"type": "token", "token_file": "/run/eaios/connectors/github-token"},
  "allowed_repositories": ["<owner>/eaios-m5-acceptance"]}}}
EOF
grep -q '^GITHUB_CONNECTOR_CONFIG=' .env \
  && sed -i 's|^GITHUB_CONNECTOR_CONFIG=.*|GITHUB_CONNECTOR_CONFIG=/run/eaios/connectors/github.json|' .env \
  || echo 'GITHUB_CONNECTOR_CONFIG=/run/eaios/connectors/github.json' >> .env
MODEL_MODE=fake python3 scripts/dev.py up
```

`var/` is git-ignored; never commit it.

### 1.4 Run the live acceptance

```bash
python3 scripts/m5_github_acceptance.py --repo <owner>/eaios-m5-acceptance --login <second-account> --mutate gh \
  --out docs/evidence/m5/$(date -u +%F)-github-acceptance-live.json
```

`--mutate gh` lets the runner rename `inventory/reservations.py` → `inventory/holds.py`, delete `billing/refunds.py` and remove the collaborator with your own `gh` login (use `--mutate manual` to make each change yourself when prompted). It checks stages A–E (README, rows G1–G4); exit 0 is PASS. Review the evidence JSON (it holds only paths, commits, quotes from the fixture, statuses) and commit it. To run again: restore the two files (commit them again), re-add and accept the collaborator, and delete the old source in the admin console. Revoke the token when done.

## 2. Windows smoke against the real backend

Completed: `2026-11-24-desktop-smoke-windows-live.json` closes row D8 live (10 Electron steps and 3 backend checks passed on `27c38f5`; fake embeddings, simulated outage, no symlink created). To reproduce, needs: a Windows 10/11 machine with **Docker Desktop** (WSL 2 engine), **Git for Windows**, **Node.js 22 LTS** (22.19 or later, below 23; CI uses 22.19.0) and **Python 3.11+** from python.org. The backend runs in Docker Desktop (the backend's hash-locked dependencies include `uvloop`, which has no Windows build); Electron runs natively on Windows. Optional: Settings → System → For developers → **Developer Mode** on, so the smoke can create a symlink and prove it is skipped (without it, that sub-check is recorded as "symlink not created").

In PowerShell:

```powershell
git clone https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS.git
cd Enterprise-AI-OS
git checkout feature/m5-wave-2
py scripts\dev.py init-env                     # only creates .env if missing; never overwrites
# If .env already existed: make sure CORS_ALLOWED_ORIGINS ends with ,app://eaios
$env:MODEL_MODE = "fake"; py scripts\dev.py up  # builds and starts the stack, applies migration 0011
py scripts\dev.py seed
npm ci
npm run build -w @eaios/web
npm run build -w @eaios/desktop
py scripts\m5_desktop_smoke.py --api http://localhost:8000 --out var\m5-desktop-smoke-windows-live.json
```

Exit code 0 and `desktop smoke passed` close the item. The script uses only the Python standard library in this mode, signs in with the development credentials from `var\dev-credentials.json`, writes no credentials to the report and deletes its temporary folder source afterwards. Copy the JSON to `docs/evidence/m5/<date>-desktop-smoke-windows-live.json` and commit it. Expect `"recursive_watcher": true` and `platform.os = "win32"` in the report.

## 3. Real-model code answers (Colab)

Completed by `2026-11-24-colab-code-real-model.json` (22/22 checks, row G7). To reproduce, use a Colab runtime with a **T4 GPU** and allow about 40 minutes. Everything runs over loopback inside the runtime as in the M2/M3 runs, so no tunnel is opened (the Colab usage gate in the roadmap does not arise). GitHub is the in-memory double holding the same fixture files; the run proves the real model's code answers and their line citations, not live GitHub (that is section 1).

Ready-to-run notebook: [`ml/colab-m5/m5_acceptance.ipynb`](../../../ml/colab-m5/m5_acceptance.ipynb).

Cells:

```python
!git clone -b feature/m5-wave-2 https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS /content/Enterprise-AI-OS
!python /content/Enterprise-AI-OS/ml/colab-m5/run_m5_code_colab.py vllm-env
```

```python
import os, secrets
os.environ["GATEWAY_TOKEN"] = secrets.token_urlsafe(32)   # stays in this runtime; do not print it
%run /content/Enterprise-AI-OS/ml/model-gateway/colab_launch.py
```

```python
!python /content/Enterprise-AI-OS/ml/colab-m5/run_m5_code_colab.py all
```

The last cell starts PostgreSQL 17, Redis and Qdrant on 127.0.0.1, installs the hash-locked backend, migrates and seeds, then runs acceptance stages A–E with `--require-gateway`: every code answer must come from the gateway (a fake-model answer fails the run), and every citation is checked against the fixture bytes (repository, path, commit, blob, line_start, line_end, exact quote). It writes `/content/m5-evidence-<stamp>.zip` after a secret scan. Download it and add `m5-code-real-model.json`, `gateway-health.json` and `versions.json` to `docs/evidence/m5/` as `<date>-colab-code-real-model*.json`. A failing stage is still evidence: commit the JSON and report which step failed (for example `B_citation_a_matches_github` if the model's quote does not land on exact lines, or a checker rejection).

Optional, after section 1: live GitHub **and** the real model together needs the local stack to reach the Colab gateway through a tunnel. That is subject to the Colab usage gate in the roadmap ("Colab feasibility has two gates"); confirm it is acceptable first, then run section 1.4 with `--require-gateway` added and the stack started with `MODEL_MODE=gateway`, `LLM_BASE_URL` and `MODEL_GATEWAY_TOKEN` set in `.env`.
