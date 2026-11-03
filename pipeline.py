"""Phase 0 prototype pipeline: Shopify order JSON -> print-ready GST invoice.

Local only — no Shopify API, no webhook. The adapter (this file) translates a
Shopify-style order into the existing GST engine's models; ALL tax math is
done by the unmodified engine package (gst_invoice/), including the
CGST/SGST vs IGST split and Decimal rounding.

Shopify notes handled here:
- Prices are tax-INCLUSIVE when the order says taxes_included=true (the
  usual Indian store setup): the taxable base is extracted with the engine's
  extract_base_from_inclusive, never by taking rate% of the gross.
- Line ``total_discount`` is in inclusive money, so it is subtracted before
  base extraction; the engine then sees the true per-unit base rate.
- GST rate per line = sum of the line's tax_lines rates (Shopify splits
  CGST/SGST into separate tax_lines; the engine re-splits itself, so only
  the combined rate is passed).
- HSN comes from a line property named "HSN" (in Phase 1 this will be a
  product metafield — Shopify orders do not carry HSN natively).
- Place of supply = shipping address state (GST rule for goods), mapped
  from Shopify's province_code to the 2-digit GST state code.

Usage:
    python3 pipeline.py samples/order_intra_state.json samples/seller.json \
        output/invoice_intra_state.html
Writes the HTML invoice and a sibling ``<name>.totals.json`` sidecar that
verify.py checks against an independent recomputation.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gst_invoice.models import Invoice, LineItem, Party
from gst_invoice.render import render_html
from gst_invoice.tax import compute_invoice, extract_base_from_inclusive

# Shopify province_code (ISO 3166-2:IN) -> GST state code
PROVINCE_TO_GST_CODE = {
    "AN": "35", "AP": "37", "AR": "12", "AS": "18", "BR": "10", "CH": "04",
    "CT": "22", "DL": "07", "DD": "26", "GA": "30", "GJ": "24", "HR": "06",
    "HP": "02", "JK": "01", "JH": "20", "KA": "29", "KL": "32", "LA": "38",
    "LD": "31", "MP": "23", "MH": "27", "MN": "14", "ML": "17", "MZ": "15",
    "NL": "13", "OR": "21", "PB": "03", "PY": "34", "RJ": "08", "SK": "11",
    "TN": "33", "TS": "36", "TR": "16", "UP": "09", "UT": "05", "WB": "19",
}


def gst_state_code(address: dict) -> str:
    code = PROVINCE_TO_GST_CODE.get((address.get("province_code") or "").upper())
    if not code:
        raise ValueError(f"unknown province_code: {address.get('province_code')!r}")
    return code


def _addr_text(a: dict) -> str:
    parts = [a.get("address1", ""), a.get("city", ""),
             a.get("province", ""), a.get("zip", "")]
    return ", ".join(p for p in parts if p)


def _hsn(line: dict) -> str:
    for prop in line.get("properties", []):
        if prop.get("name", "").upper() == "HSN":
            return str(prop.get("value", ""))
    return ""


def order_to_invoice(order: dict, seller_cfg: dict) -> Invoice:
    ship = order.get("shipping_address") or order.get("billing_address") or {}
    bill = order.get("billing_address") or ship
    supply_code = gst_state_code(ship)

    seller = Party(
        name=seller_cfg["name"], address=seller_cfg.get("address", ""),
        state_code=str(seller_cfg["state_code"]), gstin=seller_cfg.get("gstin", ""),
    )
    buyer = Party(
        name=bill.get("name", ""), address=_addr_text(bill),
        state_code=gst_state_code(bill), gstin=order.get("buyer_gstin", ""),
    )

    inclusive = bool(order.get("taxes_included", False))
    items: list[LineItem] = []
    for line in order["line_items"]:
        qty = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["price"]))
        discount = Decimal(str(line.get("total_discount", "0")))
        gst_percent = sum(
            (Decimal(str(t.get("rate", "0"))) for t in line.get("tax_lines", [])),
            Decimal("0"),
        ) * Decimal("100")
        gross = unit_price * qty - discount
        if inclusive:
            base_total = extract_base_from_inclusive(gross, gst_percent)
            unit_base = base_total / qty  # engine rounds taxable back to paisa
        else:
            unit_base = unit_price
            if discount:
                unit_base = (gross) / qty
        items.append(LineItem(
            name=line["title"], hsn=_hsn(line), qty=qty,
            rate=unit_base, gst_percent=gst_percent,
        ))

    prefix = seller_cfg.get("invoice_prefix", "SHP")
    return Invoice(
        number=f"{prefix}/{order['order_number']}",
        date=str(order.get("created_at", ""))[:10],
        seller=seller, buyer=buyer,
        place_of_supply_state_code=supply_code,
        items=items,
    )


def totals_sidecar(invoice: Invoice, totals) -> dict:
    return {
        "invoice_number": invoice.number,
        "intra_state": invoice.is_intra_state,
        "place_of_supply_state_code": invoice.place_of_supply_state_code,
        "lines": [
            {
                "name": l.item.name, "hsn": l.item.hsn,
                "qty": str(l.item.qty), "gst_percent": str(l.item.gst_percent),
                "taxable": str(l.taxable), "cgst": str(l.cgst),
                "sgst": str(l.sgst), "igst": str(l.igst), "total": str(l.total),
            }
            for l in totals.lines
        ],
        "subtotal": str(totals.subtotal),
        "total_tax": str(totals.total_tax),
        "total_cgst": str(totals.total_cgst),
        "total_sgst": str(totals.total_sgst),
        "total_igst": str(totals.total_igst),
        "grand_total": str(totals.grand_total),
        "round_off": str(totals.round_off),
        "grand_total_rounded": str(totals.grand_total_rounded),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    order_path, seller_path, out_path = map(Path, argv[1:4])
    order = json.loads(order_path.read_text())
    seller_cfg = json.loads(seller_path.read_text())

    invoice = order_to_invoice(order, seller_cfg)
    totals = compute_invoice(invoice)

    # Cross-checks against Shopify's own header numbers (order JSON fields).
    checks = []
    if order.get("total_tax") is not None:
        checks.append(("order.total_tax", Decimal(str(order["total_tax"])), totals.total_tax))
    if order.get("total_price") is not None:
        checks.append(("order.total_price", Decimal(str(order["total_price"])), totals.grand_total))
    for label, expected, got in checks:
        status = "OK " if expected == got else "MISMATCH"
        print(f"[cross-check] {label}: shopify={expected} engine={got} -> {status}")
        if expected != got:
            return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(invoice, totals))
    sidecar = out_path.with_suffix(".totals.json")
    sidecar.write_text(json.dumps(totals_sidecar(invoice, totals), indent=2))
    print(f"[ok] {invoice.number} ({'intra-state CGST+SGST' if invoice.is_intra_state else 'inter-state IGST'})"
          f" subtotal={totals.subtotal} tax={totals.total_tax}"
          f" grand={totals.grand_total} rounded={totals.grand_total_rounded}")
    print(f"[ok] HTML -> {out_path}")
    print(f"[ok] totals -> {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
