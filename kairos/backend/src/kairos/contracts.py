"""Shared data contracts between strategy, risk and execution (spec p.5, p.25, p.97).

Paper and live adapters consume the identical ``OrderIntent`` schema.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any


def _j(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, dict):
        return {k: _j(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_j(v) for v in o]
    return o


def feature_hash(features: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_j(features), sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Candidate:
    strategy_id: str  # settlement | scalp | arbitrage
    ticker: str
    side: str  # yes | no
    action: str  # buy | sell
    max_qty: int
    limit_price: Decimal
    p_point: float  # probability the side wins (settlement) or target-first prob (scalp)
    p_conservative: float
    ev_per_contract: Decimal  # conservative net EV per contract after fees at max_qty
    worst_loss_per_contract: Decimal
    confidence: float | None
    confidence_components: dict[str, float | None]
    kelly_fraction: float
    horizon_s: int
    model_version: str
    feature_hash: str
    reason_codes: list[str] = field(default_factory=list)
    target: Decimal | None = None
    stop: Decimal | None = None
    cutoff_ts: float | None = None
    scenarios: dict[str, float] = field(default_factory=dict)
    legs: list[dict[str, Any]] = field(default_factory=list)
    hold_seconds: float = 900.0
    created_ts: float = 0.0

    @property
    def ev_per_capital_minute(self) -> float:
        cap = float(self.limit_price) if self.limit_price > 0 else 1.0
        minutes = max(self.hold_seconds / 60.0, 0.25)
        return float(self.ev_per_contract) / cap / minutes

    def to_wire(self) -> dict:
        d = _j(asdict(self))
        d["ev_per_capital_minute"] = self.ev_per_capital_minute
        return d


@dataclass
class OrderIntent:
    user_id: int
    ticker: str
    side: str
    action: str
    quantity: int
    limit_price: Decimal
    strategy_id: str
    origin: str  # bot | manual
    reason: str
    fee_reserve: Decimal
    cash_reserve: Decimal
    time_in_force: str = "immediate_or_cancel"
    expiry_ts: float | None = None
    model_version: str = ""
    client_order_id: str = field(default_factory=lambda: f"kx-{uuid.uuid4().hex[:24]}")
    paper: bool = True

    @property
    def idempotency_key(self) -> str:
        return self.client_order_id

    def to_wire(self) -> dict:
        return _j(asdict(self))


@dataclass(frozen=True)
class Fill:
    fill_id: str
    client_order_id: str
    order_id: str
    ticker: str
    side: str
    action: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    is_maker: bool
    exchange_ts: float
    receive_ts: float
    paper: bool = True


@dataclass
class Decision:
    """Persisted for every evaluation — trades and skips (p.108)."""

    ts: float
    user_id: int
    ticker: str
    strategy_id: str
    side: str
    action: str  # enter | exit | hold | skip
    qty: int
    price: Decimal | None
    p_point: float | None
    p_conservative: float | None
    ev: Decimal | None
    confidence: float | None
    size_cap: Decimal | None
    reason_codes: list[str]
    model_version: str
    feature_hash: str
    mode: str
    paper: bool = True

    def to_wire(self) -> dict:
        return _j(asdict(self))
