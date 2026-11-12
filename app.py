"""Invoice Autopilot - Streamlit UI.

Run:  streamlit run app.py

Pages
  Process invoice   live run view: each stage appears as it executes
  Dashboard         history, status and outputs across runs
  Review queue      human-in-the-loop for flagged invoices
  Procurement data  the mock procurement system (POs, vendors, ledger)
  Rules & settings  tolerance thresholds, extraction engine, demo reset
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from core import extractor, pipeline, report, store
from core.rules import money, tolerance_for

ROOT = Path(__file__).parent
SAMPLES = ROOT / "sample_invoices"
ICON = {"pass": "", "warn": "⚠️ ", "fail": "", "skip": "⏭️ "}
COLOR = {"APPROVED": ("#e8f6ee", "#137a3f"), "FLAGGED": ("#fff5df", "#9a6400"), "REJECTED": ("#fdecec", "#b42318")}

st.set_page_config(page_title="Invoice Autopilot", page_icon="🧾", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 2.2rem; max-width: 1250px;}
.decision {border-radius: 12px; padding: 18px 22px; margin: 6px 0 14px 0; border: 1px solid;}
.decision .code {font-size: 1.45rem; font-weight: 700; letter-spacing: .01em;}
.decision .sub {font-size: .95rem; margin-top: 4px; opacity: .9;}
.pill {display:inline-block; padding:2px 10px; border-radius:999px; font-size:.78rem; font-weight:600;}
</style>""", unsafe_allow_html=True)


# ------------------------------------------------------------------ helpers ---
def api_key():
    if st.session_state.get("session_key"):
        return st.session_state["session_key"]
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    try:
        return st.secrets.get("GEMINI_API_KEY")
    except Exception:  # no secrets file locally - that is fine
        return None


def sample_manifest():
    path = SAMPLES / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else []


@st.cache_data(show_spinner=False)
def preview_png(pdf_bytes: bytes):
    try:
        import pypdfium2 as pdfium
        return pdfium.PdfDocument(pdf_bytes)[0].render(scale=1.3).to_pil()
    except Exception:
        return None


def stage_label(stage, n):
    return f"{ICON[stage['status']]}{n}. **{stage['title']}** — {stage['summary']}".replace("$", r"\$")


def render_stage(stage, n, box=None):
    """Draw one finished stage (into an existing live box, or a new one)."""
    state = "error" if stage["status"] == "fail" else "complete"
    if box is None:
        box = st.status(stage_label(stage, n), state=state, expanded=stage["status"] in ("warn", "fail"))
    else:
        box.update(label=stage_label(stage, n), state=state, expanded=stage["status"] in ("warn", "fail"))
    with box:
        for d in stage["details"]:
            st.markdown(f"- {d}".replace("$", r"\$"))
        if not stage["details"]:
            st.caption("Nothing further to report.")


def decision_banner(record):
    d = record["decision"]
    bg, fg = COLOR[d["status"]]
    res = record.get("resolution")
    extra = ""
    if res:
        extra = (f"<div class='sub'><b>Reviewer decision:</b> {res['action'].upper()}D by {res['by']} "
                 f"on {res['at'].replace('T', ' ')} — {res['note'] or 'no note'}</div>")
    st.markdown(
        f"<div class='decision' style='background:{bg};border-color:{fg}33;color:{fg}'>"
        f"<div class='code'>{d['code']}</div><div class='sub'>{d['next_action']}</div>{extra}</div>",
        unsafe_allow_html=True)


