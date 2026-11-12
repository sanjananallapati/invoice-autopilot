"""Mock procurement system: a handful of JSON files.

data/seed/     read-only starting state (committed to git)
data/runtime/  working copy the app reads and writes (recreated from seed)

Swapping this module for a real ERP / database client is the only change
needed to point the pipeline at a live procurement system.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data" / "seed"
FILES = ["vendors", "purchase_orders", "invoice_ledger", "runs", "settings"]

_runtime_dir = Path(os.environ.get("INVOICE_DATA_DIR", ROOT / "data" / "runtime"))


def set_runtime_dir(path) -> None:
    """Point the store at another folder (used by the tests)."""
    global _runtime_dir
    _runtime_dir = Path(path)


def _path(name: str) -> Path:
    return _runtime_dir / f"{name}.json"


def ensure_runtime() -> None:
    _runtime_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if not _path(name).exists():
            shutil.copy(SEED_DIR / f"{name}.json", _path(name))


def reset() -> None:
    """Restore the demo to its starting state."""
    _runtime_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy(SEED_DIR / f"{name}.json", _path(name))


def load(name: str):
    ensure_runtime()
    with open(_path(name), encoding="utf-8") as fh:
        return json.load(fh)


def save(name: str, data) -> None:
    ensure_runtime()
    fd, tmp = tempfile.mkstemp(dir=_runtime_dir, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _path(name))  # atomic: never leaves a half-written file


# ---- convenience accessors -------------------------------------------------
def vendors():
    return load("vendors")


def purchase_orders():
    return load("purchase_orders")


def ledger():
    return load("invoice_ledger")


def runs():
    return load("runs")


def settings():
    return load("settings")


def save_settings(s) -> None:
    save("settings", s)


def add_run(run) -> None:
    data = runs()
    data.append(run)
    save("runs", data)


def update_run(run) -> None:
    data = runs()
    for i, r in enumerate(data):
        if r["run_id"] == run["run_id"]:
            data[i] = run
    save("runs", data)


def post_to_po(po_number: str, invoice_number, amount: float, date, run_id: str):
    """Draw an approved invoice down against its PO. Returns the updated PO."""
    pos = purchase_orders()
    updated = None
    for po in pos:
        if po["po_number"] == po_number:
            po["remaining"] = round(max(0.0, po["remaining"] - amount), 2)
            po["status"] = "closed" if po["remaining"] <= 0.005 else "partially_invoiced"
            po["invoices"].append({"invoice_number": invoice_number, "amount": round(amount, 2),
                                   "date": date, "run_id": run_id})
            updated = po
    save("purchase_orders", pos)
    return updated


def add_ledger_entry(entry) -> None:
    data = ledger()
    data.append(entry)
    save("invoice_ledger", data)


def set_ledger_status(run_id: str, status: str) -> None:
    data = ledger()
    for e in data:
        if e.get("run_id") == run_id:
            e["status"] = status
    save("invoice_ledger", data)
