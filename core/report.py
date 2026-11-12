"""One-page decision report (PDF) for a run - the human-readable twin of the
JSON audit record. Built from the stored run record only, so a report can be
produced for any past run from the dashboard.
"""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .rules import money

TONE = {"APPROVED": ("#e8f6ee", "#137a3f"), "FLAGGED": ("#fff5df", "#9a6400"), "REJECTED": ("#fdecec", "#b42318")}
RESULT = {"pass": "Passed", "warn": "Attention", "fail": "Failed", "skip": "Skipped"}
RESULT_COLOR = {"pass": "#137a3f", "warn": "#9a6400", "fail": "#b42318", "skip": "#7a8494"}
INK, MUTED, RULE = "#16202e", "#5b6676", "#d9dee6"

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12.5, textColor=colors.HexColor(INK))
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, leading=11, textColor=colors.HexColor(MUTED))
H2 = ParagraphStyle("h2", parent=BODY, fontName="Helvetica-Bold", fontSize=10.5, leading=14, spaceBefore=9, spaceAfter=4)
TITLE = ParagraphStyle("title", parent=BODY, fontName="Helvetica-Bold", fontSize=16, leading=20)


def _t(value) -> str:
    """Make text safe for the built-in PDF font and for reportlab markup."""
    s = "—" if value in (None, "") else str(value)
    for a, b in ((" → ", ", "), ("→", "to"), ("≠", "does not equal"), ("₹", "INR "), ("⚠️", ""), ("✓", "")):
        s = s.replace(a, b)
    return escape(s.encode("cp1252", "replace").decode("cp1252"))


def _p(value, style=BODY):
    return Paragraph(_t(value), style)


def _grid(rows, widths, header=True, align_right=()):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor(RULE)),
             ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f6fa")))
    for col in align_right:
        style.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def build_report(record: dict) -> bytes:
    """Return the PDF bytes of the decision report for one run record."""
    inv = record.get("extracted") or {}
    d = record["decision"]
    cur = inv.get("currency") or "USD"
    bg, fg = TONE[d["status"]]
    bold = ParagraphStyle("b", parent=BODY, fontName="Helvetica-Bold")
    right = ParagraphStyle("r", parent=BODY, alignment=2)
    width = A4[0] - 36 * mm - 12   # frame width minus its inner padding
    story = []

    story.append(Paragraph("Invoice decision report", TITLE))
    story.append(_p(f"Run {record['run_id']}  ·  processed {record['timestamp'].replace('T', ' ')}  ·  "
                    f"source file {record['filename']}", SMALL))
    story.append(Spacer(1, 7))

    # decision banner
    code = ParagraphStyle("code", parent=BODY, fontName="Helvetica-Bold", fontSize=14, leading=18,
                          textColor=colors.HexColor(fg))
    sub = ParagraphStyle("sub", parent=BODY, textColor=colors.HexColor(fg))
    lines = [Paragraph(_t(d["code"]), code), Paragraph(_t(d["next_action"]), sub)]
    res = record.get("resolution")
    if res:
        lines.append(Paragraph(
            f"<b>Reviewer decision:</b> {_t(res['action'].upper())}D by {_t(res['by'])} on "
            f"{_t(res['at'].replace('T', ' '))}. Note: {_t(res.get('note') or 'none')}", sub))
    banner = Table([[lines]], colWidths=[width])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
                                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(fg)),
                                ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    story.append(banner)

    # reasons
    story.append(Paragraph("Why this decision was made", H2))
    for reason in d["reasons"]:
        story.append(Paragraph(_t(reason), BODY, bulletText="•"))
    posting = record.get("posting") or (res or {}).get("posting")
    if posting:
        story.append(Paragraph("What changed in procurement", H2))
        story.append(_p(f"{posting['po_number']}: open balance {money(posting['remaining_before'], cur)} before, "
                        f"{money(posting['posted'], cur)} posted, {money(posting['remaining_after'], cur)} left. "
                        f"The purchase order is now {posting['po_status'].replace('_', ' ')}."))

    # invoice details
    story.append(Paragraph("Invoice details", H2))
    po = record.get("po_number")
    if po and record.get("auto_matched_po"):
        po = f"{po} (inferred - not printed on the invoice)"
    tax = "Included in the total" if inv.get("tax_embedded") else money(inv.get("tax"), cur)
    half = width / 2
    left = [("Vendor", record.get("vendor_name") or inv.get("vendor_name")), ("Invoice number", inv.get("invoice_number")),
            ("Invoice date", inv.get("invoice_date")), ("Purchase order", po)]
    rightc = [("Subtotal", money(inv.get("subtotal"), cur)), ("Tax", tax),
              ("Total", money(inv.get("total"), cur)), ("Currency", cur)]
    rows = [[_p(a, SMALL), _p(b if b else "Missing"), _p(c, SMALL), _p(e)] for (a, b), (c, e) in zip(left, rightc)]
    story.append(_grid(rows, [half * 0.3, half * 0.7, half * 0.3, half * 0.7], header=False))

    if inv.get("line_items"):
        story.append(Spacer(1, 5))
        rows = [[_p("Line item", bold), Paragraph("Qty", ParagraphStyle("rb", parent=bold, alignment=2)),
                 Paragraph("Unit price", ParagraphStyle("rb2", parent=bold, alignment=2)),
                 Paragraph("Amount", ParagraphStyle("rb3", parent=bold, alignment=2))]]
        for li in inv["line_items"]:
            q = li.get("quantity")
            rows.append([_p(li["description"]), _p("—" if q is None else f"{q:g}", right),
                         _p(money(li.get("unit_price"), cur), right), _p(money(li["amount"], cur), right)])
        story.append(_grid(rows, [width * 0.52, width * 0.12, width * 0.18, width * 0.18]))

    # the eight checks
    story.append(Paragraph("Checks performed", H2))
    rows = [[_p("Step", bold), _p("Result", bold), _p("Finding", bold)]]
    for n, s in enumerate(record["stages"], 1):
        tone = ParagraphStyle(f"res{n}", parent=BODY, fontName="Helvetica-Bold",
                              textColor=colors.HexColor(RESULT_COLOR[s["status"]]))
        finding = [_p(s["summary"])] + [_p(x, SMALL) for x in s["details"] if s["key"] != "decide"]
        rows.append([_p(f"{n}. {s['title']}"), Paragraph(RESULT[s["status"]], tone), finding])
    story.append(_grid(rows, [width * 0.27, width * 0.12, width * 0.61]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(18 * mm, 10 * mm, f"Invoice Autopilot  ·  extraction: {_unescaped(record['engine'])}  ·  "
                                            f"file fingerprint {record['file_sha']}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=16 * mm, title=f"Invoice decision report {record['run_id']}",
                      author="Invoice Autopilot").build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _unescaped(value) -> str:
    return str(value).encode("cp1252", "replace").decode("cp1252")
