"""Procurement business rules. Pure functions: data in, finding out.

Nothing here reads files or calls an API, which is what makes every rule
unit-testable and every decision reproducible.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

CENT = 0.011  # two amounts are "equal" if they differ by a cent or less
LEGAL_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "pvt", "limited", "plc", "gmbh"}
FREIGHT_WORDS = ("freight", "shipping", "delivery", "handling", "carriage", "courier", "postage", "fuel surcharge")


def money(x, cur="USD") -> str:
    if x is None:
        return "—"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(cur, f"{cur} ")
    return f"-{sym}{abs(x):,.2f}" if x < 0 else f"{sym}{x:,.2f}"


# ------------------------------------------------------------------ vendor ---
def norm_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and "))
    return " ".join(t for t in s.split() if t not in LEGAL_SUFFIXES)


def match_vendor(name, vendors, threshold=0.86):
    """Returns (vendor | None, similarity, how). Tolerates 'Inc.' vs 'Inc',
    '&' vs 'and', aliases and small spelling differences."""
    target = norm_name(name)
    if not target:
        return None, 0.0, "no vendor name"
    best, best_score = None, 0.0
    for v in vendors:
        for cand in [v["name"], *v.get("aliases", [])]:
            c = norm_name(cand)
            if c == target:
                return v, 1.0, "exact name match"
            score = SequenceMatcher(None, target, c).ratio()
            if score > best_score:
                best, best_score = v, score
    if best and best_score >= threshold:
        return best, round(best_score, 2), "fuzzy name match"
    return None, round(best_score, 2), "no match"


# -------------------------------------------------------------- duplicates ---
def norm_id(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def looks_like_reissue(a, b) -> bool:
    """INV-102 vs INV-102-A / INV102R / INV-102 (2): one is the other plus a suffix."""
    a, b = norm_id(a), norm_id(b)
    if not a or not b or a == b:
        return False
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short) and len(long_) - len(short) <= 3


def find_duplicate(inv, vendor_id, ledger):
    """Three tiers, strongest first. Rejected invoices are ignored so a
    corrected re-submission is not blocked.

    exact      same vendor + same invoice number                  -> reject
    content    same vendor + same date + same total, new number   -> reject
    possible   same vendor + same total + number looks re-issued  -> human review
    """
    live = [e for e in ledger if e.get("vendor_id") == vendor_id and e.get("status") != "rejected"]
    num, date, total = inv.get("invoice_number"), inv.get("invoice_date"), inv.get("total")
    for e in live:
        if num and norm_id(e.get("invoice_number")) == norm_id(num):
            return {"tier": "exact", "match": e}
    for e in live:
        if date and total is not None and e.get("invoice_date") == date and abs(e["total"] - total) < CENT:
            return {"tier": "content", "match": e, "reissue": looks_like_reissue(num, e.get("invoice_number"))}
    for e in live:
        if total is not None and abs(e["total"] - total) < CENT and looks_like_reissue(num, e.get("invoice_number")):
            return {"tier": "possible", "match": e, "reissue": True}
    return None


# ---------------------------------------------------------------- PO match ---
def find_po(po_number, pos):
    key = norm_id(po_number)
    return next((p for p in pos if norm_id(p["po_number"]) == key), None)


def infer_po(vendor_id, total, pos):
    """Invoice arrived with no PO reference. Candidates = this vendor's open
    POs whose remaining balance equals the invoice total to the cent.
    Inference is deliberately strict: no tolerance is applied to a guess."""
    open_pos = [p for p in pos if p["vendor_id"] == vendor_id and p["remaining"] > CENT]
    exact = [p for p in open_pos if total is not None and abs(p["remaining"] - total) < CENT]
    return exact, open_pos


# ------------------------------------------------------------ reconcile ---
def compare_lines(inv_lines, po_lines):
    """Explain *where* a difference comes from: price changes, quantity
    overruns, and charges that were never on the PO (freight etc.)."""
    findings, unplanned = [], []
    for li in inv_lines:
        d = norm_name(li["description"])
        best, score = None, 0.0
        for pl in po_lines:
            s = SequenceMatcher(None, d, norm_name(pl["description"])).ratio()
            if s > score:
                best, score = pl, s
        if best and score >= 0.6:
            if li.get("unit_price") is not None and abs(li["unit_price"] - best["unit_price"]) > 0.005:
                findings.append({"kind": "price", "text":
                                 f"'{li['description']}': invoiced at {money(li['unit_price'])} per unit, "
                                 f"PO price is {money(best['unit_price'])}"})
            if li.get("quantity") is not None and li["quantity"] > best["quantity"] + 1e-9:
                findings.append({"kind": "quantity", "text":
                                 f"'{li['description']}': {li['quantity']:g} invoiced, only {best['quantity']:g} ordered"})
        else:
            is_freight = any(w in li["description"].lower() for w in FREIGHT_WORDS)
            unplanned.append({"description": li["description"], "amount": li["amount"], "freight": is_freight})
    return findings, unplanned


def tolerance_for(po_total, settings):
    """An overage must pass BOTH limits: the percentage and the hard cap."""
    return round(min(po_total * settings["tolerance_pct"] / 100.0, settings["tolerance_abs"]), 2)


def reconcile(inv, po, settings):
    """Compare the invoice total with what is still open on the PO."""
    total, remaining = inv["total"], po["remaining"]
    variance = round(total - remaining, 2)
    tol = tolerance_for(po["total"], settings)
    prior = len(po["invoices"])
    findings, unplanned = compare_lines(inv["line_items"], po["lines"])
    out = {"variance": variance, "tolerance": tol, "remaining_before": remaining,
           "findings": findings, "unplanned": unplanned, "prior_invoices": prior}
    if abs(variance) < CENT:
        out.update(outcome="final" if prior else "exact", remaining_after=0.0, post_amount=remaining)
    elif variance < 0:
        out.update(outcome="partial", remaining_after=round(remaining - total, 2), post_amount=total)
    elif variance <= tol + 1e-9:
        out.update(outcome="within_tolerance", remaining_after=0.0, post_amount=remaining,
                   variance_pct=round(variance / po["total"] * 100, 2))
    else:
        out.update(outcome="exceeds_balance" if prior else "over_tolerance", remaining_after=remaining,
                   post_amount=0.0, variance_pct=round(variance / po["total"] * 100, 2))
    return out