def render_outcome(record):
    """Decision, reasoning and evidence for one run."""
    decision_banner(record)
    inv, d = record["extracted"] or {}, record["decision"]
    left, right = st.columns([1.15, 1])
    with left:
        st.markdown("**Why**")
        for r in d["reasons"]:
            st.markdown(f"- {r}".replace("$", r"\$"))
        if record.get("posting"):
            p = record["posting"]
            st.markdown("**What changed in procurement**")
            st.markdown(f"{p['po_number']}: open balance **{money(p['remaining_before'])}** → posted "
                        f"**{money(p['posted'])}** → **{money(p['remaining_after'])}** left "
                        f"(PO now {p['po_status'].replace('_', ' ')}).".replace("$", r"\$"))
    with right:
        st.markdown("**What was read from the invoice**")
        rows = [("Vendor", inv.get("vendor_name")), ("Invoice number", inv.get("invoice_number")),
                ("Invoice date", inv.get("invoice_date")),
                ("PO number", (record["po_number"] + "  (inferred)") if record.get("auto_matched_po")
                 else inv.get("po_number")),
                ("Subtotal", money(inv.get("subtotal"))),
                ("Tax", "embedded in total" if inv.get("tax_embedded") else money(inv.get("tax"))),
                ("Total", money(inv.get("total")))]
        st.dataframe(pd.DataFrame([(k, v or "— missing —") for k, v in rows], columns=["Field", "Value"]),
                     hide_index=True, width="stretch")
    if inv.get("line_items"):
        st.dataframe(pd.DataFrame(inv["line_items"]).rename(columns={
            "description": "Line item", "quantity": "Qty", "unit_price": "Unit price", "amount": "Amount"}),
            hide_index=True, width="stretch",
            column_config={"Unit price": st.column_config.NumberColumn(format="dollar"),
                           "Amount": st.column_config.NumberColumn(format="dollar")})
    st.caption(f"Run {record['run_id']} · {record['engine']} · {record['duration_ms']} ms · file {record['file_sha']}")
    suffix = f"{record['run_id']}_{st.session_state.get('page_key', '')}"
    a, b, _ = st.columns([1.3, 1.3, 2.4])
    a.download_button("Download decision report (PDF)", report.build_report(record),
                      file_name=f"decision_report_{record['run_id']}.pdf", mime="application/pdf",
                      type="primary", key=f"pdf_{suffix}", width="stretch",
                      help="A one-page, readable summary to save or forward.")
    b.download_button("Download audit record (JSON)", json.dumps(record, indent=2),
                      file_name=f"audit_{record['run_id']}.json", mime="application/json",
                      key=f"dl_{suffix}", width="stretch",
                      help="The complete run data, for other systems or an audit.")


# ------------------------------------------------------------ process page ---
def page_process():
    st.session_state["page_key"] = "process"
    st.title("Process an invoice")
    st.caption("Upload a vendor invoice PDF. Each of the eight stages appears as it runs; the decision and its "
               "reasoning follow.")
    left, right = st.columns([1, 1.25], gap="large")
    pdf_bytes, filename = None, None
    with left:
        source = st.radio("Invoice source", ["Sample invoice", "Upload a PDF"], horizontal=True)
        if source == "Sample invoice":
            manifest = sample_manifest()
            if not manifest:
                st.warning("No samples found. Run `python scripts/generate_samples.py`.")
            else:
                choice = st.selectbox("Sample", manifest, format_func=lambda m: m["label"])
                filename = choice["file"]
                pdf_bytes = (SAMPLES / filename).read_bytes()
        else:
            up = st.file_uploader("Invoice PDF", type=["pdf"])
            if up:
                pdf_bytes, filename = up.getvalue(), up.name
        key = api_key()
        engine_label = st.selectbox(
            "Extraction engine", ["Auto", "Gemini", "Local parser"],
            help="Auto uses Gemini when an API key is configured and falls back to the local parser if the call fails.")
        engine = {"Auto": "auto", "Gemini": "gemini", "Local parser": "local"}[engine_label]
        st.caption("Gemini key: " + ("configured ✓" if key else "not set — Auto will use the local parser "
                                     "(scanned PDFs need Gemini)"))
        go = st.button("Process invoice", type="primary", disabled=pdf_bytes is None, width="stretch")
        if pdf_bytes:
            img = preview_png(pdf_bytes)
            if img is not None:
                with st.expander("Invoice preview", expanded=True):
                    st.image(img, width="stretch")

    with right:
        st.subheader("Live run")
        if go:
            pace = st.session_state.get("pace", 0.6)
            boxes, record = {}, None
            for kind, payload in pipeline.run(pdf_bytes, filename, engine=engine, api_key=key):
                if kind == "start":
                    boxes[payload["key"]] = st.status(f"⏳  {payload['n']}. {payload['title']} …",
                                                      state="running", expanded=False)
                    time.sleep(pace)
                elif kind == "done":
                    n = [k for k, _ in pipeline.STAGES].index(payload["key"]) + 1
                    render_stage(payload, n, boxes[payload["key"]])
                else:
                    record = payload
            st.session_state["last_run_id"] = record["run_id"]
            st.session_state["just_ran"] = record
        else:
            record = st.session_state.get("just_ran")
            if record is None:
                st.info("Pick a sample or upload a PDF, then press **Process invoice**.")
            else:
                for n, stage in enumerate(record["stages"], 1):
                    render_stage(stage, n)
    record = st.session_state.get("just_ran")
    if record:
        st.divider()
        st.subheader(f"Decision · {record['filename']}")
        render_outcome(record)


