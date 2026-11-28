# M8 chat on the real Qwen gateway: evidence

PR #22 (`feature/m8-real-model-chat`). Synthetic NileTech fixtures only. **Real model** means the pinned `Qwen/Qwen3-8B-AWQ@4da05a8edb55c6046cce958586c33b61da07bb79` behind `ml/model-gateway` on a Colab T4. **Fake model** means the labelled development double: plumbing evidence only, never model evidence.

## 1. Colab T4: real-model browser test (owner's run, 28 November 2026)

Application commit `ab6d35f6128aabf5d579b2469d5f5908b398d9bf` (clean checkout, all nine CI checks green). The production Vite build, the API, the gateway and headless Chromium ran in one Colab runtime over loopback.

| Check | Result |
| --- | --- |
| Documents | **PASS.** The annual-leave answer said up to five unused days, expiring 31 March, with two citations. The opened passage showed *text hash verified*. Answer call: 31.877 s, 100 completion tokens |
| General | **PASS.** Qwen answered the open-ended meeting question. The reply was labelled General and had 0 citations. General call: 36.607 s, 129 completion tokens |
| Restricted finance question | **PASS.** The sales reader got the no-sources refusal (`abstained / no_sources`). No restricted content appeared on the page |

Both successful run records showed `inference_mode = gateway` and the pinned revision above. The first attempt timed out on the Documents and General calls (`model_unavailable · timeout`). The passing retry warmed the gateway and used a read timeout of 180 s and a call deadline of 360 s. The runner now does both by default (revision `5e758fa`).

| Run | Bundle (kept in the Colab runtime / owner's machine, not committed) | SHA-256 |
| --- | --- | --- |
| First attempt | `m8-chat-evidence-20261127T234748Z.zip` | `b4e8e1e4a92ee22429e97bcccfacc60f9c535c7db00df4017ba03b08d07e4562` |
| Passing retry | `m8-chat-evidence-20261127T235146Z.zip` | `dc51bf0d56157c644187110e385931b2c039a2b14c50ff678839f6858fb6d9ad` |

## 2. Private chat preview in Colab (notebook cells 7–8)

Repeatable from `ml/colab-m8-chat/m8_real_model_chat.ipynb`. Cell 7 runs `run_m8_chat_colab.py preview`:
- it builds the UI with a relative API base;
- `preview_proxy.py` serves it and the API together on 127.0.0.1:13100, so all traffic stays on loopback;
- it checks the page in headless Chromium on that origin: the status badge, a **fresh** General question, a **fresh** Documents question with an opened citation, and the sales-reader refusal;
- it then opens the port with Colab's `serve_kernel_port_as_window`, for the notebook owner's session only. No tunnel and no public URL.

Cell 8 downloads `m8-chat-preview-evidence-<stamp>.zip`.

| Item | Status |
| --- | --- |
| Real-model preview run (badge **Connected · Qwen3-8B-AWQ**, fresh General reply, fresh Documents answer with an opening citation) | **Pending:** needs the owner to run cells 1–3 and 7 on a T4 and return `m8-chat-preview-evidence-<stamp>.zip` |
| Same path, locally against the disposable stack (fake model), 28 Nov 13:47 UTC | **PASS** ([JSON](2026-11-28-local-preview-check-fake-model.json)). Single origin through `preview_proxy.py`. Badge **FAKE model** (correct for fake mode). Fresh Documents question *"Are public holidays that fall during my approved leave deducted from my allowance?"*: cited answer, and the citation opened through the proxy (`GET …/citations/c1` 200, *text hash verified*). Fresh General question *"Explain in plain words what a confusion matrix is."*: General reply, 0 citations. Sales reader: `no_sources`. Streaming (SSE) passed through the proxy. |

## 3. Local Mailpit result (disposable stack, 28 November 2026)

`scripts/m6_demo.py` ran against `eaios-m7acc`: web :13000, API :18000, Mailpit :18025, fake model. The application code was built at `1496d06`; `471ff27` changes only tests. [JSON](2026-11-28-local-mailpit-m6-demo-fake-model.json).

| Step | Result |
| --- | --- |
| Verified Q1 report | published, `figures_verified_narrative_dev_checked` (fake inference) |
| Draft to a recipient without dataset access | `422 action_blocked` |
| Send with the pre-edit, forged and reused approvals | `409 approval_rejected` each |
| Approved v2 sent | delivered; exactly one Mailpit message, Message-ID `<99c7a6ee-91c6-439f-b807-73dee97aed69@eaios.local>`, to `finance.reader@niletech.example` |
| Mailpit total | 4 before → **5** after (13:46:24 UTC); still **5** at 13:47:11 UTC, so no re-send by the reconciler |

The real-model email path (verified report → approval → exactly one Mailpit message) passed in Colab on 26 November ([M7 smoke](../m7-functional/2026-11-26-colab-real-model-smoke/)).
