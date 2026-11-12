# Invoice Autopilot — PS-1 (Finance / AP)

Takes a vendor invoice PDF, reads it, checks it against a mock procurement system and returns a
reasoned decision — **APPROVED**, **FLAGGED** (for human review) or **REJECTED** — with every step visible.

- **Live run view**: eight stages appear one by one as they execute.
- **Dashboard**: history, status and outputs across runs, with a drill-down into any run.
- **Review queue**: a person resolves anything the process would not decide alone.
- **Procurement data**: PO balances change as invoices are approved.

---

## 1. Run it locally (about 3 minutes)

Needs Python 3.10 or newer.

```bash
cd invoice-autopilot

# create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501. It works immediately **without any API key** using the local
parser. Pick "1 · Happy path" from the Sample list and press **Process invoice**.

### Turn on Gemini (free) — needed for scanned invoices

1. Create a free key at https://aistudio.google.com/apikey (no card needed).
2. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and paste the key in.
   (Or set an environment variable: `export GEMINI_API_KEY=...`; Windows: `set GEMINI_API_KEY=...`.)
3. Restart the app, open **Rules & settings → Test Gemini connection**. It tells you which model it will use.

If the test says none of the configured models are available, it lists the ones your key can see.
Set that name in `secrets.toml`, e.g. `GEMINI_MODELS = "gemini-3.5-flash-lite"` (comma-separated, tried in order).

### Run the tests

```bash
pip install -r requirements-dev.txt
pytest -q                           # 11 tests, no key or network needed
python scripts/generate_samples.py  # only if you want to regenerate the sample PDFs
```

---

## 2. Host it (free) — Streamlit Community Cloud

1. Create a new GitHub repository and push this folder:
   ```bash
   git init && git add . && git commit -m "Invoice Autopilot"
   git branch -M main
   git remote add origin https://github.com/<you>/invoice-autopilot.git
   git push -u origin main
   ```
   `.gitignore` already keeps your key (`secrets.toml`) and runtime data out of the repo.
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app → Deploy a public app from GitHub**.
3. Repository: yours · Branch: `main` · Main file path: `app.py`.
4. Open **Advanced settings**: choose Python 3.12, and in **Secrets** paste:
   ```toml
   GEMINI_API_KEY = "your-key"
   ```
5. **Deploy**. You get a public `https://<name>.streamlit.app` link — that is the "live and runnable" link to submit.

Things to know before the interview:

- The app **sleeps after a period with no visitors**. Open the link 10 minutes before the call to wake it.
- Storage on the free tier is temporary: run history returns to the starting state when the app restarts. For a demo that is what you want.
- Press **Rules & settings → Reset demo data** before every demo/recording so PO balances and duplicate
  detection start from a known state.
- Free Gemini keys have a small daily request allowance. Rehearse with the **Local parser** engine and save
  Gemini for the recording and the scanned sample. If Gemini is rate-limited mid-demo the run does not break: it
  falls back to the local parser and says so on screen.

Alternatives if you prefer: Hugging Face Spaces (SDK: Streamlit) or Render (start command
`streamlit run app.py --server.port $PORT --server.address 0.0.0.0`).

---

## 3. Process map

```
PDF ─▶ 1 Intake ─▶ 2 Extract ─▶ 3 Validate ─▶ 4 Vendor ─▶ 5 Duplicate ─▶ 6 PO match ─▶ 7 Reconcile ─▶ 8 Decide & post
        text layer?  Gemini or     fields +      approved    3 tiers       explicit or    balance,        update PO balance,
        page count   local parser  arithmetic    vendor?                   inferred       tolerance,      ledger, run history
                                                                                          line causes
```

| Stage | What it decides | Stops the run? |
|---|---|---|
| 1 Intake | Is it a readable PDF? Digital or scanned? | Yes, if unreadable |
| 2 Extract | Vendor, invoice no., date, PO, lines, subtotal, tax, total | Yes, if nothing can be read |
| 3 Validate | Required fields present; lines + tax = total | Only if vendor or total is missing |
| 4 Vendor | In the vendor master and approved (tolerates "Inc." / "&" / small typos) | Yes → REJECTED |
| 5 Duplicate | Exact number · same vendor+date+total · likely re-issue | Yes → REJECTED (first two tiers) |
| 6 PO match | Use the PO on the invoice, or infer a unique one | No — flags and continues |
| 7 Reconcile | Compare with the PO's **open balance**; explain the cause of any difference | No |
| 8 Decide | Rejections outrank flags, flags outrank approvals; post only approvals | — |

**Design choice worth saying out loud:** the LLM only *reads* the document. Every decision is made by
plain, testable code (`core/rules.py`), so the same invoice always gets the same answer and each answer can be
explained line by line.

---

## 4. Edge cases

### The four headline cases (demo these)