# ---------------------------------------------------------- dashboard page ---
def runs_frame(runs):
    return pd.DataFrame([{
        "Time": r["timestamp"][5:].replace("T", " "), "Invoice #": (r["extracted"] or {}).get("invoice_number") or "—",
        "Vendor": r.get("vendor_name") or "—", "Total": r.get("total"), "Decision": r["decision"]["code"],
        "Status": pipeline.final_status(r), "PO": r.get("po_number") or "—", "File": r["filename"],
        "Engine": r["engine"], "ms": r["duration_ms"], "Run ID": r["run_id"]} for r in reversed(runs)])


def page_dashboard():
    st.session_state["page_key"] = "dash"
    st.title("Dashboard")
    runs = store.runs()
    if not runs:
        st.info("No runs yet. Process an invoice and it will appear here.")
        return
    status = [pipeline.final_status(r) for r in runs]
    auto = sum(r["decision"]["status"] == "APPROVED" for r in runs)
    pending = sum(s == "FLAGGED" for s in status)
    approved_value = sum((r["total"] or 0) for r, s in zip(runs, status) if s.startswith("APPROVED"))
    blocked_value = sum((r["total"] or 0) for r, s in zip(runs, status) if s.startswith("REJECTED"))
    c = st.columns(5)
    c[0].metric("Invoices processed", len(runs))
    c[1].metric("Straight-through rate", f"{auto / len(runs):.0%}", help="Approved with no human touch")
    c[2].metric("Waiting for review", pending)
    c[3].metric("Approved value", money(approved_value))
    c[4].metric("Payments blocked", money(blocked_value), help="Value of rejected invoices")

    df = runs_frame(runs)
    a, b = st.columns([1, 1.9], gap="large")
    with a:
        st.markdown("**Outcomes by decision**")
        counts = df["Decision"].value_counts().rename_axis("Decision").reset_index(name="Invoices")
        st.dataframe(counts, hide_index=True, width="stretch")
    with b:
        st.markdown("**Run history**")
        pick = st.multiselect("Filter by status", sorted(df["Status"].unique()), placeholder="All statuses")
        view = df[df["Status"].isin(pick)] if pick else df
        st.dataframe(view.drop(columns=["Run ID"]), hide_index=True, width="stretch", height=250,
                     column_config={"Total": st.column_config.NumberColumn(format="dollar")})
        st.download_button("Export history (CSV)", df.to_csv(index=False), "invoice_runs.csv", "text/csv")

    st.divider()
    st.subheader("Inspect a run")
    by_id = {r["run_id"]: r for r in runs}
    ids = [r["run_id"] for r in reversed(runs)]
    default = ids.index(st.session_state["last_run_id"]) if st.session_state.get("last_run_id") in ids else 0
    rid = st.selectbox("Run", ids, index=default, format_func=lambda i: (
        f"{by_id[i]['timestamp'][11:]}  ·  {by_id[i]['filename']}  ·  {by_id[i]['decision']['code']}"))
    record = by_id[rid]
    render_outcome(record)
    with st.expander("Stage-by-stage trace"):
        for n, stage in enumerate(record["stages"], 1):
            render_stage(stage, n)


