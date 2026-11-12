"""The invoice pipeline: eight visible stages from PDF to decision.

`run()` is a generator. It yields an event when each stage starts and when it
finishes, which is what lets the UI show the run live. The final event carries
the complete, self-explaining run record that is stored for the dashboard.

    1 intake -> 2 extract -> 3 validate -> 4 vendor -> 5 duplicate
      -> 6 PO match -> 7 reconcile -> 8 decide & post
"""
from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime

from . import extractor, rules, store
from .rules import money

STAGES = [
    ("intake", "Document intake"),
    ("extract", "Data extraction"),
    ("validate", "Field and arithmetic checks"),
    ("vendor", "Vendor verification"),
    ("duplicate", "Duplicate detection"),
    ("po", "Purchase order matching"),
    ("reconcile", "Amount and line reconciliation"),
    ("decide", "Decision and posting"),
]

NEXT_ACTION = {
    "APPROVED": "Queued for payment on the vendor's terms. No human action needed.",
    "FLAGGED": "Sent to the AP review queue. A reviewer approves or rejects it with the evidence below.",
    "REJECTED": "Not posted. Return to the vendor / requester with the reason below.",
}


def _stage(key, status, summary, details=None, data=None):
    return {"key": key, "title": dict(STAGES)[key], "status": status,
            "summary": summary, "details": details or [], "data": data or {}}


