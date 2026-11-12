"""Stage 2 - turn an invoice PDF into structured fields.

Two engines behind one function:

* Gemini (primary)   - reads the PDF natively, so it copes with scans, odd
                       layouts, bundled lines and implied PO references.
* Local parser       - pdfplumber + pattern matching. No network, no key.
                       Used when no key is configured, or automatically if the
                       Gemini call fails, so a rate limit never breaks a run.

Both engines return the same dictionary, and everything downstream (the
business rules) is deterministic code - the LLM only reads, it never decides.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re

import pdfplumber
import requests
from dateutil import parser as dateparser

FIELDS = ["vendor_name", "invoice_number", "invoice_date", "po_number", "currency",
          "line_items", "subtotal", "tax", "total", "tax_embedded"]

DEFAULT_MODELS = "gemini-3.5-flash-lite,gemini-3.8-flash,gemini-3.5-flash"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

PROMPT = """You are an accounts-payable data extraction engine. Read the attached vendor invoice and return ONE JSON object with exactly these keys:

{
  "vendor_name": string or null,        // the company that ISSUED the invoice (seller), not the bill-to customer
  "invoice_number": string or null,
  "invoice_date": "YYYY-MM-DD" or null, // the invoice/issue date, not the due date
  "po_number": string or null,          // the buyer's purchase order reference, wherever it appears (header, notes, body text)
  "currency": string or null,           // ISO code, e.g. "USD"
  "line_items": [ {"description": string, "quantity": number or null, "unit_price": number or null, "amount": number} ],
  "subtotal": number or null,           // pre-tax amount
  "tax": number or null,                // tax amount if shown separately
  "total": number or null,              // final amount payable
  "tax_embedded": boolean               // true if prices/total are stated as tax-inclusive with no separate tax amount
}

Rules:
- Never guess. If a value is not on the document, use null. Do NOT invent a PO number or invoice number.
- Charges such as freight, shipping, handling or surcharges are line items even if printed outside the item table.
- Numbers are plain numbers: no currency symbols, no thousands separators.
- Return only the JSON object."""


class ExtractionError(Exception):
    pass


# ---------------------------------------------------------------- helpers ---
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _qty(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _text(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in {"", "n/a", "na", "none", "null", "-", "—", "not provided"} else s


def _date(v):
    s = _text(v)
    if not s:
        return None
    try:
        return dateparser.parse(s, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


def normalize(raw: dict) -> dict:
    """Coerce whatever an engine returned into one clean, typed shape."""
    raw = raw or {}
    items = []
    for li in raw.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        amount = _num(li.get("amount"))
        qty, unit = _qty(li.get("quantity")), _num(li.get("unit_price"))
        if amount is None and qty is not None and unit is not None:
            amount = round(qty * unit, 2)
        if amount is None:
            continue
        items.append({"description": _text(li.get("description")) or "(no description)",
                      "quantity": qty, "unit_price": unit, "amount": amount})
    cur = (_text(raw.get("currency")) or "USD").upper()
    cur = {"$": "USD", "US$": "USD"}.get(cur, cur)
    return {
        "vendor_name": _text(raw.get("vendor_name")),
        "invoice_number": _text(raw.get("invoice_number")),
        "invoice_date": _date(raw.get("invoice_date")),
        "po_number": _text(raw.get("po_number")),
        "currency": cur,
        "line_items": items,
        "subtotal": _num(raw.get("subtotal")),
        "tax": _num(raw.get("tax")),
        "total": _num(raw.get("total")),
        "tax_embedded": bool(raw.get("tax_embedded")),
    }


def read_text_layer(pdf_bytes: bytes):
    """Return (text, page_count). Empty text means a scanned / image-only PDF."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
            return "\n".join(pages).strip(), len(pages)
    except Exception as exc:  # corrupt or not a PDF
        raise ExtractionError(f"File could not be opened as a PDF ({exc.__class__.__name__}).")


