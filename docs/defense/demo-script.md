# Defense demo script (10–15 minutes)

The demo runs on the **presentation stack** ([setup Path A](setup-clean-checkout.md#path-a--presentation-stack-recommended-for-the-defense)): app at http://localhost:13000, Mailpit at http://localhost:18025, organization `niletech` for every sign-in. Every run is labelled **FAKE INFERENCE**. Say this once at the start: *"The live demo uses a labelled stand-in model so it is deterministic and needs no GPU. The same workflows were run with the real Qwen model in Colab; I'll show that evidence at the end."*

Priorities, in order: **(1)** document permissions and citations, **(2)** a verified sales report, **(3)** approval followed by exactly one Mailpit delivery. Sections 4 and 5 are optional and can be dropped to fit the time.

Rehearsed on 27 November 2026 on commit `491dfba` with the earlier single-page dashboard ([rehearsal.md](rehearsal.md)). **Updated for the NileTech Assistant layout** (PR #21): after sign-in the app opens on the chat (**Ask**), and the icon rail on the left holds **Files and sources**, **Approvals**, **Activity log** (administrators) and **Settings**. The figures, refusals and Mailpit behaviour are the same; only where you click has changed.

**Layout cheat sheet.** The composer has a **Source** menu inside it, next to the round send button. **Enter** sends the question. The small teal numbers in an answer are citations: clicking one opens the **Sources** panel on the right and asks the server for that passage again. The top bar says *"Limited to what <name> can open"* and shows the orange **FAKE model** chip. Sign out from the avatar at the bottom of the rail.

## Before the audience arrives (10 minutes)

1. Reset to clean fixtures ([Reset procedure](#reset-procedure)). Then run the pre-flight `smoke_stack.py` command from the setup guide. It must end with `smoke: all checks passed`.
2. Look up the passwords for `sales.reader`, `finance.reader` (and `access.admin` if you plan the optional extra). The file changes on every reset. Keep the passwords off the shared screen: use a password manager, or type them from a second device.
3. Mailpit: open http://localhost:18025. The inbox must show **0** messages.
4. Browser: tab 1 at http://localhost:13000, tab 2 at http://localhost:18025. Use a window at least 1200 px wide, so the sidebar and the Sources panel sit side by side; zoom about 110 %. Close the developer tools.
5. Sessions last **15 minutes** and the web app does not refresh them. Sign in fresh at the start of each section, as written below; do not sign in early.

## 0. Opening (1 minute)

Show the architecture slide ([architecture-and-evidence.md](architecture-and-evidence.md#architecture-at-a-glance)). After the first sign-in, point to the orange **FAKE model (development double)** chip in the top bar, and to the line under the composer: *"Each question is answered on its own…"*.

## 1. Document permissions and exact citations (3 minutes)

| # | Click / type | Expected result |
| --- | --- | --- |
| 1.1 | Sign in: Organization `niletech`, Email `sales.reader@niletech.example`, password → **Sign in** | Chat opens: *"What can I find for you today, Sales Reader?"*; top bar *"Limited to what Sales Reader can open: Sales · up to Internal"* |
| 1.2 | Rail → **Files and sources** | The document list has **no** *Q1 2026 finance commentary* and **no** *Board minutes, March 2026* |
| 1.3 | Rail → **Ask**. In the composer, **Source** → **Documents you can access**. Type `Why did collections lag revenue in Q1 2026?` → **Enter** | *"I couldn't find information in documents you can access that answers this."* with the pill **No sources used**. Nothing hints that a finance document exists |
| 1.4 | Avatar (bottom of the rail) → **Sign out** | Sign-in card; the chat history is gone |
| 1.5 | Sign in as `finance.reader@niletech.example` → rail **Files and sources** | The list now includes *Q1 2026 finance commentary* (confidential · finance) |
| 1.6 | Rail → **Ask**. **Source** → **Documents you can access**, same question → **Enter** | Steps ✓, then *"Collections lagged revenue because 370 open invoices were already past their due date at the reference date."* with citation **1**; pills **Every sentence quotes a source you can open** · **Claims checked (development checker)** |
| 1.7 | Click the teal **1** | **Sources** panel opens: *Q1 2026 finance commentary · finance-q1-commentary.md · version 1*, the sentence highlighted, badge **text hash verified** |

Talking point: retrieval is filtered by the reader's current permissions on every search, and opening a citation asks the server again, so the refusal looks the same whether a document is forbidden or does not exist (ADR 0303, 0304).

## 2. A verified sales report with evidence (2 minutes)

Still signed in as `finance.reader`. Optionally click **New chat** in the sidebar first; the earlier chat stays under **Recents**.

| # | Click / type | Expected result |
| --- | --- | --- |
| 2.1 | **Source** → **Sales report (SQL)**. Type `What were net (tax-exclusive) fulfilled sales for Q1 2026, in USD?` → **Enter** | Ten steps (*Drafted a read-only query … Verified figures and claims … Published the verified result*) |
| 2.2 | Read the answer | **Fulfilled sales, Q1 2026** table: Fulfilled orders **291**, Net **USD 80,934,385.00**, Tax **USD 11,330,813.90**, Gross **USD 92,265,198.90**; pills **Numbers match query rows** · **Summary checked (development checker)**; definition line: order-date basis, FULFILLED only, net is tax-exclusive |
| 2.3 | Click **2** next to Net sales | Sources panel: the stored result row (net_sales highlighted), the read-only `SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales …` query, badge **hash verified** |
| 2.4 | Close the panel (✕) | — |

Talking point: figures are compared exactly against the stored query result. The SQL runs under a separate read-only, row-scoped database role (ADR 0201, 0202, 0204).

## 3. Approval, then exactly one Mailpit delivery (4 minutes)

Still `finance.reader`, under the report from section 2.

| # | Click / type | Expected result |
| --- | --- | --- |
| 3.0 | Click **Email this report for approval** under the report | The **Approvals** page opens with **Email "Fulfilled sales, Q1 2026 · report …"**: the report you clicked, identified by its run reference |
| 3.1 | Recipient → **Sales Reader (unauthorized for finance)** → **Create draft for review** | Red alert: *"The recipient is not authorized to see this report."* |
| 3.2 | Recipient → **Finance Reader** → **Create draft for review** | Draft opens: **Draft — not approved**, Version 1 |
| 3.3 | **Approve this version** | *"You approved version 1. The approval expires at … and any edit cancels it."* **Send now** appears |
| 3.4 | Click the end of **Subject**, type ` (edited)` | **Send now** disappears immediately |
| 3.5 | **Save as new version** | *Version 2 (edited)*, **Draft — not approved** |
| 3.6 | Switch to the Mailpit tab | Still **0** messages |
| 3.7 | Back to the app: **Approve this version** → **Send now** | **Delivered to Mailpit** · *delivered · attempts 1 · Message-ID <…@eaios.local>* |
| 3.8 | Mailpit tab (reload) | Exactly **1** message, To `finance.reader@niletech.example`, subject *Verified report: Fulfilled sales, Q1 2026 (edited)*, body with the verified figures. Match the Message-ID if asked |
| 3.9 | After about 30 seconds, reload Mailpit again | Still exactly **1**. The reconciler never re-sends a confirmed delivery |

Do steps 3.3 to 3.7 within 15 minutes, because approvals expire. Talking point: the approval is bound to the exact draft version, the actor, the session, and the tenant. Forged, reused, and pre-edit approvals are rejected with 409 (shown by `m6_demo.py` and in the [M6 evidence](../evidence/m6/README.md)). If you asked for two reports, each report's own **Email this report for approval** button opens the form for that report.

## 4. Optional: contracts with fixed-rule verification (1 minute)

| # | Click / type | Expected result |
| --- | --- | --- |
| 4.1 | Rail → **Ask**. **Source** → **Contracts (clauses and expiry)**, type `When do our contracts expire?` → **Enter** | Red Sea: *expires on 30 June 2026*; Aswan: initial term ends *31 March 2027*, renewal notice *by 31 December 2026*. The restricted NileCloud licence is not mentioned |
| 4.2 | Read the pills and the note under them | **Exact contract passages** · **Dates re-derived by fixed rules · no model**; *"… Dates, terms and notice periods were re-derived from those passages by fixed rules; no model was used."* |
| 4.3 | Click **2** | The Aswan contract passage in the Sources panel, **text hash verified** |

Note: 30 June 2026 is already in the past on the defense date. The answer says "expires on", not "expired". If asked, say that the date is extracted exactly and that the wording is a known limitation.

Then avatar → **Sign out**.

## 5. Optional extras (only if more than 3 minutes remain)

Not rehearsed in this layout. Both passed in the M7 browser walkthrough ([M7 DEMO.md](../evidence/m7-functional/DEMO.md) §4 and §6); where to click now:

- **Scheduled briefing:** rail → **Approvals** → **Scheduled briefings**. It produces a draft and sends nothing. It needs a one-minute wait for the scheduler; then **Refresh** under **Your drafts**.
- **Upload, answer, revoke:** as `access.admin`, rail → **Files and sources** → upload the memo; in a second tab as `sales.reader`, ask in **Ask** and open the citation; then as `access.admin`, rail → **Activity log** → **Users** and clear Sales Reader's groups. Clicking the citation again shows *"This source is not available to you…"* instead of the passage.
- **Appearance:** rail → **Settings** → **Appearance** (mode, theme, accent, colours, font, corners, density). Choices are remembered on this device; **Reset** restores the NileTech theme.

## 6. Closing: real-model evidence (1–2 minutes)

Open [architecture-and-evidence.md § What was tested where](architecture-and-evidence.md#what-was-tested-where) and show the [Colab smoke `checks.json`](../evidence/m7-functional/2026-11-26-colab-real-model-smoke/checks.json). On a Colab T4, Qwen3-8B-AWQ produced the same verified figures, and the approved v2 produced exactly one Mailpit message. State the limitations: the checker is a development baseline with an uncalibrated threshold, and live Drive/GitHub are not part of the live demo.

## If something goes wrong

| Symptom | Do this |
| --- | --- |
| Top bar shows **API unreachable** | `docker compose -p eaios-m7acc ps`. If something is unhealthy, run `python3 scripts/m7_acceptance.py up` (about 1 minute), sign in again |
| Answers or drafts suddenly fail, or the page looks signed out | Session expired (15 min). Avatar → **Sign out** (or reload), sign in again, redo the last step |
| **Send now** missing after approving | An edit is unsaved, or the approval expired. **Save as new version**, then **Approve this version** again |
| Mailpit shows more than 1 message | Leftover from a rehearsal or a runner. Say so, point to the Message-ID matching the app, and reset before the next run |
| A report shows *model unavailable* | The stack was started without `MODEL_MODE=fake`. Check `var/m7-acceptance/.env`, then `up` |
| Nothing works | Switch to the recording ([rehearsal.md § Recording outline](rehearsal.md#recording-outline)) |

## Reset procedure

Full reset (recommended before every run; about 1.5 minutes with a warm Docker cache):

```bash
python3 scripts/m7_acceptance.py destroy        # ONLY eaios-m7acc containers + volumes
rm var/m7-acceptance/dev-credentials.json       # stale passwords
python3 scripts/m7_acceptance.py up
python3 scripts/m7_acceptance.py seed
python3 scripts/m7_acceptance.py seed-legal
```

Then look up the new passwords and check that Mailpit shows 0.

Quick reset (between two back-to-back runs, no rebuild): in Mailpit, click **Delete all** (only after the send shows *delivered*). The old draft stays listed under **Your drafts**. If you used section 5, put `all-staff` back on Sales Reader under rail → **Activity log** → **Users**. Use the full reset whenever possible.
