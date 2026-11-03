"""Amount in words — Indian numbering system (crore / lakh / thousand)."""
from __future__ import annotations

from decimal import Decimal

ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
        "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
        "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
        "Eighty", "Ninety"]


def _below_hundred(n: int) -> str:
    if n < 20:
        return ONES[n]
    return (TENS[n // 10] + " " + ONES[n % 10]).strip()


def _below_thousand(n: int) -> str:
    if n < 100:
        return _below_hundred(n)
    rest = _below_hundred(n % 100)
    return (ONES[n // 100] + " Hundred" + (" " + rest if rest else "")).strip()


def amount_in_words(amount: Decimal) -> str:
    rupees = int(amount)
    paise = int((amount - Decimal(rupees)) * 100)
    if rupees == 0 and paise == 0:
        return "Rupees Zero Only"
    crore, rem = divmod(rupees, 10_000_000)
    lakh, rem = divmod(rem, 100_000)
    thousand, rest = divmod(rem, 1_000)
    parts: list[str] = []
    if crore:
        parts.append(_below_hundred(crore) + " Crore")
    if lakh:
        parts.append(_below_hundred(lakh) + " Lakh")
    if thousand:
        parts.append(_below_hundred(thousand) + " Thousand")
    if rest:
        parts.append(_below_thousand(rest))
    text = " ".join(parts)
    result = f"Rupees {text}" if text else "Rupees Zero"
    if paise:
        result += f" and Paise {_below_hundred(paise)}"
    return result + " Only"
