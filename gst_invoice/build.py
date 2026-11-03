"""Build an Invoice from plain dicts (parsed JSON) — input boundary layer."""
from __future__ import annotations

from decimal import Decimal

from .models import Invoice, LineItem, Party


def _party(d: dict) -> Party:
    return Party(name=d["name"], address=d.get("address", ""),
                 state_code=str(d["state_code"]), gstin=d.get("gstin", ""))


def _dec(value) -> Decimal:
    return Decimal(str(value))


def invoice_from_dict(data: dict) -> Invoice:
    inv = data.get("invoice", {})
    items = [
        LineItem(name=it["name"], hsn=str(it.get("hsn", "")),
                 qty=_dec(it["qty"]), rate=_dec(it["rate"]),
                 gst_percent=_dec(it["gst_percent"]),
                 discount_percent=_dec(it.get("discount_percent", 0)))
        for it in data["items"]
    ]
    return Invoice(
        number=str(inv.get("number", "")),
        date=str(inv.get("date", "")),
        due_date=str(inv.get("due_date", "")),
        seller=_party(data["seller"]),
        buyer=_party(data["buyer"]),
        place_of_supply_state_code=str(data["place_of_supply_state_code"]),
        items=items,
    )