def run(pdf_bytes: bytes, filename: str, engine: str = "auto", api_key: str | None = None):
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    settings, vendors, pos, ledger = store.settings(), store.vendors(), store.purchase_orders(), store.ledger()
    issues = []          # every problem found: {"severity": "reject"|"flag", "code", "reason"}
    stages = []
    halted = False       # a hard stop: later checks would be meaningless
    inv, vendor, po, rec, auto_matched, engine_used = {}, None, None, None, False, "—"

    clock = {"t0": 0.0, "busy": 0.0}   # time spent working, excluding UI pacing

    def tick():
        clock["t0"] = time.perf_counter()

    def emit(stage):
        clock["busy"] += time.perf_counter() - clock["t0"]
        stages.append(stage)
        return ("done", stage)

    def begin(key):
        return ("start", {"key": key, "title": dict(STAGES)[key], "n": [k for k, _ in STAGES].index(key) + 1})

    def issue(severity, code, reason):
        issues.append({"severity": severity, "code": code, "reason": reason})

    # 1 ── intake ------------------------------------------------------------
    yield begin("intake")
    tick()
    digest = hashlib.sha256(pdf_bytes).hexdigest()[:12]
    try:
        text, pages = extractor.read_text_layer(pdf_bytes)
        kind = "digital PDF with a text layer" if len(text) >= 20 else "scanned image, no text layer"
        yield emit(_stage("intake", "pass", f"{filename} · {pages} page(s) · {kind}",
                          [f"File fingerprint {digest} · {len(pdf_bytes) / 1024:.0f} KB"],
                          {"pages": pages, "text_layer": len(text) >= 20, "sha": digest}))
    except extractor.ExtractionError as exc:
        issue("flag", "FLAGGED - UNREADABLE DOCUMENT", str(exc))
        halted = True
        yield emit(_stage("intake", "fail", str(exc)))

    # 2 ── extract -----------------------------------------------------------
    yield begin("extract")
    tick()
    if halted:
        yield emit(_stage("extract", "skip", "Skipped - the file could not be opened."))
    else:
        try:
            ex = extractor.extract(pdf_bytes, engine=engine, api_key=api_key)
            inv, engine_used = ex["fields"], ex["engine"]
            got = [f for f in ("vendor_name", "invoice_number", "invoice_date", "po_number", "total") if inv.get(f)]
            details = [f"Vendor: {inv['vendor_name'] or '—'} · Invoice: {inv['invoice_number'] or '—'} · "
                       f"Date: {inv['invoice_date'] or '—'} · PO: {inv['po_number'] or '—'}",
                       f"{len(inv['line_items'])} line item(s) · Subtotal {money(inv['subtotal'])} · "
                       f"Tax {'embedded in total' if inv['tax_embedded'] else money(inv['tax'])} · "
                       f"Total {money(inv['total'])}"] + [f"Note: {w}" for w in ex["warnings"]]
            yield emit(_stage("extract", "warn" if ex["warnings"] else "pass",
                              f"{len(got)}/5 key fields read by {engine_used}", details, {"fields": inv}))
        except extractor.ExtractionError as exc:
            issue("flag", "FLAGGED - UNREADABLE DOCUMENT", str(exc))
            halted = True
            yield emit(_stage("extract", "fail", "Could not read the invoice automatically.", [str(exc)]))

    # 3 ── validate ----------------------------------------------------------
    yield begin("validate")
    tick()
    if halted:
        yield emit(_stage("validate", "skip", "Skipped - nothing was extracted."))
    else:
        details, status = [], "pass"
        critical = [f for f in ("vendor_name", "total") if inv.get(f) in (None, "")]
        soft = [f for f in ("invoice_number", "invoice_date") if not inv.get(f)]
        label = {"vendor_name": "vendor name", "total": "total amount",
                 "invoice_number": "invoice number", "invoice_date": "invoice date"}
        if critical or soft:
            missing = ", ".join(label[f] for f in critical + soft)
            issue("flag", "FLAGGED - INCOMPLETE INVOICE", f"Missing on the document: {missing}.")
            details.append(f"Missing: {missing}")
            status = "fail" if critical else "warn"
            halted = halted or bool(critical)
        else:
            details.append("All required fields present (vendor, invoice number, date, total).")
        if not inv.get("po_number"):
            details.append("No PO reference on the invoice - will try to infer it at PO matching.")
        # arithmetic: do the lines, tax and total agree with each other?
        if inv.get("total") is not None and inv["line_items"]:
            lines_sum = round(sum(li["amount"] for li in inv["line_items"]), 2)
            tax = inv["tax"] or 0.0
            candidates = {round(lines_sum + tax, 2), round((inv["subtotal"] or lines_sum) + tax, 2)}
            if inv["tax_embedded"] or inv["tax"] is None:
                candidates |= {lines_sum}
            if any(abs(inv["total"] - c) <= 0.02 for c in candidates):
                details.append(f"Arithmetic holds: lines {money(lines_sum)}"
                               + (f" + tax {money(tax)}" if tax else " (tax embedded)" if inv["tax_embedded"] else "")
                               + f" = total {money(inv['total'])}.")
            else:
                issue("flag", "FLAGGED - MATH MISMATCH",
                      f"Line items ({money(lines_sum)}) plus tax ({money(tax)}) do not add up to the stated "
                      f"total ({money(inv['total'])}).")
                details.append(f"Lines {money(lines_sum)} + tax {money(tax)} ≠ total {money(inv['total'])}")
                status = "warn" if status == "pass" else status
        summary = {"pass": "Invoice is complete and internally consistent.",
                   "warn": "Problems found - continuing so the reviewer sees the full picture.",
                   "fail": "Critical fields are missing - cannot continue."}[status]
        yield emit(_stage("validate", status, summary, details))

    # 4 ── vendor ------------------------------------------------------------
    yield begin("vendor")
    tick()
    if halted:
        yield emit(_stage("vendor", "skip", "Skipped."))
    else:
        vendor, score, how = rules.match_vendor(inv["vendor_name"], vendors, settings["vendor_match_threshold"])
        if vendor is None:
            issue("reject", "REJECTED - VENDOR NOT APPROVED",
                  f"'{inv['vendor_name']}' is not in the approved vendor master (closest similarity {score:.0%}). "
                  "Vendors must be onboarded before they can be paid.")
            halted = True
            yield emit(_stage("vendor", "fail", f"'{inv['vendor_name']}' is not an approved vendor.",
                              [f"Closest name in the vendor master is only {score:.0%} similar."]))
        elif vendor["status"] != "approved":
            why = vendor.get("status_reason", "status is " + vendor["status"])
            issue("reject", "REJECTED - VENDOR NOT APPROVED", f"{vendor['name']} is {vendor['status']}: {why}.")
            halted = True
            yield emit(_stage("vendor", "fail", f"{vendor['name']} is {vendor['status']}.", [why]))
        else:
            yield emit(_stage("vendor", "pass", f"{vendor['name']} ({vendor['vendor_id']}) is approved.",
                              [f"{how.capitalize()} ({score:.0%}) · terms {vendor['payment_terms']}"],
                              {"vendor_id": vendor["vendor_id"]}))

    # 5 ── duplicate ---------------------------------------------------------
    yield begin("duplicate")
    tick()
    if halted:
        yield emit(_stage("duplicate", "skip", "Skipped."))
    else:
        dup = rules.find_duplicate(inv, vendor["vendor_id"], ledger)
        if dup is None:
            yield emit(_stage("duplicate", "pass", "No earlier invoice matches this one.",
                              [f"Checked {len(ledger)} ledger entries on invoice number, and on vendor + date + amount."]))
        else:
            m = dup["match"]
            ref = (f"{m['invoice_number']} ({money(m['total'])}, dated {m['invoice_date']}, "
                   f"{m.get('payment_status') or m['status']})")
            if dup["tier"] == "exact":
                code, sev = "REJECTED - DUPLICATE INVOICE", "reject"
                reason = f"Invoice number {inv['invoice_number']} was already processed for this vendor: {ref}."
            elif dup["tier"] == "content":
                code, sev = "REJECTED - SUSPECTED DUPLICATE", "reject"
                reason = (f"Different invoice number, but same vendor, same date and same total as {ref}."
                          + (f" '{inv['invoice_number']}' looks like a re-issue of '{m['invoice_number']}'."
                             if dup.get("reissue") else ""))
            else:
                code, sev = "FLAGGED - POSSIBLE DUPLICATE", "flag"
                reason = (f"Same vendor and total as {ref}, and '{inv['invoice_number']}' looks like a re-issue "
                          f"of '{m['invoice_number']}', but the dates differ.")
            issue(sev, code, reason)
            halted = sev == "reject"
            yield emit(_stage("duplicate", "fail" if halted else "warn", reason,
                              ["Paying this would pay the vendor twice." if halted else "Needs a human look."],
                              {"match": m, "tier": dup["tier"]}))

    # 6 ── PO match ----------------------------------------------------------
    yield begin("po")
    tick()
    if halted:
        yield emit(_stage("po", "skip", "Skipped."))
    elif inv.get("po_number"):
        po = rules.find_po(inv["po_number"], pos)
        if po is None:
            issue("flag", "FLAGGED - PO NOT FOUND", f"PO reference '{inv['po_number']}' does not exist in procurement.")
            yield emit(_stage("po", "fail", f"PO '{inv['po_number']}' not found in the procurement system."))
        elif po["vendor_id"] != vendor["vendor_id"]:
            issue("flag", "FLAGGED - PO VENDOR MISMATCH",
                  f"{po['po_number']} was issued to a different vendor, not {vendor['name']}.")
            yield emit(_stage("po", "fail", f"{po['po_number']} belongs to another vendor."))
            po = None
        elif po["currency"] != inv["currency"]:
            issue("flag", "FLAGGED - CURRENCY MISMATCH",
                  f"Invoice is in {inv['currency']} but {po['po_number']} is in {po['currency']}.")
            yield emit(_stage("po", "fail", f"Currency differs from {po['po_number']}."))
            po = None
        elif po["remaining"] <= rules.CENT:
            issue("flag", "FLAGGED - PO FULLY INVOICED",
                  f"{po['po_number']} ({money(po['total'])}) has no balance left; it was already invoiced in full.")
            yield emit(_stage("po", "fail", f"{po['po_number']} has no remaining balance."))
            po = None
        else:
            yield emit(_stage("po", "pass", f"Matched {po['po_number']} from the reference on the invoice.",
                              [f"PO value {money(po['total'])} · open balance {money(po['remaining'])} · "
                               f"{len(po['invoices'])} earlier invoice(s)"], {"po_number": po["po_number"]}))
    else:
        exact, open_pos = rules.infer_po(vendor["vendor_id"], inv["total"], pos)
        if len(exact) == 1:
            po, auto_matched = exact[0], True
            yield emit(_stage("po", "warn", f"No PO on the invoice - inferred {po['po_number']}.",
                              [f"{vendor['name']} has {len(open_pos)} open PO(s); exactly one has an open balance of "
                               f"{money(inv['total'])}.",
                               "Inference only fires on a unique, to-the-cent match."],
                              {"po_number": po["po_number"], "auto_matched": True}))
        elif len(exact) > 1:
            names = ", ".join(p["po_number"] for p in exact)
            issue("flag", "FLAGGED - AMBIGUOUS PO MATCH",
                  f"No PO on the invoice and {len(exact)} open POs fit the amount ({names}). A person must choose.")
            yield emit(_stage("po", "fail", f"No PO on the invoice; {len(exact)} candidates fit ({names})."))
        else:
            listing = ", ".join(f"{p['po_number']} ({money(p['remaining'])} open)" for p in open_pos) or "none"
            issue("flag", "FLAGGED - NO MATCHING PO",
                  f"No PO on the invoice and no open PO for {vendor['name']} has a balance of {money(inv['total'])}. "
                  f"Open POs: {listing}.")
            yield emit(_stage("po", "fail", "No PO on the invoice and none could be inferred.",
                              [f"Open POs for this vendor: {listing}"]))

    # 7 ── reconcile ---------------------------------------------------------
    yield begin("reconcile")
    tick()
    if halted or po is None:
        yield emit(_stage("reconcile", "skip", "Skipped - no purchase order to compare against."))
    else:
        rec = rules.reconcile(inv, po, settings)
        details = [f"Invoice {money(inv['total'])} vs open PO balance {money(rec['remaining_before'])} "
                   f"→ variance {money(rec['variance'])} · tolerance for this PO {money(rec['tolerance'])} "
                   f"(lower of {settings['tolerance_pct']:g}% and {money(settings['tolerance_abs'])})"]
        details += [f"Not on the PO: {u['description']} {money(u['amount'])}"
                    + (" (freight-type charge)" if u["freight"] else "") for u in rec["unplanned"]]
        details += [f["text"] for f in rec["findings"]]
        o = rec["outcome"]
        if o in ("exact", "final"):
            summary, status = "Invoice total equals the open PO balance.", "pass"
        elif o == "partial":
            summary, status = (f"Partial invoice: draws {money(inv['total'])} from the PO, "
                               f"{money(rec['remaining_after'])} stays open."), "pass"
        elif o == "within_tolerance":
            summary, status = (f"Over the PO by {money(rec['variance'])} ({rec['variance_pct']}%) - "
                               "inside tolerance."), "warn"
        else:
            cause = "; ".join([f["text"] for f in rec["findings"]]
                              + [f"{u['description']} {money(u['amount'])} is not on the PO" for u in rec["unplanned"]])
            code = "FLAGGED - EXCEEDS PO BALANCE" if o == "exceeds_balance" else "FLAGGED - OVER TOLERANCE"
            issue("flag", code,
                  f"Invoice is {money(rec['variance'])} ({rec['variance_pct']}% of PO value) over the open balance of "
                  f"{po['po_number']}; the limit is {money(rec['tolerance'])}." + (f" Cause: {cause}." if cause else ""))
            summary, status = f"Over the PO by {money(rec['variance'])} - outside tolerance.", "fail"
        yield emit(_stage("reconcile", status, summary, details, rec))

    # 8 ── decide & post -----------------------------------------------------
    yield begin("decide")
    tick()
    rejects = [i for i in issues if i["severity"] == "reject"]
    flags = [i for i in issues if i["severity"] == "flag"]
    posting = None
    if rejects:
        status, code, reasons = "REJECTED", rejects[0]["code"], [i["reason"] for i in rejects + flags]
    elif flags:
        status, code, reasons = "FLAGGED", flags[0]["code"], [i["reason"] for i in flags]
    else:
        status, o = "APPROVED", rec["outcome"]
        reasons = [f"{vendor['name']} is an approved vendor.", "Not a duplicate of any earlier invoice."]
        if auto_matched:
            code = "APPROVED - AUTO-MATCHED PO"
            reasons.append(f"The invoice had no PO number; {po['po_number']} was attached because it is this vendor's "
                           f"only open PO for exactly {money(inv['total'])}.")
        elif o == "within_tolerance":
            code = "APPROVED - WITHIN TOLERANCE"
            extra = ", ".join(f"{u['description']} {money(u['amount'])}" for u in rec["unplanned"])
            reasons.append(f"Total is {money(rec['variance'])} ({rec['variance_pct']}%) over {po['po_number']}, within "
                           f"the {money(rec['tolerance'])} tolerance." + (f" Variance comes from: {extra}." if extra else ""))
        elif o == "partial":
            code = "APPROVED - PARTIAL DRAWDOWN"
            reasons.append(f"Invoice covers {money(inv['total'])} of {po['po_number']} ({money(po['total'])}). "
                           f"{money(rec['remaining_after'])} remains open for later invoices.")
        elif o == "final":
            code = "APPROVED - FINAL DRAWDOWN"
            reasons.append(f"This invoice uses the last {money(inv['total'])} of {po['po_number']}; "
                           "the PO is now fully invoiced and closed.")
        else:
            code = "APPROVED"
            reasons.append(f"Total matches {po['po_number']} exactly ({money(inv['total'])}).")
        updated = store.post_to_po(po["po_number"], inv["invoice_number"], rec["post_amount"],
                                   inv["invoice_date"], run_id)
        posting = {"po_number": po["po_number"], "posted": rec["post_amount"],
                   "remaining_before": rec["remaining_before"], "remaining_after": updated["remaining"],
                   "po_status": updated["status"]}

    if status != "REJECTED" and vendor and inv.get("total") is not None:
        store.add_ledger_entry({
            "invoice_number": inv.get("invoice_number"), "vendor_id": vendor["vendor_id"], "vendor_name": vendor["name"],
            "invoice_date": inv.get("invoice_date"), "total": inv["total"], "po_number": po["po_number"] if po else None,
            "status": "approved" if status == "APPROVED" else "in_review",
            "payment_status": "awaiting payment" if status == "APPROVED" else "on hold - in review",
            "run_id": run_id, "processed_at": datetime.now().isoformat(timespec="seconds")})

    details = list(reasons)
    if posting:
        details.append(f"Posted {money(posting['posted'])} to {posting['po_number']}: open balance "
                       f"{money(posting['remaining_before'])} → {money(posting['remaining_after'])} "
                       f"(PO now {posting['po_status'].replace('_', ' ')}).")
    yield emit(_stage("decide", {"APPROVED": "pass", "FLAGGED": "warn", "REJECTED": "fail"}[status], code, details))

    record = {
        "run_id": run_id, "timestamp": datetime.now().isoformat(timespec="seconds"), "filename": filename,
        "file_sha": digest, "engine": engine_used, "duration_ms": int(clock["busy"] * 1000),
        "extracted": inv, "vendor_id": vendor["vendor_id"] if vendor else None,
        "vendor_name": vendor["name"] if vendor else inv.get("vendor_name"),
        "po_number": po["po_number"] if po else inv.get("po_number"), "auto_matched_po": auto_matched,
        "total": inv.get("total"), "stages": stages,
        "decision": {"status": status, "code": code, "reasons": reasons, "next_action": NEXT_ACTION[status]},
        "posting": posting, "resolution": None,
    }
    store.add_run(record)
    yield ("result", record)


