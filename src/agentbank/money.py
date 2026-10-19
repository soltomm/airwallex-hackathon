"""Money in major units with Decimal arithmetic. Never use floats for amounts."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Currencies without minor units. Everything else rounds to 2 places.
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "IDR", "HUF"}


def _places(currency: str) -> Decimal:
    return Decimal("1") if currency in _ZERO_DECIMAL else Decimal("0.01")


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    @classmethod
    def of(cls, amount: str | int | float | Decimal, currency: str) -> "Money":
        cur = currency.upper()
        return cls(Decimal(str(amount)).quantize(_places(cur), ROUND_HALF_UP), cur)

    def _same(self, other: "Money") -> None:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money.of(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money.of(self.amount - other.amount, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        return not self <= other

    def __ge__(self, other: "Money") -> bool:
        return not self < other

    def api(self) -> float:
        """Value for an Airwallex request body (major units, e.g. 100 is one hundred)."""
        return float(self.amount)

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"