# -------------------------------------------------------------- review page ---
def page_review():
    st.title("Review queue")
    st.caption("Invoices the process would not decide alone. The reviewer sees the evidence, decides, and the "
               "decision is recorded against the run.")
    queue = [r for r in store.runs() if r["decision"]["status"] == "FLAGGED" and not r["resolution"]]
    if not queue:
        st.success("Nothing waiting for review.")
        return
    for r in reversed(queue):
        inv = r["extracted"] or {}
        with st.container(border=True):
            st.markdown(f"**{r['decision']['code']}** · {r.get('vendor_name') or 'Unknown vendor'} · "
                        f"{inv.get('invoice_number') or 'no invoice number'} · {money(r.get('total'))}".replace("$", r"\$"))
            for reason in r["decision"]["reasons"]:
                st.markdown(f"- {reason}".replace("$", r"\$"))
            note = st.text_input("Reviewer note", key=f"note_{r['run_id']}",
                                 placeholder="e.g. Price increase confirmed with buyer by email")
            a, b, _ = st.columns([1, 1, 4])
            if a.button("Approve", key=f"ap_{r['run_id']}", type="primary"):
                pipeline.resolve_review(r["run_id"], "approve", note)
                st.rerun()
            if b.button("Reject", key=f"rj_{r['run_id']}"):
                pipeline.resolve_review(r["run_id"], "reject", note)
                st.rerun()


# ------------------------------------------------------------- procurement ---
def page_data():
    st.title("Procurement data")
    st.caption("The mock procurement system the process reads from and posts to (JSON files in `data/`).")
    settings = store.settings()
    vendors = {v["vendor_id"]: v["name"] for v in store.vendors()}
    t1, t2, t3 = st.tabs(["Purchase orders", "Approved vendors", "Invoice ledger"])
    with t1:
        pos = pd.DataFrame([{
            "PO": p["po_number"], "Vendor": vendors.get(p["vendor_id"], p["vendor_id"]), "PO value": p["total"],
            "Open balance": p["remaining"], "Invoiced": (p["total"] - p["remaining"]) / p["total"],
            "Tolerance": tolerance_for(p["total"], settings), "Status": p["status"].replace("_", " "),
            "Invoices": ", ".join(str(i["invoice_number"]) for i in p["invoices"]) or "—"} for p in store.purchase_orders()])
        st.dataframe(pos, hide_index=True, width="stretch", column_config={
            "PO value": st.column_config.NumberColumn(format="dollar"),
            "Open balance": st.column_config.NumberColumn(format="dollar"),
            "Tolerance": st.column_config.NumberColumn(format="dollar"),
            "Invoiced": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})
    with t2:
        st.dataframe(pd.DataFrame(store.vendors()), hide_index=True, width="stretch")
    with t3:
        st.dataframe(pd.DataFrame(store.ledger()), hide_index=True, width="stretch",
                     column_config={"total": st.column_config.NumberColumn(format="dollar")})


