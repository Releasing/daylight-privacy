"""Schema-tolerant parsers for Kalshi payloads (spec p.2, p.133).

Kalshi migrated from integer-cent fields (``yes_bid``) to fixed-point dollar strings
(``yes_bid_dollars``) and fixed-point counts (``*_fp``). Parsers accept both and record which variant
was seen; unknown or missing critical fields make the market ineligible (fail closed) rather than
substituting zero.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..money import D, MoneyError, cents_to_dollars, price


class ParseError(ValueError):
    pass


def parse_ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:  # ms
            x /= 1000.0
        return datetime.fromtimestamp(x, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ParseError(f"bad timestamp {v!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _price_field(obj: dict[str, Any], name: str) -> Decimal | None:
    """Read ``name_dollars`` (preferred) or legacy integer-cent ``name``. Missing → None (never 0)."""
    dv = obj.get(f"{name}_dollars")
    if dv not in (None, ""):
        try:
            return price(dv)
        except MoneyError:
            return None
    cv = obj.get(name)
    if cv in (None, ""):
        return None
    try:
        return price(cents_to_dollars(cv))
    except MoneyError:
        return None


def _count_field(obj: dict[str, Any], name: str) -> Decimal | None:
    for key in (f"{name}_fp", name):
        v = obj.get(key)
        if v not in (None, ""):
            try:
                return D(v)
            except MoneyError:
                return None
    return None


@dataclass(frozen=True)
class MarketMeta:
    ticker: str
    event_ticker: str
    series_ticker: str
    status: str
    open_time: datetime | None
    close_time: datetime | None
    expiration_time: datetime | None
    strike: Decimal | None
    strike_type: str | None
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    last_price: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None
    result: str | None
    title: str
    subtitle: str
    rules_primary: str
    rules_secondary: str
    tick_size: Decimal | None
    settlement_value: Decimal | None
    raw_variant: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def rules_hash(self) -> str:
        return hashlib.sha256((self.rules_primary + "\n" + self.rules_secondary).encode()).hexdigest()[:16]

    @property
    def is_open(self) -> bool:
        return self.status in ("open", "active")

    @property
    def missing_critical(self) -> list[str]:
        miss = []
        if not self.ticker:
            miss.append("ticker")
        if self.close_time is None:
            miss.append("close_time")
        if self.open_time is None:
            miss.append("open_time")
        if not self.rules_primary:
            miss.append("rules")
        return miss


def series_from_ticker(ticker: str) -> str:
    return ticker.split("-", 1)[0].upper() if ticker else ""


def parse_market(m: dict[str, Any]) -> MarketMeta:
    if not isinstance(m, dict) or not m.get("ticker"):
        raise ParseError("market payload without ticker")
    variant = "dollars" if any(k.endswith("_dollars") for k in m) else ("cents" if "yes_bid" in m else "unknown")
    strike: Decimal | None = None
    for key in ("floor_strike", "functional_strike", "cap_strike", "strike"):
        v = m.get(key)
        if v not in (None, ""):
            try:
                strike = D(v)
                break
            except MoneyError:
                continue
    tick = None
    for key in ("tick_size_dollars", "tick_size"):
        v = m.get(key)
        if v not in (None, ""):
            try:
                tick = D(v) if key.endswith("dollars") else cents_to_dollars(v)
                break
            except MoneyError:
                pass
    sv = m.get("settlement_value_dollars", m.get("settlement_value"))
    settle = None
    if sv not in (None, ""):
        try:
            settle = D(sv) if "settlement_value_dollars" in m else cents_to_dollars(sv)
        except MoneyError:
            settle = None
    return MarketMeta(
        ticker=str(m["ticker"]),
        event_ticker=str(m.get("event_ticker", "")),
        series_ticker=str(m.get("series_ticker") or series_from_ticker(str(m["ticker"]))),
        status=str(m.get("status", "unknown")).lower(),
        open_time=parse_ts(m.get("open_time")),
        close_time=parse_ts(m.get("close_time")),
        expiration_time=parse_ts(m.get("expected_expiration_time") or m.get("expiration_time")),
        strike=strike,
        strike_type=m.get("strike_type"),
        yes_bid=_price_field(m, "yes_bid"),
        yes_ask=_price_field(m, "yes_ask"),
        no_bid=_price_field(m, "no_bid"),
        no_ask=_price_field(m, "no_ask"),
        last_price=_price_field(m, "last_price"),
        volume=_count_field(m, "volume"),
        open_interest=_count_field(m, "open_interest"),
        result=(m.get("result") or None),
        title=str(m.get("title", "")),
        subtitle=str(m.get("yes_sub_title") or m.get("subtitle") or ""),
        rules_primary=str(m.get("rules_primary", "")),
        rules_secondary=str(m.get("rules_secondary", "")),
        tick_size=tick,
        settlement_value=settle,
        raw_variant=variant,
    )


Level = tuple[Decimal, Decimal]


@dataclass(frozen=True)
class BookSnapshot:
    yes_bids: list[Level]
    no_bids: list[Level]
    variant: str


def _levels(raw: Any, dollars: bool) -> list[Level]:
    out: list[Level] = []
    if not raw:
        return out
    for lvl in raw:
        if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
            raise ParseError(f"bad book level {lvl!r}")
        px = price(lvl[0]) if dollars else price(cents_to_dollars(lvl[0]))
        qty = D(lvl[1])
        if qty < 0:
            raise ParseError("negative depth")
        if qty > 0:
            out.append((px, qty))
    return out


def parse_orderbook(payload: dict[str, Any]) -> BookSnapshot:
    """Accept ``{"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}`` (current) or legacy
    ``{"orderbook": {"yes": [[cents, qty]], "no": [...]}}``. Bids only for both sides."""
    if "orderbook_fp" in payload:
        ob = payload["orderbook_fp"] or {}
        return BookSnapshot(_levels(ob.get("yes_dollars"), True), _levels(ob.get("no_dollars"), True), "fp")
    if "orderbook" in payload:
        ob = payload["orderbook"] or {}
        if "yes_dollars" in ob or "no_dollars" in ob:
            return BookSnapshot(_levels(ob.get("yes_dollars"), True), _levels(ob.get("no_dollars"), True), "dollars")
        return BookSnapshot(_levels(ob.get("yes"), False), _levels(ob.get("no"), False), "cents")
    # WS snapshot message body
    if "yes_dollars_fp" in payload or "no_dollars_fp" in payload:
        return BookSnapshot(_levels(payload.get("yes_dollars_fp"), True), _levels(payload.get("no_dollars_fp"), True), "ws_fp")
    if "yes_dollars" in payload or "no_dollars" in payload:
        return BookSnapshot(_levels(payload.get("yes_dollars"), True), _levels(payload.get("no_dollars"), True), "ws_dollars")
    if "yes" in payload or "no" in payload:
        return BookSnapshot(_levels(payload.get("yes"), False), _levels(payload.get("no"), False), "ws_cents")
    raise ParseError("unrecognized order book shape")


@dataclass(frozen=True)
class BookDelta:
    side: str  # "yes" | "no"
    price: Decimal
    delta: Decimal


def parse_delta(msg: dict[str, Any]) -> BookDelta:
    side = str(msg.get("side", "")).lower()
    if side not in ("yes", "no"):
        raise ParseError("delta side")
    if msg.get("price_dollars") not in (None, ""):
        px = price(msg["price_dollars"])
    elif msg.get("price") not in (None, ""):
        px = price(cents_to_dollars(msg["price"]))
    else:
        raise ParseError("delta price")
    dv = msg.get("delta_fp", msg.get("delta"))
    if dv in (None, ""):
        raise ParseError("delta size")
    return BookDelta(side, px, D(dv))


@dataclass(frozen=True)
class TradePrint:
    trade_id: str
    ticker: str
    ts: datetime
    yes_price: Decimal
    count: Decimal
    taker_side: str | None


def parse_trade(t: dict[str, Any]) -> TradePrint:
    yp = _price_field(t, "yes_price")
    if yp is None:
        np_ = _price_field(t, "no_price")
        if np_ is None:
            raise ParseError("trade without price")
        yp = price(Decimal("1") - np_)
    cnt = _count_field(t, "count")
    if cnt is None:
        raise ParseError("trade without count")
    ts = parse_ts(t.get("created_time") or t.get("ts"))
    if ts is None:
        raise ParseError("trade without time")
    return TradePrint(
        trade_id=str(t.get("trade_id", "")),
        ticker=str(t.get("ticker") or t.get("market_ticker") or ""),
        ts=ts,
        yes_price=yp,
        count=cnt,
        taker_side=t.get("taker_side"),
    )


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    yes_bid_close: Decimal | None
    yes_ask_close: Decimal | None


def parse_candles(payload: dict[str, Any]) -> list[Candle]:
    out: list[Candle] = []
    for c in payload.get("candlesticks", []) or []:
        p = c.get("price") or {}
        yb = c.get("yes_bid") or {}
        ya = c.get("yes_ask") or {}
        out.append(
            Candle(
                ts=parse_ts(c.get("end_period_ts")) or parse_ts(c.get("ts")),  # type: ignore[arg-type]
                open=_price_field(p, "open"),
                high=_price_field(p, "high"),
                low=_price_field(p, "low"),
                close=_price_field(p, "close"),
                volume=_count_field(c, "volume"),
                yes_bid_close=_price_field(yb, "close"),
                yes_ask_close=_price_field(ya, "close"),
            )
        )
    return out
