"""PostgreSQL schema (SQLAlchemy Core) — transactional records (spec p.76, p.141).

Every user-owned table carries ``user_id`` and every query is tenant-scoped (p.137). Ledger events are
append-only; balances are materialized atomically in ``funds_state``.
"""
from __future__ import annotations

from sqlalchemy import (JSON, BigInteger, Boolean, Column, Float, ForeignKey, Index, Integer, MetaData, Numeric, String,
                        Table, Text, UniqueConstraint, create_engine, event)

metadata = MetaData()
MONEY = Numeric(18, 6)

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(64), unique=True, nullable=False),
    Column("password_hash", String(256), nullable=False),
    Column("totp_secret_enc", Text, nullable=False),
    Column("role", String(16), nullable=False, default="user"),  # owner | user | test
    Column("created_ts", Float, nullable=False),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("community_opt_in", Boolean, nullable=False, default=False),
    Column("community_alias", String(32)),
    Column("paper_start_equity", MONEY, nullable=False),
    Column("bot_manages_manual", Boolean, nullable=False, default=False),
    Column("display_tz", String(64), nullable=False, default="UTC"),
    Column("notify_prefs", JSON, nullable=False, default=dict),
)

sessions = Table(
    "sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("csrf", String(64), nullable=False),
    Column("created_ts", Float, nullable=False),
    Column("expires_ts", Float, nullable=False),
    Column("reauth_ts", Float, nullable=False),
    Column("ip", String(64)),
    Column("user_agent", String(256)),
    Column("revoked", Boolean, nullable=False, default=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False),
    Column("user_id", Integer),
    Column("action", String(64), nullable=False),
    Column("detail", JSON, nullable=False, default=dict),
    Column("ip", String(64)),
)

credentials = Table(
    "credentials", metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("kind", String(32), primary_key=True),  # kalshi_live | kalshi_demo
    Column("key_id_enc", Text, nullable=False),
    Column("private_key_enc", Text, nullable=False),
    Column("created_ts", Float, nullable=False),
)

orders = Table(
    "orders", metadata,
    Column("client_order_id", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("order_id", String(64)),
    Column("ticker", String(64), nullable=False),
    Column("side", String(4), nullable=False),
    Column("action", String(4), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("limit_price", MONEY, nullable=False),
    Column("state", String(16), nullable=False),
    Column("filled", MONEY, nullable=False),
    Column("filled_cost", MONEY, nullable=False),
    Column("fees", MONEY, nullable=False),
    Column("origin", String(8), nullable=False),
    Column("strategy", String(32), nullable=False),
    Column("paper", Boolean, nullable=False),
    Column("tif", String(32), nullable=False),
    Column("created_ts", Float, nullable=False),
    Column("updated_ts", Float, nullable=False),
    Column("reject_reason", String(128)),
    Column("model_version", String(64)),
    Column("reason", String(128)),
    Index("ix_orders_user_state", "user_id", "state"),
)

fills = Table(
    "fills", metadata,
    Column("fill_id", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("client_order_id", String(64), nullable=False),
    Column("order_id", String(64)),
    Column("ticker", String(64), nullable=False),
    Column("side", String(4), nullable=False),
    Column("action", String(4), nullable=False),
    Column("quantity", MONEY, nullable=False),
    Column("price", MONEY, nullable=False),
    Column("fee", MONEY, nullable=False),
    Column("is_maker", Boolean, nullable=False),
    Column("exchange_ts", Float, nullable=False),
    Column("receive_ts", Float, nullable=False),
    Column("paper", Boolean, nullable=False),
    Column("origin", String(8), nullable=False),
    Column("strategy", String(32), nullable=False),
    Index("ix_fills_user_ts", "user_id", "exchange_ts"),
)

ledger_events = Table(
    "ledger_events", metadata,
    Column("event_id", String(96), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("exchange_fill_id", String(64)),
    Column("market_id", String(64)),
    Column("order_id", String(64)),
    Column("event_type", String(24), nullable=False),  # fill | settle | deposit | correction | distribution
    Column("ts", Float, nullable=False),
    Column("quantity", MONEY),
    Column("price", MONEY),
    Column("fee", MONEY),
    Column("realized_pnl", MONEY),
    Column("bucket_delta", JSON),
    Column("policy_version", String(32), nullable=False),
    Column("reconciliation_status", String(16), nullable=False),
    Column("paper", Boolean, nullable=False),
    Column("strategy", String(32)),
    Column("origin", String(8)),
    Column("episode", Integer),
    Index("ix_ledger_user_ts", "user_id", "paper", "ts"),
)

funds_state = Table(
    "funds_state", metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("paper", Boolean, primary_key=True),
    Column("state", JSON, nullable=False),
    Column("updated_ts", Float, nullable=False),
)

decisions = Table(
    "decisions", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("ts", Float, nullable=False),
    Column("ticker", String(64), nullable=False),
    Column("strategy", String(32), nullable=False),
    Column("side", String(4)),
    Column("action", String(8), nullable=False),
    Column("qty", Integer),
    Column("price", MONEY),
    Column("p_point", Float),
    Column("p_conservative", Float),
    Column("ev", MONEY),
    Column("confidence", Float),
    Column("size_cap", MONEY),
    Column("reasons", JSON, nullable=False),
    Column("model_version", String(64)),
    Column("feature_hash", String(32)),
    Column("mode", String(16)),
    Column("paper", Boolean, nullable=False),
    Index("ix_decisions_user_ts", "user_id", "ts"),
)

settlements = Table(
    "settlements", metadata,
    Column("ticker", String(64), primary_key=True),
    Column("result", String(4), nullable=False),
    Column("expiration_value", MONEY),
    Column("strike", MONEY),
    Column("proxy_avg", Float),
    Column("model_p_yes", Float),
    Column("settled_ts", Float, nullable=False),
    Column("verified", Boolean, nullable=False),
)

windows = Table(
    "windows", metadata,
    Column("ticker", String(64), primary_key=True),
    Column("open_ts", Float, nullable=False),
    Column("close_ts", Float, nullable=False),
    Column("strike", MONEY),
    Column("analyzed", Boolean, nullable=False, default=False),
    Column("eligible_count", Integer, nullable=False, default=0),
    Column("candidates", Integer, nullable=False, default=0),
    Column("skip_reasons", JSON, nullable=False, default=dict),
    Column("feed_uptime", Float),
)

live_authorizations = Table(
    "live_authorizations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("ts", Float, nullable=False),
    Column("operator", String(64), nullable=False),
    Column("api_fingerprint", String(64), nullable=False),
    Column("validation_report_sha256", String(64), nullable=False),
    Column("policy_fingerprint", String(32), nullable=False),
    Column("revoked_ts", Float),
)

push_subscriptions = Table(
    "push_subscriptions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("keys", JSON, nullable=False),
    Column("created_ts", Float, nullable=False),
    UniqueConstraint("user_id", "endpoint"),
)

kv = Table(
    "kv", metadata,
    Column("key", String(128), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_ts", Float, nullable=False),
)


def make_engine(url: str):
    if url.startswith("sqlite"):
        eng = create_engine(url, future=True, connect_args={"check_same_thread": False})

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - test backend only
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng
    return create_engine(url, future=True, pool_pre_ping=True, pool_size=5, max_overflow=5)


def init_db(engine) -> None:
    metadata.create_all(engine)