# ---------------------------------------------------------------- settings ---
def page_settings():
    st.title("Rules & settings")
    s = store.settings()
    st.subheader("Tolerance threshold")
    st.caption("An invoice may exceed its PO only if the overage is inside BOTH limits. These are business "
               "settings, not code: change them here and the next run uses them.")
    a, b = st.columns(2)
    pct = a.number_input("Percentage of PO value (%)", 0.0, 25.0, float(s["tolerance_pct"]), 0.5)
    cap = b.number_input("Hard cap ($)", 0.0, 1000.0, float(s["tolerance_abs"]), 5.0)
    if st.button("Save thresholds"):
        store.save_settings({**s, "tolerance_pct": pct, "tolerance_abs": cap})
        st.success("Saved.")

    st.subheader("Extraction engine")
    key = api_key()
    st.write("Gemini API key:", "configured ✓" if key else "not configured")
    typed = st.text_input("Paste a key for this browser session only (never written to disk)", type="password")
    if typed:
        st.session_state["session_key"] = typed.strip()
        key = typed.strip()
    if st.button("Test Gemini connection", disabled=not key):
        try:
            ok, msg = extractor.test_gemini(key)
        except Exception as exc:
            ok, msg = False, f"Could not reach Gemini: {exc.__class__.__name__}"
        (st.success if ok else st.error)(msg)
    st.caption(f"Model order (env `GEMINI_MODELS`): {', '.join(extractor.gemini_models())}")

    st.subheader("Demo controls")
    st.session_state["pace"] = st.slider("Live-run pacing: pause before each stage (seconds)", 0.0, 1.5,
                                         float(st.session_state.get("pace", 0.6)), 0.1)
    st.caption("Reset restores every PO balance, clears run history and the review queue. Do this before each "
               "demo so duplicate detection starts from a known state.")
    if st.button("Reset demo data", type="primary"):
        store.reset()
        for k in ("just_ran", "last_run_id"):
            st.session_state.pop(k, None)
        st.success("Demo data restored to its starting state.")

    st.subheader("Decision rules in force")
    st.markdown(r"""
| Check | Outcome |
|---|---|
| Vendor not in the vendor master, or blocked | **REJECTED - VENDOR NOT APPROVED** |
| Same vendor + same invoice number as an earlier invoice | **REJECTED - DUPLICATE INVOICE** |
| Same vendor + same date + same total, different number | **REJECTED - SUSPECTED DUPLICATE** |
| No PO on invoice, exactly one open PO for that vendor and amount | **APPROVED - AUTO-MATCHED PO** |
| No PO on invoice, zero or several candidates | **FLAGGED** for a person to choose |
| Total equals open PO balance | **APPROVED** |
| Total below open PO balance | **APPROVED - PARTIAL DRAWDOWN** (balance reduced) |
| Total above balance but inside tolerance | **APPROVED - WITHIN TOLERANCE** |
| Total above balance and outside tolerance | **FLAGGED - OVER TOLERANCE / EXCEEDS PO BALANCE** |
| Missing fields, arithmetic that does not add up, unreadable file | **FLAGGED** |
""")


# -------------------------------------------------------------------- shell ---
store.ensure_runtime()
pages = [
    st.Page(page_process, title="Process invoice", icon=":material/receipt_long:", default=True),
    st.Page(page_dashboard, title="Dashboard", icon=":material/monitoring:", url_path="dashboard"),
    st.Page(page_review, title="Review queue", icon=":material/rate_review:", url_path="review"),
    st.Page(page_data, title="Procurement data", icon=":material/database:", url_path="data"),
    st.Page(page_settings, title="Rules & settings", icon=":material/tune:", url_path="settings"),
]
nav = st.navigation(pages)
with st.sidebar:
    st.markdown("### 🧾 Invoice Autopilot")
    st.caption("Accounts-payable invoice processing: PDF in, reasoned decision out.")
    counter = st.empty()
nav.run()
_runs = store.runs()   # counted after the page ran, so a run that just finished is included
_open = sum(1 for r in _runs if r["decision"]["status"] == "FLAGGED" and not r["resolution"])
counter.caption(f"{len(_runs)} run(s) · {_open} waiting for review")
