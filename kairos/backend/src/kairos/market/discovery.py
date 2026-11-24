"""Market discovery and rollover (spec p.2, p.106, p.133).

Finds the active and next windows of the allowlisted series from exchange metadata. The engine
switches context atomically when the active ticker closes; positions on the previous ticker remain keyed
by that ticker until settled and reconciled.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..kalshi.parse import MarketMeta, ParseError, parse_market
from .rules import RulesError, SettlementRule, parse_rule


@dataclass
class DiscoveryResult:
    active: MarketMeta | None
    upcoming: list[MarketMeta]
    rejected: dict[str, str] = field(default_factory=dict)


def select_windows(markets_payload: dict, series: str, now: float) -> DiscoveryResult:
    rejected: dict[str, str] = {}
    metas: list[MarketMeta] = []
    for raw in markets_payload.get("markets", []) or []:
        try:
            m = parse_market(raw)
        except ParseError as exc:
            rejected[str(raw.get("ticker", "?"))] = f"parse:{exc}"
            continue
        if m.series_ticker != series:
            rejected[m.ticker] = "series_not_allowlisted"
            continue
        miss = m.missing_critical
        if miss:
            rejected[m.ticker] = "missing:" + ",".join(miss)
            continue
        metas.append(m)
    metas.sort(key=lambda m: m.close_time)  # type: ignore[arg-type,return-value]
    active = None
    upcoming: list[MarketMeta] = []
    for m in metas:
        o, c = m.open_time.timestamp(), m.close_time.timestamp()  # type: ignore[union-attr]
        if o <= now < c and m.is_open and active is None:
            active = m
        elif o > now:
            upcoming.append(m)
    return DiscoveryResult(active, upcoming, rejected)


def eligibility(meta: MarketMeta, verified_templates: set[str]) -> tuple[SettlementRule | None, list[str]]:
    """Return the parsed rule and reason codes that make the market ineligible for new entries."""
    reasons: list[str] = []
    rule = None
    try:
        rule = parse_rule(meta)
    except RulesError as exc:
        reasons.append(f"rules_unparsed:{exc}")
    if rule is not None and rule.template not in verified_templates:
        reasons.append("rules_template_unverified")
    if meta.strike is None:
        reasons.append("strike_missing")
    if not meta.is_open:
        reasons.append(f"status_{meta.status}")
    return rule, reasons
