"""Fixed-point money arithmetic and the versioned Kalshi fee adapter (spec p.6, p.14, p.63).

All monetary amounts are ``Decimal`` dollars. Contract prices are dollars in [0, 1] with up to
four decimals (Kalshi fixed-point ``*_dollars`` fields). Fees are charged in whole cents, rounded up,
using a per-order accumulator across fills (Kalshi fee-rounding documentation).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
PRICE_Q = Decimal("0.0001")
ZERO = Decimal("0")
ONE = Decimal("1")


class MoneyError(ValueError):
    pass


def D(value: Any) -> Decimal:
    """Strict Decimal conversion. Floats are converted through ``repr`` to avoid binary noise."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool):
        raise MoneyError("bool is not money")
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise MoneyError("non-finite float")
        d = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise MoneyError(f"invalid decimal {value!r}") from exc
    else:
        raise MoneyError(f"unsupported money type {type(value)!r}")
    if not d.is_finite():
        raise MoneyError("non-finite decimal")
    return d


def ceil_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


def floor_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_DOWN)


def round_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_EVEN)


def price(x: Any) -> Decimal:
    p = D(x).quantize(PRICE_Q, rounding=ROUND_HALF_EVEN)
    if p < ZERO or p > ONE:
        raise MoneyError(f"contract price out of range: {p}")
    return p


def cents_to_dollars(c: Any) -> Decimal:
    return (D(c) / 100).quantize(PRICE_Q)


@dataclass(frozen=True)
class FeeSchedule:
    """Versioned series-specific fee schedule.

    ``fee_type``: ``quadratic`` (taker only), ``quadratic_with_maker_fees`` or ``flat``.
    ``unknown`` schedules disable trading (spec p.14: "disable trading if schedule unknown").
    """

    fee_type: str
    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    multiplier: Decimal = Decimal("1")
    flat_per_contract: Decimal = ZERO
    source: str = "unverified"
    effective_from: str = ""

    KNOWN = ("quadratic", "quadratic_with_maker_fees", "flat")

    @property
    def known(self) -> bool:
        return self.fee_type in self.KNOWN

    @property
    def version(self) -> str:
        payload = json.dumps(
            {
                "t": self.fee_type,
                "tr": str(self.taker_rate),
                "mr": str(self.maker_rate),
                "m": str(self.multiplier),
                "f": str(self.flat_per_contract),
                "e": self.effective_from,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def raw_fee(self, count: Decimal, px: Decimal, maker: bool) -> Decimal:
        """Unrounded fee for ``count`` contracts at ``px`` (dollars)."""
        if not self.known:
            raise MoneyError("fee schedule unknown — trading disabled")
        count = D(count)
        px = price(px)
        if count < 0:
            raise MoneyError("negative count")
        if self.fee_type == "flat":
            return self.flat_per_contract * count * self.multiplier
        if maker:
            if self.fee_type != "quadratic_with_maker_fees":
                return ZERO
            rate = self.maker_rate
        else:
            rate = self.taker_rate
        return rate * self.multiplier * count * px * (ONE - px)

    def fee(self, count: Any, px: Any, maker: bool = False) -> Decimal:
        """Fee for a single-fill order, rounded up to the cent."""
        return ceil_cent(self.raw_fee(D(count), D(px), maker))

    @classmethod
    def from_series(cls, series: dict[str, Any]) -> "FeeSchedule":
        fee_type = str(series.get("fee_type") or "unknown")
        mult = series.get("fee_multiplier")
        try:
            multiplier = D(mult) if mult is not None else Decimal("1")
        except MoneyError:
            return cls(fee_type="unknown", source="series-unparseable")
        return cls(fee_type=fee_type, multiplier=multiplier, source="kalshi:series")


@dataclass
class FeeAccumulator:
    """Per-order fee accumulator: the fee charged on each fill is
    ``ceil_cent(raw_total_after) - ceil_cent(raw_total_before)``.
    """

    schedule: FeeSchedule
    maker: bool = False
    raw_total: Decimal = ZERO
    charged: Decimal = ZERO
    fills: list[tuple[Decimal, Decimal, Decimal]] = field(default_factory=list)

    def add_fill(self, count: Any, px: Any) -> Decimal:
        self.raw_total += self.schedule.raw_fee(D(count), D(px), self.maker)
        new_charged = ceil_cent(self.raw_total)
        inc = new_charged - self.charged
        self.charged = new_charged
        self.fills.append((D(count), price(px), inc))
        return inc


def quadratic_fee_float(count: float, px: float, rate: float = 0.07, mult: float = 1.0) -> float:
    """Float approximation used only inside vectorized simulations; never for the ledger."""
    import math

    raw = rate * mult * count * px * (1.0 - px)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0
