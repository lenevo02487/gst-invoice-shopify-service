"""GST tax computation.

Rules implemented (per GST law / CBIC practice):
- Tax is computed PER LINE ITEM at that item's own rate — never one rate
  applied to the invoice total, and never rounding the total tax first.
- All money is Decimal, rounded to the paisa with ROUND_HALF_UP.
- Intra-state: CGST = half the line tax (rounded); SGST = line tax - CGST,
  so CGST + SGST ALWAYS equals the line GST exactly, even on an odd paisa.
- Inter-state: the full line tax is IGST. A line is never both.
- Invoice grand total is rounded to the nearest rupee; the difference is
  shown as an explicit round-off line so the arithmetic stays transparent.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .models import Invoice, LineItem

PAISA = Decimal("0.01")
RUPEE = Decimal("1")


def round_paisa(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ComputedLine:
    item: LineItem
    taxable: Decimal
    tax: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total: Decimal


@dataclass(frozen=True)
class RateSummary:
    gst_percent: Decimal
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal


@dataclass(frozen=True)
class InvoiceTotals:
    lines: list[ComputedLine]
    subtotal: Decimal
    total_tax: Decimal
    total_cgst: Decimal
    total_sgst: Decimal
    total_igst: Decimal
    grand_total: Decimal        # exact, to the paisa
    round_off: Decimal          # grand_total_rounded - grand_total
    grand_total_rounded: Decimal
    by_rate: list[RateSummary]


def compute_line(item: LineItem, intra_state: bool) -> ComputedLine:
    gross = item.qty * item.rate
    taxable = round_paisa(gross * (Decimal("100") - item.discount_percent) / Decimal("100"))
    tax = round_paisa(taxable * item.gst_percent / Decimal("100"))
    if intra_state:
        cgst = round_paisa(tax / 2)
        sgst = tax - cgst            # exact-sum split, odd paisa goes to CGST
        igst = Decimal("0")
    else:
        cgst = sgst = Decimal("0")
        igst = tax
    return ComputedLine(item=item, taxable=taxable, tax=tax,
                        cgst=cgst, sgst=sgst, igst=igst, total=taxable + tax)


def compute_invoice(invoice: Invoice) -> InvoiceTotals:
    lines = [compute_line(it, invoice.is_intra_state) for it in invoice.items]
    subtotal = sum((l.taxable for l in lines), Decimal("0"))
    total_tax = sum((l.tax for l in lines), Decimal("0"))
    cgst = sum((l.cgst for l in lines), Decimal("0"))
    sgst = sum((l.sgst for l in lines), Decimal("0"))
    igst = sum((l.igst for l in lines), Decimal("0"))
    grand = subtotal + total_tax
    rounded = grand.quantize(RUPEE, rounding=ROUND_HALF_UP)

    buckets: dict[Decimal, RateSummary] = {}
    for line in lines:
        key = line.item.gst_percent
        prev = buckets.get(key, RateSummary(key, Decimal("0"), Decimal("0"),
                                            Decimal("0"), Decimal("0")))
        buckets[key] = RateSummary(
            gst_percent=key,
            taxable=prev.taxable + line.taxable,
            cgst=prev.cgst + line.cgst,
            sgst=prev.sgst + line.sgst,
            igst=prev.igst + line.igst,
        )
    return InvoiceTotals(
        lines=lines, subtotal=subtotal, total_tax=total_tax,
        total_cgst=cgst, total_sgst=sgst, total_igst=igst,
        grand_total=grand, round_off=rounded - grand,
        grand_total_rounded=rounded,
        by_rate=[buckets[k] for k in sorted(buckets)],
    )


def extract_base_from_inclusive(inclusive: Decimal, gst_percent: Decimal) -> Decimal:
    """Tax inside a tax-INCLUSIVE price: base = inclusive / (1 + rate/100).
    (Taking rate% of the inclusive amount is the classic overstatement bug.)"""
    return round_paisa(inclusive * Decimal("100") / (Decimal("100") + gst_percent))