# ----------------------------------------------------------- local parser ---
MONEY = r"\(?-?\$?\s?[\d,]+\.\d{2}\)?"
DATE_PATTERNS = [
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}",
    r"\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}",
    r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}",
]
SKIP_VENDOR = re.compile(r"^(tax\s+)?invoice\b|^page\b|^bill\s*to|^ship\s*to|^original|^statement", re.I)


def _last_money(line: str):
    found = re.findall(MONEY, line)
    return _num(found[-1]) if found else None


def parse_locally(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {"line_items": [], "tax_embedded": False}

    # vendor: an explicit label wins, otherwise the letterhead (first real line)
    m = re.search(r"(?:^|\n)\s*(?:from|vendor|supplier|seller|issued by)\s*:\s*(.+)", text, re.I)
    if m:
        out["vendor_name"] = m.group(1).strip()
    else:
        out["vendor_name"] = next((ln for ln in lines[:6] if not SKIP_VENDOR.search(ln)), None)

    m = re.search(r"\b(?:invoice|inv)\.?\s*(?:no\.?|number|num\.?|#|id)\s*[:#]?\s*([A-Z0-9][\w\-/]*)", text, re.I)
    if m:
        out["invoice_number"] = m.group(1)

    m = re.search(r"(?<!due )(?<!due)\b(?:invoice\s+date|date\s+of\s+issue|issue\s+date|dated|date)\s*:?\s*(.{0,30})", text, re.I)
    if m:
        for pat in DATE_PATTERNS:
            d = re.search(pat, m.group(1))
            if d:
                out["invoice_date"] = d.group(0)
                break

    m = re.search(r"\b(?:p\.?\s?o\.?|purchase\s+order)\s*(?:number|no\.?|#|ref(?:erence)?\.?)?\s*[:#]?\s*((?:PO[-\s]?)?\d{3,}[\w-]*)", text, re.I)
    if not m:
        m = re.search(r"\b(PO[-\s]?\d{3,}[\w-]*)", text)
    if m:
        out["po_number"] = m.group(1).strip()

    for code in ("USD", "EUR", "GBP", "INR", "CAD", "AUD"):
        if re.search(rf"\b{code}\b", text):
            out["currency"] = code
            break

    # totals block + line items
    in_items = False
    for ln in lines:
        low = ln.lower()
        money = _last_money(ln)
        if re.match(r"^sub[\s-]?total", low):
            out["subtotal"], in_items = money, False
        elif re.match(r"^(sales\s+tax|tax|gst|vat|hst)\b(?!\s*(id|no|reg))", low) and money is not None:
            out["tax"], in_items = money, False
        elif re.match(r"^(grand\s+total|invoice\s+total|total|amount\s+due|balance\s+due|amount\s+payable)", low) and money is not None:
            out["total"], in_items = money, False
            if re.search(r"incl", low):
                out["tax_embedded"] = True
        elif re.search(r"\bdescription\b|\bparticulars\b|\bitem\b.*\bamount\b", low) and money is None:
            in_items = True
        elif in_items and money is not None:
            full = re.match(rf"^(?P<d>.+?)\s+(?P<q>[\d,]+(?:\.\d+)?)\s*(?:hrs?|hours|pcs|ea|units?|x)?\s+(?P<u>{MONEY})\s+(?P<a>{MONEY})$", ln)
            if full:
                out["line_items"].append({"description": full["d"], "quantity": full["q"],
                                          "unit_price": full["u"], "amount": full["a"]})
            else:
                desc = re.sub(rf"\s*{MONEY}$", "", ln).strip()
                out["line_items"].append({"description": desc, "quantity": None,
                                          "unit_price": None, "amount": money})
    if re.search(r"inclusive\s+of|incl(?:\.|uding)?\s+(?:\d+%\s+)?(?:sales\s+)?(?:tax|gst|vat)|tax\s+included", text, re.I) \
            and out.get("tax") is None:
        out["tax_embedded"] = True
    return out


# ------------------------------------------------------------ Gemini REST ---
def _json_from_response(payload):
    """Find the model's JSON answer wherever the API put it (works for both
    the generateContent and the Interactions response shapes)."""
    texts = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("text", "output_text") and isinstance(v, str):
                    texts.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    for t in reversed(texts):
        t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(t[start:end + 1])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise ExtractionError("Gemini replied, but not with readable JSON.")


def gemini_models():
    return [m.strip() for m in os.environ.get("GEMINI_MODELS", DEFAULT_MODELS).split(",") if m.strip()]


def call_gemini(pdf_bytes: bytes, api_key: str, timeout: int = 60):
    """Returns (fields_dict, model_used). Tries each configured model in turn."""
    b64 = base64.b64encode(pdf_bytes).decode()
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    errors = []
    for model in gemini_models():
        attempts = [
            (f"{API_ROOT}/models/{model}:generateContent",
             {"contents": [{"parts": [{"inline_data": {"mime_type": "application/pdf", "data": b64}},
                                      {"text": PROMPT}]}],
              "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}),
            (f"{API_ROOT}/interactions",
             {"model": model,
              "input": [{"type": "document", "data": b64, "mime_type": "application/pdf"},
                        {"type": "text", "text": PROMPT}]}),
        ]
        for url, body in attempts:
            try:
                r = requests.post(url, headers=headers, json=body, timeout=timeout)
            except requests.RequestException as exc:
                errors.append(f"{model}: network error ({exc.__class__.__name__})")
                break
            if r.status_code == 200:
                return _json_from_response(r.json()), model
            try:
                msg = r.json().get("error", {}).get("message", "")[:140]
            except ValueError:
                msg = r.text[:140]
            errors.append(f"{model}: HTTP {r.status_code} {msg}")
            if r.status_code in (401, 403):      # bad key: no point trying further
                raise ExtractionError("Gemini rejected the API key. " + msg)
            if r.status_code == 429:             # quota: try the next model
                break
            # 400 / 404: try the other endpoint shape, then the next model
    raise ExtractionError("Gemini call failed - " + " | ".join(errors[-3:]))


def test_gemini(api_key: str):
    """Used by the Settings page: which models can this key see?"""
    r = requests.get(f"{API_ROOT}/models", headers={"x-goog-api-key": api_key},
                     params={"pageSize": 200}, timeout=20)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    names = [m.get("name", "").replace("models/", "") for m in r.json().get("models", [])]
    usable = [m for m in gemini_models() if m in names]
    flash = sorted(n for n in names if "flash" in n)[:12]
    if usable:
        return True, f"Key works. Will use: {usable[0]}. (Configured order: {', '.join(gemini_models())})"
    return False, ("Key works, but none of the configured models are available to it. "
                   f"Set GEMINI_MODELS to one of: {', '.join(flash) or ', '.join(names[:12])}")


# -------------------------------------------------------------- main entry ---
def extract(pdf_bytes: bytes, engine: str = "auto", api_key: str | None = None) -> dict:
    """engine: 'auto' (Gemini if a key exists, else local), 'gemini', or 'local'."""
    text, pages = read_text_layer(pdf_bytes)
    has_text = len(text) >= 20
    info = {"pages": pages, "text_layer": has_text, "warnings": [], "engine": None, "fields": None}
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    use_gemini = engine == "gemini" or (engine == "auto" and bool(api_key))

    if use_gemini:
        if not api_key:
            info["warnings"].append("Gemini selected but no API key is configured - used the local parser.")
        else:
            try:
                raw, model = call_gemini(pdf_bytes, api_key)
                info["engine"], info["fields"] = f"Gemini ({model})", normalize(raw)
                return info
            except ExtractionError as exc:
                info["warnings"].append(f"{exc} Fell back to the local parser.")

    if not has_text:
        raise ExtractionError(
            "This PDF is a scanned image with no text layer. Reading it needs the Gemini engine"
            + (" - " + info["warnings"][-1] if info["warnings"] else " (add a GEMINI_API_KEY).")
        )
    info["engine"], info["fields"] = "Local parser (pdfplumber)", normalize(parse_locally(text))
    return info
