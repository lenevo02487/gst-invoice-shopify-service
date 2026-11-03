"""Render a computed invoice as a clean, print-ready HTML document."""
from __future__ import annotations

import html

from .models import Invoice
from .tax import InvoiceTotals
from .words import amount_in_words


def _inr(value) -> str:
    return f"₹{value:,.2f}"


def render_html(invoice: Invoice, totals: InvoiceTotals) -> str:
    esc = html.escape
    rows = []
    for i, line in enumerate(totals.lines, start=1):
        it = line.item
        tax_cell = (f"CGST ₹{line.cgst:,.2f}<br>SGST ₹{line.sgst:,.2f}"
                    if invoice.is_intra_state else f"IGST ₹{line.igst:,.2f}")
        rows.append(
            f"<tr><td>{i}</td><td>{esc(it.name)}<br><small>HSN {esc(it.hsn)}</small></td>"
            f"<td class='r'>{it.qty}</td><td class='r'>{_inr(it.rate)}</td>"
            f"<td class='r'>{_inr(line.taxable)}</td>"
            f"<td class='r'>{it.gst_percent}%</td><td class='r'>{tax_cell}</td>"
            f"<td class='r'>{_inr(line.total)}</td></tr>"
        )
    summary = "".join(
        f"<tr><td>{s.gst_percent}%</td><td class='r'>{_inr(s.taxable)}</td>"
        f"<td class='r'>{_inr(s.cgst)}</td><td class='r'>{_inr(s.sgst)}</td>"
        f"<td class='r'>{_inr(s.igst)}</td></tr>"
        for s in totals.by_rate
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Tax Invoice {esc(invoice.number)}</title>
<style>
 body{{font-family:system-ui,Arial,sans-serif;color:#1c2420;margin:2rem}}
 table{{border-collapse:collapse;width:100%;margin:1rem 0}}
 th,td{{border:1px solid #c9d2ca;padding:.45rem .6rem;text-align:left}}
 td.r,th.r{{text-align:right}} h1{{font-size:1.4rem;margin:.2rem 0}}
 .meta{{display:flex;justify-content:space-between;gap:2rem;flex-wrap:wrap}}
 .tot td{{font-weight:600}} @media print{{body{{margin:0}}}}
</style></head><body>
<h1>Tax Invoice</h1>
<div class="meta">
 <div><strong>{esc(invoice.seller.name)}</strong><br>{esc(invoice.seller.address)}<br>
 GSTIN: {esc(invoice.seller.gstin or '—')}</div>
 <div>Invoice No: <strong>{esc(invoice.number)}</strong><br>Date: {esc(invoice.date)}<br>
 Due: {esc(invoice.due_date or '—')}<br>
 Place of supply (state code): {esc(invoice.place_of_supply_state_code)}</div>
</div>
<p><strong>Bill to:</strong> {esc(invoice.buyer.name)}<br>{esc(invoice.buyer.address)}<br>
GSTIN: {esc(invoice.buyer.gstin or '—')}</p>
<table>
<tr><th>#</th><th>Item</th><th class="r">Qty</th><th class="r">Rate</th>
<th class="r">Taxable</th><th class="r">GST</th><th class="r">Tax</th><th class="r">Total</th></tr>
{''.join(rows)}
</table>
<table>
<tr><th>Rate</th><th class="r">Taxable</th><th class="r">CGST</th>
<th class="r">SGST</th><th class="r">IGST</th></tr>
{summary}
</table>
<table class="tot">
<tr><td>Subtotal</td><td class="r">{_inr(totals.subtotal)}</td></tr>
<tr><td>Total GST</td><td class="r">{_inr(totals.total_tax)}</td></tr>
<tr><td>Round off</td><td class="r">₹{totals.round_off:+,.2f}</td></tr>
<tr><td>Grand Total</td><td class="r">{_inr(totals.grand_total_rounded)}</td></tr>
</table>
<p><strong>Amount in words:</strong> {amount_in_words(totals.grand_total_rounded)}</p>
</body></html>"""
