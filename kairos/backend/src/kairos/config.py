"""Versioned, validated configuration (spec p.25, p.50, p.65, p.100).

Configuration fails closed: unknown keys, unsafe overrides or missing required values raise at
startup. The risk policy is frozen; nothing in the research or strategy layers can mutate it
(spec p.16, p.74, p.96, p.107).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

POLICY_VERSION = "11.2.0"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModeCeilings:
    """Entry ceilings as fractions of active equity (spec p.67)."""

    paused: Decimal = Decimal("0")
    defensive: Decimal = Decimal("0.01")
    conservative: Decimal = Decimal("0.02")
    standard: Decimal = Decimal("0.05")
    opportunistic: Decimal = Decimal("0.10")
    aggressive: Decimal = Decimal("0.15")


@dataclass(frozen=True)
class ConfidenceTiers:
    """Experimental confidence tiers (spec p.52). Score < observe_below → reject;
    observe band → observe/paper only; higher → cap fraction of equity."""

    reject_below: int = 50
    observe_below: int = 65
    caps: tuple[tuple[int, Decimal], ...] = (
        (65, Decimal("0.01")),
        (75, Decimal("0.02")),
        (85, Decimal("0.03")),
        (90, Decimal("0.04")),
        (95, Decimal("0.05")),
    )

    def cap_for(self, score: float) -> Decimal:
        if score < self.observe_below:
            return Decimal("0")
        cap = Decimal("0")
        for lo, c in self.caps:
            if score >= lo:
                cap = c
        return cap


@dataclass(frozen=True)
class RiskPolicy:
    """Immutable absolute risk boundaries. Changing these is an administrative release (p.66, p.96)."""

    max_entry_fraction: Decimal = Decimal("0.05")  # standard-mode per-entry ceiling (p.60, p.71)
    max_market_exposure_fraction: Decimal = Decimal("0.10")  # aggregate per market (p.60)
    max_portfolio_exposure_fraction: Decimal = Decimal("0.10")  # correlated BTC windows (p.16, p.71)
    daily_loss_fraction: Decimal = Decimal("0.02")  # p.16
    max_drawdown_fraction: Decimal = Decimal("0.10")
    max_concurrent_positions: int = 3
    max_open_orders: int = 6
    max_orders_per_market: int = 24  # churn cap (p.23)
    max_turnover_per_market_fraction: Decimal = Decimal("1.0")
    entry_cutoff_seconds: int = 20  # scalp/new-risk cutoff; settlement strategy may be validated later (p.106)
    min_contracts: int = 1
    kelly_fraction: Decimal = Decimal("0.25")
    max_quote_age_ms: int = 3000
    max_feed_age_ms: int = 5000
    max_clock_skew_ms: int = 1000
    max_rejects_before_halt: int = 5
    aggressive_enabled: bool = False  # p.67: disabled by default
    allowed_series: tuple[str, ...] = ("KXBTC15M",)
    mode_ceilings: ModeCeilings = field(default_factory=ModeCeilings)
    confidence: ConfidenceTiers = field(default_factory=ConfidenceTiers)
    mode_upgrade_dwell_seconds: int = 900
    mode_upgrade_observations: int = 3

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(_jsonable(asdict(self)), sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class StrategySwitches:
    """Paper research switches (p.25). V8 p.106 requires continuous evaluation of scaling, partial
    exits and reentry, so these are on in paper mode; each still passes every risk gate."""

    settlement_enabled: bool = True
    scalp_enabled: bool = True
    arbitrage_enabled: bool = True
    staged_entries: bool = True
    partial_exits: bool = True
    reentry: bool = True
    scalp_target_cents: int = 20
    scalp_stop_cents: int = 10
    scalp_timeout_seconds: int = 300
    min_edge_dollars: Decimal = Decimal("0.0")  # conservative EV must exceed this (p.71 validated margin)
    mc_paths: int = 4000
    mc_seed: int = 20261124


@dataclass(frozen=True)
class StorageBudget:
    """120 GB NVMe plan (p.114). Values are ceilings in decimal GB."""

    os_deps_gb: int = 15
    historical_gb: int = 40
    recent_gb: int = 25
    models_replay_gb: int = 10
    app_db_logs_gb: int = 5
    free_reserve_gb: int = 25
    raw_retention_hours: int = 72
    snapshot_retention_days: int = 30
    feature_retention_days: int = 365


@dataclass(frozen=True)
class ResearchConfig:
    research_enabled: bool = True
    research_can_trade: bool = False
    claude_enabled: bool = False
    claude_monthly_cap_usd: Decimal = Decimal("10")
    claude_cutoff_usd: Decimal = Decimal("8")
    claude_reserve_usd: Decimal = Decimal("2")
    claude_model: str = ""
    claude_input_usd_per_mtok: Decimal | None = None  # operator must enter current rate card
    claude_output_usd_per_mtok: Decimal | None = None
    claude_max_output_tokens: int = 2000
    claude_max_input_tokens: int = 20000


@dataclass(frozen=True)
class KalshiConfig:
    rest_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    demo_rest_base: str = "https://demo-api.kalshi.co/trade-api/v2"
    demo_ws_url: str = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    series_ticker: str = "KXBTC15M"
    read_rate_per_s: float = 10.0
    write_rate_per_s: float = 5.0
    rest_poll_interval_s: float = 1.0
    order_path: str = "/portfolio/orders"  # verify-api decides between legacy and V2 events path


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"
    social_sources_enabled: bool = False
    disk_budget_gb: int = 120
    reserve_gb: int = 25
    data_dir: str = "data"
    database_url: str = "sqlite:///data/kairos.db"
    public_origin: str = "https://usekairos.net"
    session_ttl_minutes: int = 720
    max_users: int = 5
    risk: RiskPolicy = field(default_factory=RiskPolicy)
    strategies: StrategySwitches = field(default_factory=StrategySwitches)
    storage: StorageBudget = field(default_factory=StorageBudget)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    policy_version: str = POLICY_VERSION

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(_jsonable(asdict(self)), sort_keys=True).encode()).hexdigest()[:16]

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, Decimal):
        return str(o)
    return o


def validate(s: Settings) -> Settings:
    """Reject unsafe configurations (p.50, p.100, p.112)."""
    errors: list[str] = []
    if s.mode not in ("paper", "live"):
        errors.append(f"mode must be paper|live, got {s.mode!r}")
    if s.social_sources_enabled:
        errors.append("social_sources_enabled must be false (p.39)")
    if s.research.research_can_trade:
        errors.append("research_can_trade must be false (p.50)")
    if s.research.claude_monthly_cap_usd > Decimal("10"):
        errors.append("claude_monthly_cap_usd may not exceed 10 (p.19)")
    if s.research.claude_cutoff_usd + s.research.claude_reserve_usd > s.research.claude_monthly_cap_usd:
        errors.append("claude cutoff + reserve exceeds cap")
    r = s.risk
    for name in ("max_entry_fraction", "max_market_exposure_fraction", "max_portfolio_exposure_fraction",
                 "daily_loss_fraction", "max_drawdown_fraction", "kelly_fraction"):
        v = getattr(r, name)
        if not (Decimal("0") < v <= Decimal("0.25")):
            errors.append(f"risk.{name}={v} outside (0, 0.25] — above 25% is stress-only (p.73)")
    if r.mode_ceilings.aggressive > Decimal("0.15"):
        errors.append("aggressive ceiling above 15% (p.67)")
    if any(not t.startswith("KXBTC") for t in r.allowed_series):
        errors.append("only BTC series may be tradable (p.101)")
    if r.min_contracts < 1:
        errors.append("min_contracts must be >= 1")
    st = s.storage
    total = st.os_deps_gb + st.historical_gb + st.recent_gb + st.models_replay_gb + st.app_db_logs_gb + st.free_reserve_gb
    if total > s.disk_budget_gb:
        errors.append(f"storage allocations {total} GB exceed disk budget {s.disk_budget_gb} GB")
    if s.mode == "live" and s.kalshi.rest_base.startswith("https://demo"):
        errors.append("live mode cannot point at demo endpoint")
    if errors:
        raise ConfigError("; ".join(errors))
    return s


_ENV_MAP = {
    "KAIROS_MODE": ("mode", str),
    "KAIROS_DATA_DIR": ("data_dir", str),
    "KAIROS_DATABASE_URL": ("database_url", str),
    "KAIROS_PUBLIC_ORIGIN": ("public_origin", str),
    "KAIROS_MAX_USERS": ("max_users", int),
}


def load_settings(path: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None) -> Settings:
    """Load ``config/kairos.json`` (optional) then environment overrides; validate."""
    env = dict(os.environ if env is None else env)
    s = Settings()
    p = path or env.get("KAIROS_CONFIG")
    if p:
        raw = json.loads(Path(p).read_text())
        s = _apply(s, raw)
    for key, (attr, typ) in _ENV_MAP.items():
        if key in env and env[key] != "":
            s = replace(s, **{attr: typ(env[key])})
    if "KAIROS_KALSHI_REST_BASE" in env:
        s = replace(s, kalshi=replace(s.kalshi, rest_base=env["KAIROS_KALSHI_REST_BASE"]))
    if "KAIROS_KALSHI_WS_URL" in env:
        s = replace(s, kalshi=replace(s.kalshi, ws_url=env["KAIROS_KALSHI_WS_URL"]))
    return validate(s)


def _apply(obj: Any, raw: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(obj)}
    updates: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in known:
            raise ConfigError(f"unknown config key {k!r} in {type(obj).__name__}")
        cur = getattr(obj, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            updates[k] = _apply(cur, v)
        elif isinstance(cur, Decimal):
            updates[k] = Decimal(str(v))
        elif isinstance(cur, tuple) and isinstance(v, list):
            updates[k] = tuple(v)
        elif isinstance(cur, bool) and not isinstance(v, bool):
            raise ConfigError(f"{k} must be boolean")
        else:
            updates[k] = v
    return replace(obj, **updates)