| # | Sample file | Scenario | Decision |
|---|---|---|---|
| 1 | `02_ghost_po_missing_po_number.pdf` | **Ghost PO** — no PO number. Vendor has two open POs; exactly one has an open balance equal to the invoice total, so it is attached. | `APPROVED - AUTO-MATCHED PO` |
| 2 | `03_freight_within_tolerance.pdf` | **Sneaky freight** — lines match the PO, plus a $25 freight line not on it. $25 is 1.93% of the PO, inside tolerance. The freight line is named as the cause. | `APPROVED - WITHIN TOLERANCE` |
| 3 | `04_phantom_duplicate.pdf` | **Phantom duplicate** — INV-102 (already in the ledger, awaiting payment) re-sent as INV-102-A. Caught on vendor + date + total, not on the number. | `REJECTED - SUSPECTED DUPLICATE` |
| 4 | `05a_…1_of_2.pdf` then `05b_…2_of_2.pdf` | **Split billing** — $4,000 then $6,000 against a $10,000 PO. The balance is drawn down, then the PO closes. | `APPROVED - PARTIAL DRAWDOWN`, then `APPROVED - FINAL DRAWDOWN` |

Changes I made to the original brief, and why:

- **Tolerance is "5% AND $50", not "5% OR $50".** With "or", a $10,000 PO could be overrun by $500 without anyone
  seeing it. The overage must pass both limits; both are editable on the settings page.
- **PO inference never uses tolerance.** A guessed PO must match to the cent and be the only candidate. If two POs fit,
  the invoice is flagged for a person to choose rather than guessed.
- **Invoices are compared with the PO's remaining balance, not its original value** — that is what makes split
  billing and over-drawing work.
- **Added `APPROVED - FINAL DRAWDOWN`** so the second half of a split PO reads clearly in the dashboard.

### Guardrails also built (supporting cases, each with a sample)

| Sample | Scenario | Decision |
|---|---|---|
| `01_happy_path.pdf` run twice | Same invoice number again | `REJECTED - DUPLICATE INVOICE` |
| `06_scanned_invoice.pdf` | Image-only scan. Gemini reads it; without Gemini it is routed to a person instead of guessed. | `APPROVED` with Gemini · `FLAGGED - UNREADABLE DOCUMENT` without |
| `07_over_tolerance.pdf` | Unit price raised from $0.55 to $0.70 — 15% over the PO. The price change is named as the cause. | `FLAGGED - OVER TOLERANCE` |
| `08_unapproved_vendor.pdf` | Unknown vendor quoting another vendor's real PO number | `REJECTED - VENDOR NOT APPROVED` |
| `09_embedded_tax_bundled.pdf` | One bundled line, tax embedded in the total, PO only mentioned in a sentence | `APPROVED` |

Handled in code without a dedicated sample: missing invoice number/date (`FLAGGED - INCOMPLETE INVOICE`), lines that
do not add up to the total (`FLAGGED - MATH MISMATCH`), PO not found, PO belongs to another vendor, PO already fully
invoiced, currency mismatch, several POs fitting a PO-less invoice (`FLAGGED - AMBIGUOUS PO MATCH`), and a likely
re-issue with a different date (`FLAGGED - POSSIBLE DUPLICATE`).

---

## 5. Suggested 5-minute demo order

Reset demo data first.

1. **Happy path** (`1`) — walk the eight stages once, slowly. Show the PO balance change.
2. **Ghost PO** (`2`) — point at stage 6: "no PO on the invoice, inferred because it is unique and exact".
3. **Sneaky freight** (`3`) — stage 7 names the $25 freight line; open settings to show the threshold is configurable.
4. **Phantom duplicate** (`4`) — stage 5 stops the run; nothing is posted.
5. **Split billing** (`5a`, then `5b`) — open *Procurement data* between the two to show $6,000 still open.
6. **Dashboard** — straight-through rate, history, drill into one run, download its audit record.
7. If time: **Price hike** (`7`) → *Review queue* → approve with a note (human in the loop).

---

## 6. Assumptions

- PO values include tax; PO lines are pre-tax.
- One invoice maps to one PO.
- An invoice that was rejected does not block a corrected re-submission; one that is approved or in review does.
- Unknown vendors are rejected rather than flagged: onboarding is a separate process (PS-2).
- Amounts are compared to the cent.

## 7. Known limits / what I would build next

- The local parser is a fallback tuned to common invoice label patterns; unusual layouts need Gemini.
- No local OCR: a scanned invoice with no Gemini available is sent to review, not read.
- JSON files stand in for the procurement system. `core/store.py` is the only file to replace to use a real database or ERP.
- No multi-PO invoices, credit notes, or three-way match against goods receipts yet.
- Single user: no login, and two simultaneous runs could race on the JSON files.

## 8. Project layout

```
app.py                     Streamlit UI (live run, dashboard, review queue, data, settings)
core/extractor.py          Stage 2: Gemini + local parser, one output shape
core/rules.py              Business rules as pure functions
core/pipeline.py           The eight stages, decision logic, posting, human review
core/store.py              Mock procurement system (JSON)
data/seed/                 Starting state: vendors, purchase orders, invoice ledger, settings
sample_invoices/           10 demo PDFs + manifest.json
scripts/generate_samples.py  Regenerates the PDFs
tests/test_pipeline.py     11 tests covering every scenario
```
