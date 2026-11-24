"""Intent journal, order lifecycle, paper fill simulation and the gated live adapter
(spec p.5, p.15, p.22, p.30, p.48, p.72, p.98, p.135).

* The intent and its unique client order ID are persisted **before** submission.
* States: created, submitted, acknowledged, resting, partial, filled, canceled, rejected, unknown, expired.
  ``unknown`` is not ``rejected``: it blocks new entries until reconciled.
* Inventory changes only on fills — acknowledgements never change it.
* Paper fills consume visible depth after measured latency; a touched quote is not a fill; resting orders
  fill only when trades print *through* the limit (conservative queue). Consumed liquidity is remembered
  so the simulator never fills the same displayed size twice.
* The paper adapter has no reference to any authenticated write path.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from ..contracts import Fill, OrderIntent
from ..market.book import OrderBook
from ..money import ZERO, FeeAccumulator, FeeSchedule, price

TERMINAL = {"filled", "canceled", "rejected", "expired"}


class ExecutionError(RuntimeError):
    pass


class LiveTradingDisabled(ExecutionError):
    pass


@dataclass
class Order:
    intent: OrderIntent
    state: str = "created"
    order_id: str = ""
    filled: Decimal = ZERO
    filled_cost: Decimal = ZERO
    fees: Decimal = ZERO
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)
    submit_mono: float | None = None
    ack_latency_ms: float | None = None
    reject_reason: str | None = None
    fee_acc: FeeAccumulator | None = None
    history: list[tuple[float, str]] = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return Decimal(self.intent.quantity) - self.filled

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def to_wire(self) -> dict:
        i = self.intent
        return {"client_order_id": i.client_order_id, "order_id": self.order_id, "ticker": i.ticker, "side": i.side,
                "action": i.action, "quantity": i.quantity, "limit": str(i.limit_price), "state": self.state,
                "filled": str(self.filled), "avg_price": str((self.filled_cost / self.filled).quantize(Decimal("0.0001"))) if self.filled else None,
                "fees": str(self.fees), "origin": i.origin, "strategy": i.strategy_id, "paper": i.paper,
                "tif": i.time_in_force, "created_ts": self.created_ts, "updated_ts": self.updated_ts,
                "reject_reason": self.reject_reason}


_ALLOWED = {
    "created": {"submitted", "rejected"},
    "submitted": {"acknowledged", "rejected", "unknown", "partial", "filled", "canceled"},
    "acknowledged": {"resting", "partial", "filled", "canceled", "rejected", "expired", "unknown"},
    "resting": {"partial", "filled", "canceled", "expired", "unknown"},
    "partial": {"partial", "filled", "canceled", "expired", "unknown"},
    "unknown": {"acknowledged", "resting", "partial", "filled", "canceled", "rejected", "expired"},
}


class Journal:
    """Authoritative order journal; persistence callback is invoked synchronously on every change."""

    def __init__(self, persist: Callable[[Order], None] | None = None):
        self.orders: dict[str, Order] = {}
        self.persist = persist or (lambda o: None)

    def record(self, intent: OrderIntent) -> Order:
        if intent.client_order_id in self.orders:
            raise ExecutionError("duplicate client_order_id")  # idempotency: never submit twice
        o = Order(intent)
        o.history.append((time.time(), "created"))
        self.orders[intent.client_order_id] = o
        self.persist(o)  # durable BEFORE submit (p.15)
        return o

    def transition(self, o: Order, state: str, reason: str | None = None) -> None:
        if o.state == state and state == "partial":
            pass
        elif state not in _ALLOWED.get(o.state, set()):
            raise ExecutionError(f"illegal transition {o.state}->{state}")
        o.state = state
        o.updated_ts = time.time()
        if reason:
            o.reject_reason = reason
        o.history.append((o.updated_ts, state))
        self.persist(o)

    def apply_fill(self, o: Order, qty: Decimal, px: Decimal, fee: Decimal) -> None:
        if qty <= 0 or qty > o.remaining:
            raise ExecutionError("fill exceeds remaining quantity")
        o.filled += qty
        o.filled_cost += qty * px
        o.fees += fee
        self.transition(o, "filled" if o.remaining == 0 else "partial")

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.terminal]

    def unknown(self) -> list[Order]:
        return [o for o in self.orders.values() if o.state == "unknown"]


@dataclass
class PaperExchange:
    """Conservative fill simulator for paper mode (shared proposal schema with live)."""

    fees: FeeSchedule
    latency_ms: float = 150.0
    consumed: dict[tuple[str, str, Decimal], tuple[Decimal, float]] = field(default_factory=dict)

    consume_memory_s: float = 5.0

    def _available(self, book: OrderBook, side_book: str, px: Decimal, qty: Decimal, now: float) -> Decimal:
        """Displayed size minus what our own simulated fills already took at this level recently
        (the real book does not reflect paper fills, so the same liquidity must not be used twice)."""
        used, ts = self.consumed.get((book.ticker, side_book, px), (ZERO, 0.0))
        if now - ts > self.consume_memory_s:
            used = ZERO
        return max(ZERO, qty - used)

    def _consume(self, book: OrderBook, side_book: str, px: Decimal, q: Decimal, now: float) -> None:
        used, ts = self.consumed.get((book.ticker, side_book, px), (ZERO, now))
        if now - ts > self.consume_memory_s:
            used = ZERO
        self.consumed[(book.ticker, side_book, px)] = (used + q, now)

    def execute_ioc(self, o: Order, book: OrderBook, now: float) -> list[Fill]:
        """Marketable limit IOC against the book as seen *after* latency (caller passes the later book)."""
        i = o.intent
        fills: list[Fill] = []
        if not book.valid:
            return fills
        if o.fee_acc is None:
            o.fee_acc = FeeAccumulator(self.fees, maker=False)
        remaining = o.remaining
        if i.action == "buy":
            # YES asks come from NO bids (price 1 − p); walk ascending
            opp = "no" if i.side == "yes" else "yes"
            levels = sorted(((Decimal(1) - p, q, p) for p, q in (book.no if i.side == "yes" else book.yes).items()), key=lambda x: x[0])
            for ask, q, raw in levels:
                if remaining <= 0 or ask > i.limit_price:
                    break
                avail = self._available(book, opp, raw, q, now)
                take = min(avail, remaining)
                if take <= 0:
                    continue
                take = Decimal(int(take))  # whole contracts only
                if take <= 0:
                    continue
                self._consume(book, opp, raw, take, now)
                fills.append(self._fill(o, take, ask, now))
                remaining -= take
        else:
            levels = sorted((book.yes if i.side == "yes" else book.no).items(), key=lambda x: -x[0])
            for bid, q in levels:
                if remaining <= 0 or bid < i.limit_price:
                    break
                avail = self._available(book, i.side, bid, q, now)
                take = Decimal(int(min(avail, remaining)))
                if take <= 0:
                    continue
                self._consume(book, i.side, bid, take, now)
                fills.append(self._fill(o, take, bid, now))
                remaining -= take
        return fills

    def resting_fill_from_trade(self, o: Order, trade_yes_price: Decimal, trade_count: Decimal, now: float) -> list[Fill]:
        """Resting limit fills only when a trade prints strictly through the limit (queue unknown → pessimistic)."""
        i = o.intent
        side_px = trade_yes_price if i.side == "yes" else Decimal(1) - trade_yes_price
        through = side_px < i.limit_price if i.action == "buy" else side_px > i.limit_price
        if not through or o.remaining <= 0:
            return []
        if o.fee_acc is None:
            o.fee_acc = FeeAccumulator(self.fees, maker=True)
        q = Decimal(int(min(o.remaining, trade_count)))
        return [self._fill(o, q, i.limit_price, now, maker=True)] if q > 0 else []

    def _fill(self, o: Order, q: Decimal, px: Decimal, now: float, maker: bool = False) -> Fill:
        fee = o.fee_acc.add_fill(q, px)  # type: ignore[union-attr]
        return Fill(f"pf-{uuid.uuid4().hex[:16]}", o.intent.client_order_id, o.order_id or "paper", o.intent.ticker,
                    o.intent.side, o.intent.action, q, price(px), fee, maker, now, now, True)


class LiveAdapter:
    """Authenticated order submission. Disabled unless every administrative gate is satisfied (p.4, p.100)."""

    def __init__(self, rest, order_path: str, authorized: Callable[[], tuple[bool, str]]):
        self.rest = rest
        self.order_path = order_path
        self.authorized = authorized

    def body(self, i: OrderIntent) -> dict:
        px = str(i.limit_price.quantize(Decimal("0.0001")))
        b = {"ticker": i.ticker, "client_order_id": i.client_order_id, "side": i.side, "action": i.action,
             "count": i.quantity, "type": "limit", "time_in_force": i.time_in_force}
        b["yes_price_dollars" if i.side == "yes" else "no_price_dollars"] = px
        return b

    async def submit(self, i: OrderIntent):
        ok, why = self.authorized()
        if i.paper or not ok:
            raise LiveTradingDisabled(why or "paper intent")
        return await self.rest.write("POST", self.order_path, self.body(i))

    async def cancel(self, order_id: str):
        ok, why = self.authorized()
        if not ok:
            raise LiveTradingDisabled(why)
        return await self.rest.write("DELETE", f"/portfolio/orders/{order_id}")
