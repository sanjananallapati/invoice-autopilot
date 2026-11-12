"""Run:  pytest -q     (uses the local parser, so no API key or network is needed)"""
import json
from pathlib import Path

import pytest

from core import pipeline, rules, store

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "sample_invoices"
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text())


@pytest.fixture(autouse=True)
def fresh_data(tmp_path):
    store.set_runtime_dir(tmp_path)
    store.reset()


def process(name):
    return pipeline.run_to_completion((SAMPLES / name).read_bytes(), name, engine="local")


def test_every_sample_reaches_its_expected_decision():
    for m in MANIFEST:  # in order: 5b depends on 5a having drawn the PO down
        got = process(m["file"])["decision"]["code"]
        assert got == m.get("expected_local", m["expected"]), m["file"]


def test_happy_path_posts_to_po_and_ledger():
    r = process("01_happy_path.pdf")
    assert r["decision"]["status"] == "APPROVED" and len(r["stages"]) == 8
    po = rules.find_po("PO-1001", store.purchase_orders())
    assert po["remaining"] == 0 and po["status"] == "closed"
    assert any(e["invoice_number"] == "INV-NW-8841" for e in store.ledger())


def test_same_invoice_twice_is_rejected_and_not_posted_twice():
    process("01_happy_path.pdf")
    again = process("01_happy_path.pdf")
    assert again["decision"]["code"] == "REJECTED - DUPLICATE INVOICE"
    assert len(rules.find_po("PO-1001", store.purchase_orders())["invoices"]) == 1


def test_ghost_po_is_inferred_only_when_unique():
    r = process("02_ghost_po_missing_po_number.pdf")
    assert r["auto_matched_po"] and r["po_number"] == "PO-1002"
    pos = store.purchase_orders()
    pos.append({**rules.find_po("PO-1003", pos), "po_number": "PO-1003-B"})  # two POs, same balance
    exact, _ = rules.infer_po("V-002", 2106.00, pos)
    assert len(exact) == 2  # pipeline flags this as AMBIGUOUS instead of guessing


def test_tolerance_uses_the_stricter_limit():
    s = {"tolerance_pct": 5.0, "tolerance_abs": 50.0}
    assert rules.tolerance_for(10000, s) == 50.0   # 5% would be 500: the cap wins
    assert rules.tolerance_for(600, s) == 30.0     # 5% of 600: the percentage wins


def test_split_billing_draws_down_then_blocks_overdraw():
    assert process("05a_split_billing_1_of_2.pdf")["posting"]["remaining_after"] == 6000.0
    po = rules.find_po("PO-1006", store.purchase_orders())
    over = rules.reconcile({"total": 7000.0, "line_items": []}, po, store.settings())
    assert over["outcome"] == "exceeds_balance"
    assert process("05b_split_billing_2_of_2.pdf")["posting"]["po_status"] == "closed"


def test_phantom_duplicate_needs_no_matching_invoice_number():
    r = process("04_phantom_duplicate.pdf")
    assert r["decision"]["code"] == "REJECTED - SUSPECTED DUPLICATE"
    assert "re-issue" in r["decision"]["reasons"][0]


def test_vendor_matching_tolerates_formatting_but_not_strangers():
    v = store.vendors()
    assert rules.match_vendor("NORTHWIND OFFICE SUPPLIES, INC", v)[0]["vendor_id"] == "V-001"
    assert rules.match_vendor("Halcyon Print and Media Ltd", v)[0]["vendor_id"] == "V-007"
    assert rules.match_vendor("Quickfix Hardware Traders", v)[0] is None


def test_reviewer_can_approve_a_flagged_invoice():
    r = process("07_over_tolerance.pdf")
    assert r["decision"]["status"] == "FLAGGED" and r["posting"] is None
    done = pipeline.resolve_review(r["run_id"], "approve", "Price rise agreed by buyer")
    assert pipeline.final_status(done) == "APPROVED (manual)"
    assert rules.find_po("PO-1008", store.purchase_orders())["status"] == "closed"


def test_gemini_failure_falls_back_to_local_parser(monkeypatch):
    from core import extractor
    def boom(*a, **k):
        raise extractor.ExtractionError("Gemini call failed - HTTP 429 quota")
    monkeypatch.setattr(extractor, "call_gemini", boom)
    out = extractor.extract((SAMPLES / "01_happy_path.pdf").read_bytes(), engine="auto", api_key="fake")
    assert out["engine"].startswith("Local") and out["fields"]["total"] == 2592.0 and out["warnings"]


def test_gemini_answer_is_normalised(monkeypatch):
    from core import extractor
    raw = {"vendor_name": " Northwind Office Supplies Inc. ", "invoice_number": "INV-NW-8841",
           "invoice_date": "03 Nov 2026", "po_number": "N/A", "currency": "usd", "total": "$2,592.00",
           "subtotal": 2400, "tax": "192.00", "tax_embedded": False,
           "line_items": [{"description": "Chair", "quantity": "8", "unit_price": "185.00", "amount": "1,480.00"}]}
    monkeypatch.setattr(extractor, "call_gemini", lambda *a, **k: (raw, "test-model"))
    f = extractor.extract((SAMPLES / "06_scanned_invoice.pdf").read_bytes(), "gemini", "fake")["fields"]
    assert f["invoice_date"] == "2026-11-03" and f["po_number"] is None and f["total"] == 2592.0
    assert f["line_items"][0]["amount"] == 1480.0