def run_to_completion(pdf_bytes, filename, engine="auto", api_key=None):
    """Convenience for scripts and tests: run without a UI, return the record."""
    record = None
    for kind, payload in run(pdf_bytes, filename, engine, api_key):
        if kind == "result":
            record = payload
    return record


def resolve_review(run_id: str, action: str, note: str, reviewer: str = "AP reviewer"):
    """Human-in-the-loop: a reviewer closes out a flagged invoice."""
    record = next(r for r in store.runs() if r["run_id"] == run_id)
    if record["decision"]["status"] != "FLAGGED" or record["resolution"]:
        raise ValueError("Only an open flagged run can be resolved.")
    posting = None
    if action == "approve":
        po = rules.find_po(record["po_number"], store.purchase_orders()) if record["po_number"] else None
        if po and record["total"] is not None:
            amount = min(record["total"], po["remaining"])
            updated = store.post_to_po(po["po_number"], record["extracted"].get("invoice_number"), amount,
                                       record["extracted"].get("invoice_date"), run_id)
            posting = {"po_number": po["po_number"], "posted": amount, "remaining_before": po["remaining"],
                       "remaining_after": updated["remaining"], "po_status": updated["status"],
                       "over_po": round(record["total"] - amount, 2)}
        store.set_ledger_status(run_id, "approved")
    else:
        store.set_ledger_status(run_id, "rejected")
    record["resolution"] = {"action": action, "note": note, "by": reviewer,
                            "at": datetime.now().isoformat(timespec="seconds"), "posting": posting}
    store.update_run(record)
    return record


def final_status(record) -> str:
    """Status after any human review - what the dashboard reports."""
    res = record.get("resolution")
    if res:
        return "APPROVED (manual)" if res["action"] == "approve" else "REJECTED (manual)"
    return record["decision"]["status"]
