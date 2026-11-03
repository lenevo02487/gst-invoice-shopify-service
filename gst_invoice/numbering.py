"""Financial-year aware invoice numbering.

Indian FY runs April-March. Invoice numbers must be unique and sequential
within an FY, so the series resets every April. State is a small JSON file
holding the last number per FY label — simple, auditable, no database.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def fy_label(day: date) -> str:
    """date(2026, 11, 2) -> '2026-27'; date(2027, 2, 1) -> '2026-27'."""
    start_year = day.year if day.month >= 4 else day.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def next_invoice_number(state_file: str | Path, day: date,
                        prefix: str = "INV") -> str:
    path = Path(state_file)
    state: dict[str, int] = {}
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
    label = fy_label(day)
    nxt = int(state.get(label, 0)) + 1
    state[label] = nxt
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return f"{prefix}/{label}/{nxt:04d}"
