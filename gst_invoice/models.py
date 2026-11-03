"""Data models + validation for a GST tax invoice."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
STATE_CODE_RE = re.compile(r"^\d{2}$")


def _check_state_code(code: str, who: str) -> None:
    if not STATE_CODE_RE.match(code or ""):
        raise ValueError(f"{who}: state code must be 2 digits, got {code!r}")


@dataclass(frozen=True)
class Party:
    name: str
    address: str
    state_code: str                      # e.g. "18"
    gstin: str = ""                      # optional for unregistered buyers

    def __post_init__(self) -> None:
        _check_state_code(self.state_code, self.name)
        if self.gstin and not GSTIN_RE.match(self.gstin):
            raise ValueError(f"{self.name}: GSTIN format invalid: {self.gstin!r}")
        if self.gstin and self.gstin[:2] != self.state_code:
            raise ValueError(
                f"{self.name}: GSTIN state prefix {self.gstin[:2]} "
                f"does not match state code {self.state_code}"
            )


@dataclass(frozen=True)
class LineItem:
    name: str
    hsn: str
    qty: Decimal
    rate: Decimal                        # per unit, tax-exclusive
    gst_percent: Decimal                 # e.g. Decimal("18")
    discount_percent: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"{self.name}: qty must be positive")
        if self.rate < 0:
            raise ValueError(f"{self.name}: rate cannot be negative")
        if not (Decimal("0") <= self.discount_percent <= Decimal("100")):
            raise ValueError(f"{self.name}: discount must be 0-100%")


@dataclass
class Invoice:
    number: str
    date: str                            # ISO yyyy-mm-dd
    seller: Party
    buyer: Party
    place_of_supply_state_code: str
    items: list[LineItem] = field(default_factory=list)
    due_date: str = ""

    def __post_init__(self) -> None:
        _check_state_code(self.place_of_supply_state_code, "place of supply")
        if not self.items:
            raise ValueError("invoice needs at least one line item")

    @property
    def is_intra_state(self) -> bool:
        """The one rule that decides the tax split: seller's state vs
        place of supply. Same -> CGST+SGST, different -> IGST."""
        return self.seller.state_code == self.place_of_supply_state_code
