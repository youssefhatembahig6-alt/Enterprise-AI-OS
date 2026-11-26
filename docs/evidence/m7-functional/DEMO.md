# M7 demo script (about 15 minutes, fake model)

A walkthrough of the main workflows in the web UI. It uses the disposable stack, so your own development stack and data are untouched. Every run is labelled **FAKE INFERENCE**. Say so when presenting: this shows the product's behaviour, not model quality.

## Before you start (once, about 5 minutes)

```bash
python3 scripts/m7_acceptance.py init-env
python3 scripts/m7_acceptance.py up
python3 scripts/m7_acceptance.py seed
python3 scripts/m7_acceptance.py seed-legal
```

Look up a password without printing the others (repeat for each user you need):

```bash
python3 -c "import json; print(json.load(open('var/m7-acceptance/dev-credentials.json'))['niletech']['finance.reader@niletech.example'])"
```

Open http://localhost:13000 (app) and http://localhost:18025 (Mailpit inbox). The organization for every sign-in is `niletech`. Use **Sign out** between users; reloading the page also signs you out.

> The disposable frontend uses the CSP workaround for bug B1 (see README). Without it the UI on port 13000 says "API unreachable".

## 1. Permissions decide what you can see (2 minutes)

1. Sign in as **sales.reader@niletech.example**. Point out the identity card (department sales, clearance internal) and the **Documents** list: no *Q1 2026 finance commentary*, no *Board minutes*.
2. **Ask** with source **Documents you can access**: `How many invoices were overdue at the reference date?`
   Expected: *"I couldn't find information in documents you can access that answers this."* There's no hint that the finance document exists.
3. Sign out.

## 2. A verified sales report with evidence (2 minutes)

1. Sign in as **finance.reader@niletech.example**.
2. Source **Sales report (SQL)**, click **Use example**, then **Run report**.
3. Watch the progress steps. Expected: **Fulfilled sales, Q1 2026 — Verified**: 291 orders, net **USD 80,934,385.00**, tax USD 11,330,813.90, gross USD 92,265,198.90.
4. Click citation **[2]**. The evidence panel shows the exact stored row, the read-only SQL and "hash verified". Close it.

## 3. Email only after a human approves the exact version (4 minutes)

1. Under **Email "Fulfilled sales, Q1 2026"**, choose recipient **Sales Reader** and click **Create draft for review**. Expected: *"The recipient is not authorized to see this report."*
2. Choose **Finance Reader**, then **Create draft for review**. The draft opens: "Draft — not approved".
3. Click **Approve this version**. It reads "You approved version 1 … any edit cancels it", and **Send now** appears.
4. Add ` (edited)` to the subject. **Send now** disappears. Click **Save as new version**. It's now **Version 2**, "Draft — not approved". Show Mailpit: nothing new.
5. Click **Approve this version**, then **Send now**. Expected: "Delivered to Mailpit", attempts 1, with a Message-ID.
6. In Mailpit, open the message (subject ends with "(edited)"). Refresh after half a minute: still exactly one copy.

## 4. Scheduled briefings never send on their own (2 minutes, then wait about 1 minute)

1. Under **Scheduled briefings**, enter `What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?`, choose **Finance Reader** and **every day**, then click **Schedule briefing**. The first run is due in about a minute.
2. Talk through section 5 meanwhile. Then click **Refresh** under **Your drafts**. A new **draft** appears, made by the briefing. Mailpit has received nothing new.

## 5. Contracts with exact citations (1 minute)

1. Still as finance.reader, choose source **Contracts (clauses and expiry)** and ask `When do our contracts expire?`
2. Expected: Red Sea expires 30 June 2026; Aswan's initial term ends 31 March 2027, with renewal notice due by 31 December 2026. The restricted NileCloud licence isn't mentioned.
3. Open an Aswan citation: the contract passage with "text hash verified".
4. Known issue (B3): the line under "Verified against sources" wrongly talks about a "query result". Mention it if anyone asks.
5. Sign out.

## 6. Upload, answer, revoke (4 minutes)

1. Sign in as **access.admin@niletech.example**. In **Documents → Upload**, choose `backend/fixtures/m7-acceptance/sales-territory-memo.md`. Set Department `sales` and Classification **Internal**. Leave Share with groups as `all-staff` (it's pre-filled; don't type it again). Click **Upload**.
   Known issue (B2): the label can say "Not searchable" until you sign in again. The document is indexed.
2. Open a **second tab** at http://localhost:13000 and sign in as **sales.reader** there. Source **Documents you can access**, ask: `What is the quarterly visit target for the Delta territory?`
   Expected: verified answer, *"48 customer visits per field representative [1]"*. Click [1] to see the exact passage.
3. Back in the admin tab: **Administration → Users**. In **Sales Reader**'s row, clear the groups field (`all-staff`) and click the **Save** next to it.
4. In the sales reader tab, without reloading, click [1] again. Expected: *"This source is not available to you (your access may have changed or the document was removed)."* Ask the same question again: *"I couldn't find information…"*

## Afterwards

Put `all-staff` back on Sales Reader (admin tab, same field), or reset everything:

```bash
python3 scripts/m7_acceptance.py destroy && rm var/m7-acceptance/dev-credentials.json
```

(Run `up`, `seed` and `seed-legal` again before the next demo.)
