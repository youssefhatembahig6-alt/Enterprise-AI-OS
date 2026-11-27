# Rehearsal checklist, results and recording outline (M8)

## Rehearsal checklist

Use this list for every rehearsal and on the defense day. Steps refer to [demo-script.md](demo-script.md).

**Day before**

- [ ] `git pull` on `main`; note the commit you will present (`git rev-parse --short HEAD`).
- [ ] Full reset ([demo-script.md § Reset procedure](demo-script.md#reset-procedure)) completes without errors. `seed-legal` ends with `all 3 legal fixtures ready and searchable`.
- [ ] One complete run of sections 0–3 (and 4, if kept) against a timer. Target 11–13 minutes of talking.
- [ ] Recording made and played back once ([Recording outline](#recording-outline)). Copy it to a USB stick or the presentation laptop.
- [ ] Slides include the architecture diagram and the "What was tested where" table.
- [ ] **Required to finish M8:** a teammate follows [setup-clean-checkout.md](setup-clean-checkout.md) from a fresh clone on their own machine and records the commit, OS, commands, and any deviations. The M8 roadmap exit criterion requires it ("another team member reproduces setup and results"). Not done yet.

**Defense day, 15 minutes before**

- [ ] Laptop on mains power; notifications, auto-updates and screen lock off.
- [ ] Other stacks stopped if you want more free RAM (your decision; see risk R7).
- [ ] Full reset done. Pre-flight `smoke_stack.py` ends with `smoke: all checks passed`.
- [ ] Mailpit (http://localhost:18025) shows **0** messages.
- [ ] Fresh passwords looked up and kept off the shared screen.
- [ ] Browser: app tab and Mailpit tab open, zoom about 125 %, developer tools closed, not signed in yet.
- [ ] Recording file open in a player, paused at 0:00, as the fallback.
- [ ] Do **not** run `m7_acceptance.py runners` or `m6_demo.py` now (they add Mailpit messages).

**During the demo**

- [ ] Say "FAKE INFERENCE is a labelled stand-in; real-model evidence comes at the end."
- [ ] Sign in fresh at each section. Never reload the page (a reload signs you out).
- [ ] Click **Run report**; Enter only adds a new line.
- [ ] Open document citations from the blue **[n]** inside the answer text.
- [ ] Approve and send within 15 minutes.

## Rehearsal results (27 November 2026)

One full rehearsal on the disposable `eaios-m8def` stack, built from a fresh clone of `491dfba` with the labelled fake model. The built-in Chromium browser of the Claude desktop app drove the UI at http://localhost:23000; Mailpit was at :28025. The owner's `eaios` and `eaios-m7acc` stacks kept running and were not touched (checked afterwards: 9 `eaios-m7acc` containers still up, 5 `eaios_*` volumes present).

| Step | Result | Notes |
| --- | --- | --- |
| Setup from the clean clone | **PASS** after finding 1 | `var/` was created by root on `up`; `seed` failed to write credentials. Recovered as the setup guide describes |
| Runners: `smoke_stack`, `m2_demo_capture`, `m3_demo capture`, `m6_demo` | **Completed** (all exit 0); `smoke_stack`, `m2_demo_capture` and `m6_demo` **PASS** on their own checks | Before the reset; their Mailpit message was removed by the reset. `m3_demo capture` completed and recorded the authorized answer and citation, but its exit code does **not** prove the refusal: with the default question its "no access" leg returned an answer (`outcome=published`) and it still exited 0. The refusal was verified separately (rows 1.3 below) |
| Full reset (remove volumes, `up`, all seeds) | **PASS**, 76 s | Mailpit total 0 afterwards |
| 1.1–1.2 sales.reader identity and document list | **PASS** | No finance commentary, no board minutes |
| 1.3 refusal — **original M7 question** "How many invoices were overdue at the reference date?" | **Did not refuse** | With the legal contracts seeded, it returned a verified but off-topic answer citing the Red Sea agreement: *"NileTech shall pay each invoice within 30 days of the invoice date. [1]"*. Nothing leaked (sales.reader may read that contract), but it contradicts the M7 demo's expected text. The M7 runs refused because the runners ran **before** `seed-legal` (M7 capture: `no_sources`) |
| 1.3 refusal — **replacement question** "Why did collections lag revenue in Q1 2026?" | **PASS** | *"I couldn't find information in documents you can access that answers this."* Probed first through `m3_demo.py capture --question` (finance.reader `published`, sales.reader `abstained / no_sources`) |
| 1.5–1.7 finance.reader answer and citation | **PASS** | Cited sentence from *Q1 2026 finance commentary* v1; passage highlighted, **text hash verified**. Only the inline **[1]** opens the passage; the footnote line does not |
| 2 verified sales report and evidence | **PASS** | 291 / USD 80,934,385.00 / 11,330,813.90 / 92,265,198.90; [2] shows the SQL and **hash verified** |
| 3.1 unauthorized recipient | **PASS** | *"The recipient is not authorized to see this report."* |
| 3.2–3.6 approve, edit, new version | **PASS** | **Send now** disappeared on edit; v2 *Draft — not approved*; Mailpit 0 |
| 3.7–3.9 approve v2, send, exactly one delivery | **PASS** | *delivered · attempts 1 · Message-ID `<b496c1bc-0566-43c9-87fb-66c4a266bbda@eaios.local>`*. Mailpit total **1** at 18:36:09 and still **1** at 18:36:43 UTC; same Message-ID, subject *… (edited)*, to finance.reader |
| 4 contracts | **PASS** | Red Sea 30 June 2026; Aswan 31 March 2027, notice by 31 December 2026; fixed-rule verification text; NileCloud absent |
| 5 optional extras | Not rehearsed | Passed in the M7 walkthrough (26 Nov) |

### Timings

| Item | Time |
| --- | --- |
| Each report/answer from **Run report** to the published result (fake model) | under 1 s (M2 capture: done at 0.64 s) |
| Sections 1–4, clicking only (no talking) | about 6 minutes |
| Expected with talking points | 11–13 minutes, plus 1–2 minutes closing |
| `up` with a warm Docker cache | 63 s |
| No-cache rebuild of the backend and web images | Not completed: stopped after about 18 minutes, still in `pip install`. PyPI downloads on this network ran at about 40 kB/s (for example pglast 6.3 MB in 1:43). A cold first build depends on the network, so build the day before, on a good connection |
| Full reset with a warm cache | 76 s |

## Risks that could interrupt the presentation

| # | Risk | Likelihood | Mitigation |
| --- | --- | --- | --- |
| R1 | **Session expires after 15 minutes** (`ACCESS_TOKEN_TTL_SECONDS=900`), and the web app has no token refresh | Medium, if you sign in early or talk long | Sign in at the start of each section. If a step fails, sign out and back in. For the recording only, you may raise `ACCESS_TOKEN_TTL_SECONDS` in `var/m7-acceptance/.env` and run `up` (this is configuration, not a product change) |
| R2 | **Approval expires after 15 minutes** (`ACTION_APPROVAL_TTL_SECONDS=900`) | Low | Do 3.3–3.7 without long pauses; if it expires, approve again |
| R3 | **The M7 demo's §1 question no longer refuses** once the contracts are seeded (see results) | Certain, with the old question | This script uses the replacement question. The M7 DEMO.md text is unchanged in this PR. Related tooling gap: `m3_demo.py capture` exits 0 even when the "no access" reader receives an answer |
| R4 | **Leftover Mailpit messages** break "exactly one" | Medium after rehearsals or runners | Full reset right before presenting; check that Mailpit shows 0 |
| R5 | Passwords change on every reset and could appear on the projector | Medium | Look them up after the final reset; keep them off the shared screen |
| R6 | Page reload signs you out (by design) | Low | Never reload the app tab; reload only the Mailpit tab |
| R7 | Memory pressure with three stacks on a 16 GB laptop (about 7 GB was free during the rehearsal) | Low | Optionally stop the stacks you don't need on the day (your call) |
| R8 | The first build on a new machine needs the network and takes several minutes | Only on a new machine | Build the day before; the demo then needs no network |
| R9 | The Red Sea expiry (30 June 2026) is in the past but worded "expires on" | Low (question from the panel) | Acknowledge it as a known wording limitation |
| R10 | Path A (`m7_acceptance.py`) was **not rerun on `491dfba`** in this rehearsal, because the owner's `eaios-m7acc` stack was in use and was left alone | Low: it last passed at `7c13282` in the M7 closeout, and `git diff 7c13282 491dfba` touches only docs | Do one full reset + section 1–3 run on `eaios-m7acc` before the defense |
| R11 | No live real model during the demo | Certain (by design; Colab cannot serve reliably) | Show the recorded Colab evidence; say so up front |

Live Drive and GitHub remain **optional** and out of the demo: the GitHub token is absent, and Drive needs a second Google account and a person to remove the share.

## Recording outline

A screen recording is the fallback if the live demo fails, and it can be submitted with the defense package. Record the presentation stack at 1920×1080 with the browser zoom at 125 %. Use a microphone, or add captions afterwards. Keep it under 12 minutes.

| # | Segment | Length | Content |
| --- | --- | --- | --- |
| 1 | Title and architecture | 0:45 | Slide: project, team, and the architecture diagram. Say that the live parts use the labelled fake model |
| 2 | Permissions and citations | 2:30 | Demo section 1: sales.reader refusal → finance.reader cited answer → passage with **text hash verified** |
| 3 | Verified sales report | 1:45 | Section 2: progress steps, the four figures, evidence [2] with SQL and **hash verified** |
| 4 | Approval and Mailpit | 3:00 | Section 3: unauthorized recipient, approve v1, edit cancels it, v2, Mailpit 0, approve v2, send, Mailpit 1, reload after 30 s, still 1 |
| 5 | Contracts (optional) | 1:00 | Section 4 |
| 6 | Real-model evidence | 1:30 | Show `docs/evidence/m7-functional/2026-11-26-colab-real-model-smoke/checks.json` and `versions.json` (Qwen3-8B-AWQ revision, T4, gateway labels), the same figures, and Mailpit total 1. Reuse the existing evidence; no new GPU run |
| 7 | Limitations and close | 0:45 | From [architecture-and-evidence.md § Known limitations](architecture-and-evidence.md#known-limitations-to-state-at-the-defense) |

Before recording: full reset, and Mailpit at 0. After recording, check that no password, `.env` content or token appears in any frame.
