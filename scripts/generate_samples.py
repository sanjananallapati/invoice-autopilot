"""Generate the demo invoice PDFs in sample_invoices/.

Run:  python scripts/generate_samples.py

Three deliberately different layouts are used (different vendors format
invoices differently), plus one image-only "scan" with no text layer.
"""
from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "sample_invoices"
BUYER = ["Cedar Ridge Manufacturing Co.", "Accounts Payable", "1200 Foundry Road", "Columbus, OH 43215"]
W, H = LETTER


def usd(x):
    return f"${x:,.2f}"


def qty(q):
    return f"{q:,}" if isinstance(q, int) else f"{q:g}"


def totals(inv):
    sub = round(sum(q * p for _, q, p in inv["lines"]), 2)
    return sub, inv["tax"], round(sub + inv["tax"], 2)


# ------------------------------------------------------------- layout A ---
def layout_classic(inv, path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica-Bold", 17); c.drawString(50, H - 60, inv["vendor"])
    c.setFont("Helvetica", 9); y = H - 76
    for ln in inv["address"]:
        c.drawString(50, y, ln); y -= 12
    c.setFont("Helvetica-Bold", 22); c.setFillColor(colors.HexColor("#1f3a5f"))
    c.drawRightString(W - 50, H - 110, "INVOICE"); c.setFillColor(colors.black)
    y = H - 150; c.setFont("Helvetica", 10)
    meta = [("Invoice No:", inv["number"]), ("Invoice Date:", inv["date"])]
    if inv.get("po"):
        meta.append(("PO Number:", inv["po"]))
    meta.append(("Payment Terms:", inv.get("terms", "Net 30")))
    for k, v in meta:
        c.setFont("Helvetica-Bold", 10); c.drawString(50, y, k)
        c.setFont("Helvetica", 10); c.drawString(140, y, v); y -= 15
    y -= 10; c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Bill To: " + BUYER[0]); y -= 13
    c.setFont("Helvetica", 9)
    for ln in BUYER[1:]:
        c.drawString(50, y, ln); y -= 12
    y -= 18; c.setFillColor(colors.HexColor("#1f3a5f")); c.rect(45, y - 5, W - 90, 18, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description"); c.drawRightString(370, y, "Qty")
    c.drawRightString(460, y, "Unit Price"); c.drawRightString(W - 50, y, "Amount")
    c.setFillColor(colors.black); c.setFont("Helvetica", 10); y -= 22
    for d, q, p in inv["lines"]:
        c.drawString(50, y, d); c.drawRightString(370, y, qty(q))
        c.drawRightString(460, y, usd(p)); c.drawRightString(W - 50, y, usd(q * p)); y -= 17
    sub, tax, total = totals(inv)
    y -= 8; c.line(350, y + 10, W - 50, y + 10)
    for k, v, bold in [("Subtotal", sub, False), (inv.get("tax_label", "Sales Tax (8%)"), tax, False),
                       ("Total Due", total, True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawString(350, y, k); c.drawRightString(W - 50, y, usd(v)); y -= 17
    c.setFont("Helvetica", 8); c.setFillColor(colors.grey)
    c.drawString(50, 60, inv.get("footer", "Thank you for your business. Remit by ACH to the account on file."))
    c.save()


# ------------------------------------------------------------- layout B ---
def layout_modern(inv, path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFillColor(colors.HexColor("#0b6e4f")); c.rect(0, H - 46, W, 46, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 16); c.drawCentredString(W / 2, H - 30, "TAX INVOICE")
    c.setFillColor(colors.black); y = H - 80
    c.setFont("Helvetica-Bold", 11); c.drawString(50, y, "From: " + inv["vendor"])
    c.setFont("Helvetica", 9); yy = y - 14
    for ln in inv["address"]:
        c.drawString(50, yy, ln); yy -= 12
    c.setFont("Helvetica", 10); ry = y - 34
    right = [("Invoice #", inv["number"]), ("Date of issue", inv["date"])]
    if inv.get("po"):
        right.append(("Purchase Order", inv["po"]))
    else:
        right.append(("Customer ref", "Dock 4 receiving"))
    for k, v in right:
        c.drawRightString(W - 50, ry, f"{k}: {v}"); ry -= 14
    y = yy - 26
    c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Billed to"); y -= 13; c.setFont("Helvetica", 9)
    for ln in BUYER:
        c.drawString(50, y, ln); y -= 12
    y -= 18; c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description"); c.drawRightString(370, y, "Qty")
    c.drawRightString(460, y, "Rate"); c.drawRightString(W - 50, y, "Amount")
    c.line(50, y - 5, W - 50, y - 5); c.setFont("Helvetica", 10); y -= 22
    for d, q, p in inv["lines"]:
        c.drawString(50, y, d); c.drawRightString(370, y, qty(q))
        c.drawRightString(460, y, usd(p)); c.drawRightString(W - 50, y, usd(q * p)); y -= 17
    sub, tax, total = totals(inv); y -= 14
    for k, v in [("Sub-total", sub), (inv.get("tax_label", "Tax 8%"), tax)]:
        c.drawString(360, y, k); c.drawRightString(W - 50, y, usd(v)); y -= 16
    c.setFillColor(colors.HexColor("#e7f3ee")); c.rect(350, y - 7, W - 400, 22, fill=1, stroke=0)
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 12)
    c.drawString(360, y, "AMOUNT DUE"); c.drawRightString(W - 50, y, usd(total))
    c.setFont("Helvetica", 8); c.setFillColor(colors.grey)
    c.drawString(50, 60, inv.get("footer", "Payment within terms. Queries: billing desk, Mon-Fri."))
    c.save()


# ------------------------------------------------------------- layout C ---
def layout_letter(inv, path):
    """Bundled single line, tax embedded in the total, PO only implied in a sentence."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Times-Bold", 16); c.drawString(60, H - 70, inv["vendor"])
    c.setFont("Times-Roman", 10); y = H - 86
    for ln in inv["address"]:
        c.drawString(60, y, ln); y -= 12
    y -= 20; c.setFont("Times-Bold", 12)
    c.drawString(60, y, f"INVOICE # {inv['number']}"); c.drawRightString(W - 60, y, f"Dated: {inv['date']}")
    y -= 26; c.setFont("Times-Roman", 11)
    c.drawString(60, y, "To: " + BUYER[0]); y -= 22
    c.drawString(60, y, f"Re: Supply of consumables as per your order {inv['implied_po']}."); y -= 30
    c.setFont("Times-Bold", 11); c.drawString(60, y, "Particulars"); c.drawRightString(W - 60, y, "Amount (USD)")
    c.line(60, y - 5, W - 60, y - 5); y -= 22; c.setFont("Times-Roman", 11)
    d, q, p = inv["lines"][0]
    c.drawString(60, y, d); c.drawRightString(W - 60, y, usd(q * p)); y -= 30
    c.setFont("Times-Bold", 11)
    c.drawString(60, y, "Total payable (inclusive of 8% sales tax)"); c.drawRightString(W - 60, y, usd(q * p))
    y -= 40; c.setFont("Times-Italic", 10)
    c.drawString(60, y, "Prices are quoted tax-inclusive. Please pay within 30 days.")
    c.save()


def make_scan(src: Path, dst: Path):
    """Rasterise a PDF into a slightly skewed, noisy greyscale image-only PDF."""
    page = pdfium.PdfDocument(str(src))[0]
    img = page.render(scale=1.7).to_pil().convert("L")
    img = img.rotate(0.7, expand=False, fillcolor=255, resample=Image.BICUBIC)
    rnd = random.Random(7); px = img.load()
    for _ in range(int(img.width * img.height * 0.002)):
        px[rnd.randrange(img.width), rnd.randrange(img.height)] = rnd.randrange(120, 220)
    img = img.filter(ImageFilter.GaussianBlur(0.45))
    buf = io.BytesIO(); img.convert("RGB").save(buf, "PDF", resolution=130, quality=72)
    dst.write_bytes(buf.getvalue())


NW = ["48 Harbor Street", "Portland, OR 97201", "Tel 503-555-0142"]
INVOICES = [
    dict(file="01_happy_path.pdf", layout="A", label="1 · Happy path - clean invoice, exact PO match",
         expected="APPROVED", vendor="Northwind Office Supplies Inc.", address=NW,
         number="INV-NW-8841", date="03 Nov 2026", po="PO-1001", tax=192.00,
         lines=[("Ergonomic office chair", 8, 185.00), ("Standing desk converter", 4, 230.00)]),
    dict(file="02_ghost_po_missing_po_number.pdf", layout="B", label="2 · Ghost PO - no PO number on the invoice",
         expected="APPROVED - AUTO-MATCHED PO", vendor="Summit Logistics LLC",
         address=["901 Freight Avenue", "Reno, NV 89502"], number="SL-20931", date="November 5, 2026", tax=280.00,
         lines=[("Regional pallet shipment", 14, 250.00)]),
    dict(file="03_freight_within_tolerance.pdf", layout="A", label="3 · Sneaky freight - $25 not on the PO",
         expected="APPROVED - WITHIN TOLERANCE", vendor="Brightline Packaging Co.",
         address=["77 Mill Lane", "Dayton, OH 45402"], number="BL-5567", date="2026-11-04", po="PO-1004",
         tax=96.00, tax_label="Sales Tax (8% on goods)",
         lines=[("Corrugated shipping boxes (500 pack)", 20, 42.00), ("Packing tape case", 12, 30.00),
                ("Freight & handling", 1, 25.00)]),
    dict(file="04_phantom_duplicate.pdf", layout="A", label="4 · Phantom duplicate - INV-102 re-sent as INV-102-A",
         expected="REJECTED - SUSPECTED DUPLICATE", vendor="Kestrel Industrial Parts Ltd.",
         address=["15 Foundry Park", "Cleveland, OH 44114"], number="INV-102-A", date="28 Oct 2026", po="PO-1005",
         tax=388.00, lines=[("Hydraulic seal kit", 10, 320.00), ("Bearing assembly", 6, 275.00)],
         footer="RE-ISSUED COPY - original not yet settled. Please process at your earliest convenience."),
    dict(file="05a_split_billing_1_of_2.pdf", layout="B", label="5a · Split billing - $4,000 of a $10,000 PO",
         expected="APPROVED - PARTIAL DRAWDOWN", vendor="Atlas Consulting Group",
         address=["300 Market Square, Suite 12", "Chicago, IL 60601"], number="AC-3301", date="November 2, 2026",
         po="PO-1006", tax=0.00, tax_label="Tax (exempt services)",
         lines=[("Process automation consulting (hours)", 40, 100.00)]),
    dict(file="05b_split_billing_2_of_2.pdf", layout="B", label="5b · Split billing - final $6,000 (run 5a first)",
         expected="APPROVED - FINAL DRAWDOWN", vendor="Atlas Consulting Group",
         address=["300 Market Square, Suite 12", "Chicago, IL 60601"], number="AC-3302", date="November 9, 2026",
         po="PO-1006", tax=0.00, tax_label="Tax (exempt services)",
         lines=[("Process automation consulting (hours)", 60, 100.00)]),
    dict(file="06_scanned_invoice.pdf", layout="A", scan=True, label="6 · Scanned image - no text layer (needs Gemini)",
         expected="APPROVED", expected_local="FLAGGED - UNREADABLE DOCUMENT", vendor="Meridian Facilities Services",
         address=["640 Depot Road", "Columbus, OH 43204"], number="MFS-7719", date="11/06/2026", po="PO-1007",
         tax=133.60, lines=[("Monthly janitorial service - October", 1, 1400.00), ("HVAC filter replacement", 6, 45.00)]),
    dict(file="07_over_tolerance.pdf", layout="A", label="7 · Price hike - 15% over the PO",
         expected="FLAGGED - OVER TOLERANCE", vendor="Halcyon Print & Media",
         address=["12 Press Row", "Austin, TX 78701"], number="HP-2210", date="07 Nov 2026", po="PO-1008",
         tax=184.00, lines=[("Trade show banner", 5, 180.00), ("Brochure print run", 2000, 0.70)]),
    dict(file="08_unapproved_vendor.pdf", layout="B", label="8 · Unknown vendor quoting a real PO",
         expected="REJECTED - VENDOR NOT APPROVED", vendor="Quickfix Hardware Traders",
         address=["Unit 9, Canal Works", "Newark, NJ 07102"], number="QF-0091", date="November 8, 2026",
         po="PO-1001", tax=71.20, lines=[("Cordless drill kit", 4, 160.00), ("Drill bit set", 10, 25.00)]),
    dict(file="09_embedded_tax_bundled.pdf", layout="C", label="9 · Messy format - bundled line, tax embedded, PO implied",
         expected="APPROVED", vendor="Northwind Office Supplies Inc.", address=NW, number="NW-8870",
         date="November 10, 2026", implied_po="PO-1009", tax=0.0,
         lines=[("Printer toner and office consumables bundle (as quoted)", 1, 648.00)]),
]


def main():
    OUT.mkdir(exist_ok=True)
    manifest = []
    for inv in INVOICES:
        path = OUT / inv["file"]
        {"A": layout_classic, "B": layout_modern, "C": layout_letter}[inv["layout"]](inv, path)
        if inv.get("scan"):
            tmp = path.with_suffix(".tmp.pdf"); path.rename(tmp); make_scan(tmp, path); tmp.unlink()
        manifest.append({k: inv[k] for k in ("file", "label", "expected", "expected_local") if k in inv})
        print("wrote", path.name)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
