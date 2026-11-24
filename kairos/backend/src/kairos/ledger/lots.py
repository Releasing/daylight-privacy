"""Lot accounting (spec p.12, p.21–22, p.31–32, p.62, p.70, p.76).

* All-in average cost = Σ(qᵢ·aᵢ + fᵢ)/Σqᵢ.
* A sale realizes: proceeds − exit fees − allocated entry cost (incl. entry fees), FIFO lots, entry fees
  allocated pro rata per contract. Remaining basis stays with unsold lots.
* Settlement pays $1 per winning contract; realized = payout − allocated cost.
* Duplicate fill events (same fill_id) are ignored. Sell quantities are reserved so overlapping exits
  cannot sell the same inventory.
* Kalshi nets YES/NO in the same market: buying NO against YES inventory closes YES at 1 − price
  (venue semantics to be verified on the VPS; behaviour is explicit and tested).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..money import ONE, ZERO


class LedgerError(RuntimeError):
    pass


@dataclass
class Lot:
    qty: Decimal
    price: Decimal
    fee: Decimal  # remaining entry fee allocated to this lot
    origin: str
    strategy: str
    opened_ts: float

    @property
    def cost(self) -> Decimal:
        return self.qty * self.price + self.fee


@dataclass
class Realization:
    ticker: str
    side: str
    qty: Decimal
    proceeds: Decimal
    exit_fee: Decimal
    cost_basis: Decimal
    realized: Decimal
    kind: str  # sell | settle | net
    strategy: str
    origin: str
    ts: float


@dataclass
class Position:
    ticker: str
    side: str
    lots: list[Lot] = field(default_factory=list)
    reserved_sell: Decimal = ZERO
    realized: Decimal = ZERO
    fees_paid: Decimal = ZERO
    episode_realized: Decimal = ZERO  # current open→flat episode (community W/L definition, p.136)
    episode_id: int = 0

    @property
    def qty(self) -> Decimal:
        return sum((l.qty for l in self.lots), ZERO)

    @property
    def cost(self) -> Decimal:
        return sum((l.cost for l in self.lots), ZERO)

    @property
    def avg_cost(self) -> Decimal | None:
        q = self.qty
        return (self.cost / q) if q > 0 else None

    @property
    def available(self) -> Decimal:
        return self.qty - self.reserved_sell


class Ledger:
    def __init__(self, netting: bool = True):
        self.positions: dict[tuple[str, str], Position] = {}
        self.seen_fills: set[str] = set()
        self.realizations: list[Realization] = []
        self.netting = netting
        self.closed_episodes: list[dict] = []
        self._episode_seq = 0

    def pos(self, ticker: str, side: str) -> Position:
        return self.positions.setdefault((ticker, side), Position(ticker, side))

    # ---- fills -------------------------------------------------------------------------------
    def apply_fill(self, fill_id: str, ticker: str, side: str, action: str, qty: Decimal, price: Decimal, fee: Decimal,
                   ts: float, origin: str = "bot", strategy: str = "", sell_reservation: bool = False) -> list[Realization]:
        if fill_id in self.seen_fills:
            return []  # duplicate event
        if qty <= 0 or not (ZERO <= price <= ONE) or fee < 0:
            raise LedgerError("invalid fill")
        self.seen_fills.add(fill_id)
        out: list[Realization] = []
        if action == "buy":
            opp = "no" if side == "yes" else "yes"
            opp_pos = self.positions.get((ticker, opp))
            if self.netting and opp_pos is not None and opp_pos.qty > 0:
                # buying the complement closes existing inventory at 1 − price
                close_q = min(qty, opp_pos.available + (opp_pos.reserved_sell if sell_reservation else ZERO))
                if close_q > 0:
                    fee_close = (fee * close_q / qty).quantize(Decimal("0.000001"))
                    out.extend(self._sell(opp_pos, close_q, ONE - price, fee_close, ts, "net", origin, strategy, sell_reservation))
                    qty -= close_q
                    fee -= fee_close
            if qty > 0:
                p = self.pos(ticker, side)
                if p.qty == 0:
                    self._episode_seq += 1
                    p.episode_id = self._episode_seq
                    p.episode_realized = ZERO
                p.lots.append(Lot(qty, price, fee, origin, strategy, ts))
                p.fees_paid += fee
        elif action == "sell":
            p = self.pos(ticker, side)
            out.extend(self._sell(p, qty, price, fee, ts, "sell", origin, strategy, sell_reservation))
        else:
            raise LedgerError("action")
        self.realizations.extend(out)
        return out

    def reserve_sell(self, ticker: str, side: str, qty: Decimal) -> bool:
        p = self.pos(ticker, side)
        if qty <= 0 or qty > p.available:
            return False
        p.reserved_sell += qty
        return True

    def release_sell(self, ticker: str, side: str, qty: Decimal) -> None:
        p = self.pos(ticker, side)
        p.reserved_sell = max(ZERO, p.reserved_sell - qty)

    def _sell(self, p: Position, qty: Decimal, price: Decimal, fee: Decimal, ts: float, kind: str, origin: str,
              strategy: str, from_reservation: bool) -> list[Realization]:
        avail = p.qty if from_reservation else p.available
        if qty > avail:
            raise LedgerError(f"oversell {p.ticker}/{p.side}: {qty} > {avail}")
        if from_reservation:
            p.reserved_sell = max(ZERO, p.reserved_sell - qty)
        remaining = qty
        basis = ZERO
        while remaining > 0:
            lot = p.lots[0]
            take = min(lot.qty, remaining)
            fee_alloc = (lot.fee * take / lot.qty) if lot.qty > 0 else ZERO
            basis += take * lot.price + fee_alloc
            lot.fee -= fee_alloc
            lot.qty -= take
            remaining -= take
            if lot.qty == 0:
                p.lots.pop(0)
        proceeds = qty * price
        realized = proceeds - fee - basis
        p.realized += realized
        p.fees_paid += fee
        p.episode_realized += realized
        r = Realization(p.ticker, p.side, qty, proceeds, fee, basis, realized, kind, strategy, origin, ts)
        if p.qty == 0:
            self._close_episode(p, ts)
        return [r]

    def settle(self, ticker: str, result: str, ts: float) -> list[Realization]:
        """Official result: 'yes' or 'no'. Pays $1 per winning contract; reconciles exactly once."""
        if result not in ("yes", "no"):
            raise LedgerError("settlement result must be yes|no")
        out = []
        for side in ("yes", "no"):
            p = self.positions.get((ticker, side))
            if p is None or p.qty == 0:
                continue
            payout_px = ONE if side == result else ZERO
            q = p.qty
            p.reserved_sell = ZERO
            out.extend(self._sell(p, q, payout_px, ZERO, ts, "settle", p.lots[0].origin, p.lots[0].strategy, True))
        self.realizations.extend(out)
        return out

    def _close_episode(self, p: Position, ts: float) -> None:
        self.closed_episodes.append({"ticker": p.ticker, "side": p.side, "episode": p.episode_id,
                                     "realized": p.episode_realized, "ts": ts})
        p.episode_realized = ZERO

    # ---- views ---------------------------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.qty > 0]

    def worst_case_exposure(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in self.open_positions():
            out[p.ticker] = out.get(p.ticker, ZERO) + p.cost
        return out

    def total_realized(self) -> Decimal:
        return sum((r.realized for r in self.realizations), ZERO)

    def unrealized(self, marks: dict[tuple[str, str], Decimal | None]) -> Decimal | None:
        """Unrealized P&L at executable bids (never midpoint). None if any open position lacks a bid."""
        total = ZERO
        for p in self.open_positions():
            m = marks.get((p.ticker, p.side))
            if m is None:
                return None
            total += p.qty * m - p.cost
        return total
