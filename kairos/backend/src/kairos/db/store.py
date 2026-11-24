"""Tenant-scoped persistence (spec p.15, p.76, p.97, p.108, p.137).

Every method that touches user data takes ``user_id`` and filters on it. Order and ledger writes are
synchronous and durable (never batched); decision records are buffered and flushed in small batches.
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, func, insert, select, update

from ..contracts import Decision, Fill
from ..execution.orders import Order
from . import schema as S


def _n(v: Any) -> Any:
    return str(v) if isinstance(v, Decimal) else v


class Store:
    def __init__(self, engine):
        self.engine = engine
        S.init_db(engine)
        self._decisions: list[dict] = []
        self._lock = threading.Lock()

    # ---- users / sessions / audit ------------------------------------------------------------
    def create_user(self, username: str, password_hash: str, totp_enc: str, role: str, paper_equity: Decimal) -> int:
        with self.engine.begin() as c:
            r = c.execute(insert(S.users).values(username=username, password_hash=password_hash, totp_secret_enc=totp_enc,
                                                 role=role, created_ts=time.time(), disabled=False, community_opt_in=False,
                                                 paper_start_equity=paper_equity, bot_manages_manual=False, display_tz="UTC",
                                                 notify_prefs={}))
            return int(r.inserted_primary_key[0])

    def user_by_name(self, username: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(S.users).where(S.users.c.username == username)).mappings().first()
            return dict(r) if r else None

    def user(self, user_id: int) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(S.users).where(S.users.c.id == user_id)).mappings().first()
            return dict(r) if r else None

    def users(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(select(S.users).where(S.users.c.disabled.is_(False))).mappings()]

    def update_user(self, user_id: int, **values: Any) -> None:
        allowed = {"community_opt_in", "community_alias", "bot_manages_manual", "display_tz", "notify_prefs", "disabled", "password_hash"}
        bad = set(values) - allowed
        if bad:
            raise ValueError(f"not updatable: {bad}")
        with self.engine.begin() as c:
            c.execute(update(S.users).where(S.users.c.id == user_id).values(**values))

    def create_session(self, token_hash: str, user_id: int, csrf: str, ttl_s: float, ip: str, ua: str) -> None:
        now = time.time()
        with self.engine.begin() as c:
            c.execute(insert(S.sessions).values(token_hash=token_hash, user_id=user_id, csrf=csrf, created_ts=now,
                                                expires_ts=now + ttl_s, reauth_ts=now, ip=ip[:64], user_agent=ua[:256], revoked=False))

    def session(self, token_hash: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(S.sessions).where(and_(S.sessions.c.token_hash == token_hash, S.sessions.c.revoked.is_(False),
                                                         S.sessions.c.expires_ts > time.time()))).mappings().first()
            return dict(r) if r else None

    def touch_reauth(self, token_hash: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(S.sessions).where(S.sessions.c.token_hash == token_hash).values(reauth_ts=time.time()))

    def revoke_session(self, token_hash: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(S.sessions).where(S.sessions.c.token_hash == token_hash).values(revoked=True))

    def revoke_all_sessions(self, user_id: int) -> None:
        with self.engine.begin() as c:
            c.execute(update(S.sessions).where(S.sessions.c.user_id == user_id).values(revoked=True))

    def audit(self, user_id: int | None, action: str, detail: dict | None = None, ip: str | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(S.audit_log).values(ts=time.time(), user_id=user_id, action=action, detail=detail or {}, ip=ip))

    def audit_tail(self, user_id: int, n: int = 50) -> list[dict]:
        with self.engine.connect() as c:
            q = select(S.audit_log).where(S.audit_log.c.user_id == user_id).order_by(S.audit_log.c.id.desc()).limit(n)
            return [dict(r) for r in c.execute(q).mappings()]

    # ---- credentials (encrypted blobs only) ------------------------------------------------------
    def put_credential(self, user_id: int, kind: str, key_id_enc: str, pk_enc: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(S.credentials).where(and_(S.credentials.c.user_id == user_id, S.credentials.c.kind == kind)))
            c.execute(insert(S.credentials).values(user_id=user_id, kind=kind, key_id_enc=key_id_enc, private_key_enc=pk_enc,
                                                   created_ts=time.time()))

    def credential(self, user_id: int, kind: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(S.credentials).where(and_(S.credentials.c.user_id == user_id, S.credentials.c.kind == kind))).mappings().first()
            return dict(r) if r else None

    # ---- orders / fills / ledger (durable, synchronous) --------------------------------------------
    def save_order(self, user_id: int, o: Order) -> None:
        i = o.intent
        if i.user_id != user_id:
            raise PermissionError("cross-tenant order write")
        row = dict(client_order_id=i.client_order_id, user_id=user_id, order_id=o.order_id, ticker=i.ticker, side=i.side,
                   action=i.action, quantity=i.quantity, limit_price=i.limit_price, state=o.state, filled=o.filled,
                   filled_cost=o.filled_cost, fees=o.fees, origin=i.origin, strategy=i.strategy_id, paper=i.paper,
                   tif=i.time_in_force, created_ts=o.created_ts, updated_ts=o.updated_ts, reject_reason=o.reject_reason,
                   model_version=i.model_version, reason=i.reason[:128])
        with self.engine.begin() as c:
            n = c.execute(update(S.orders).where(and_(S.orders.c.client_order_id == i.client_order_id, S.orders.c.user_id == user_id))
                          .values(**{k: v for k, v in row.items() if k not in ("client_order_id", "user_id")})).rowcount
            if n == 0:
                c.execute(insert(S.orders).values(**row))

    def save_fill(self, user_id: int, f: Fill, origin: str, strategy: str) -> bool:
        with self.engine.begin() as c:
            exists = c.execute(select(S.fills.c.fill_id).where(S.fills.c.fill_id == f.fill_id)).first()
            if exists:
                return False  # deduplicate by authoritative identifier
            c.execute(insert(S.fills).values(fill_id=f.fill_id, user_id=user_id, client_order_id=f.client_order_id, order_id=f.order_id,
                                             ticker=f.ticker, side=f.side, action=f.action, quantity=f.quantity, price=f.price, fee=f.fee,
                                             is_maker=f.is_maker, exchange_ts=f.exchange_ts, receive_ts=f.receive_ts, paper=f.paper,
                                             origin=origin, strategy=strategy))
            return True

    def append_ledger(self, user_id: int, event_id: str, event_type: str, ts: float, paper: bool, policy_version: str, **kw: Any) -> bool:
        with self.engine.begin() as c:
            if c.execute(select(S.ledger_events.c.event_id).where(S.ledger_events.c.event_id == event_id)).first():
                return False
            c.execute(insert(S.ledger_events).values(event_id=event_id, user_id=user_id, event_type=event_type, ts=ts, paper=paper,
                                                     policy_version=policy_version, reconciliation_status=kw.pop("reconciliation_status", "reconciled"),
                                                     **kw))
            return True

    def save_funds(self, user_id: int, paper: bool, state: dict) -> None:
        with self.engine.begin() as c:
            n = c.execute(update(S.funds_state).where(and_(S.funds_state.c.user_id == user_id, S.funds_state.c.paper == paper))
                          .values(state=state, updated_ts=time.time())).rowcount
            if n == 0:
                c.execute(insert(S.funds_state).values(user_id=user_id, paper=paper, state=state, updated_ts=time.time()))

    def funds(self, user_id: int, paper: bool) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(S.funds_state.c.state).where(and_(S.funds_state.c.user_id == user_id, S.funds_state.c.paper == paper))).first()
            return r[0] if r else None

    def orders_for(self, user_id: int, paper: bool, limit: int = 200, open_only: bool = False) -> list[dict]:
        q = select(S.orders).where(and_(S.orders.c.user_id == user_id, S.orders.c.paper == paper))
        if open_only:
            q = q.where(S.orders.c.state.notin_(["filled", "canceled", "rejected", "expired"]))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q.order_by(S.orders.c.created_ts.desc()).limit(limit)).mappings()]

    def fills_for(self, user_id: int, paper: bool, since: float = 0.0, limit: int = 5000) -> list[dict]:
        with self.engine.connect() as c:
            q = (select(S.fills).where(and_(S.fills.c.user_id == user_id, S.fills.c.paper == paper, S.fills.c.exchange_ts >= since))
                 .order_by(S.fills.c.exchange_ts).limit(limit))
            return [dict(r) for r in c.execute(q).mappings()]

    def ledger_for(self, user_id: int, paper: bool, since: float = 0.0) -> list[dict]:
        with self.engine.connect() as c:
            q = (select(S.ledger_events).where(and_(S.ledger_events.c.user_id == user_id, S.ledger_events.c.paper == paper,
                                                     S.ledger_events.c.ts >= since)).order_by(S.ledger_events.c.ts))
            return [dict(r) for r in c.execute(q).mappings()]

    # ---- decisions (buffered, noncritical) ------------------------------------------------------
    def add_decision(self, d: Decision) -> None:
        with self._lock:
            self._decisions.append(dict(user_id=d.user_id, ts=d.ts, ticker=d.ticker, strategy=d.strategy_id, side=d.side, action=d.action,
                                        qty=d.qty, price=d.price, p_point=d.p_point, p_conservative=d.p_conservative, ev=d.ev,
                                        confidence=d.confidence, size_cap=d.size_cap, reasons=d.reason_codes, model_version=d.model_version,
                                        feature_hash=d.feature_hash, mode=d.mode, paper=d.paper))
            n = len(self._decisions)
        if n >= 200:
            self.flush_decisions()

    def flush_decisions(self) -> None:
        with self._lock:
            rows, self._decisions = self._decisions, []
        if rows:
            with self.engine.begin() as c:
                c.execute(insert(S.decisions), rows)

    def decisions_for(self, user_id: int, paper: bool, limit: int = 100, actions: tuple[str, ...] | None = None) -> list[dict]:
        self.flush_decisions()
        q = select(S.decisions).where(and_(S.decisions.c.user_id == user_id, S.decisions.c.paper == paper))
        if actions:
            q = q.where(S.decisions.c.action.in_(actions))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q.order_by(S.decisions.c.ts.desc()).limit(limit)).mappings()]

    def skip_reason_counts(self, user_id: int, paper: bool, since: float) -> dict[str, int]:
        self.flush_decisions()
        out: dict[str, int] = {}
        with self.engine.connect() as c:
            q = select(S.decisions.c.reasons).where(and_(S.decisions.c.user_id == user_id, S.decisions.c.paper == paper,
                                                          S.decisions.c.action == "skip", S.decisions.c.ts >= since))
            for (reasons,) in c.execute(q):
                for r in reasons or []:
                    key = r.split(":", 1)[0]
                    out[key] = out.get(key, 0) + 1
        return out

    # ---- market-wide -----------------------------------------------------------------------------
    def save_settlement(self, ticker: str, result: str, ts: float, verified: bool, **kw: Any) -> None:
        with self.engine.begin() as c:
            if not c.execute(select(S.settlements.c.ticker).where(S.settlements.c.ticker == ticker)).first():
                c.execute(insert(S.settlements).values(ticker=ticker, result=result, settled_ts=ts, verified=verified, **kw))

    def settlements_recent(self, n: int = 200) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(select(S.settlements).order_by(S.settlements.c.settled_ts.desc()).limit(n)).mappings()]

    def upsert_window(self, ticker: str, open_ts: float, close_ts: float, strike: Decimal | None, **kw: Any) -> None:
        with self.engine.begin() as c:
            n = c.execute(update(S.windows).where(S.windows.c.ticker == ticker).values(**kw)).rowcount if kw else \
                (1 if c.execute(select(S.windows.c.ticker).where(S.windows.c.ticker == ticker)).first() else 0)
            if n == 0:
                c.execute(insert(S.windows).values(ticker=ticker, open_ts=open_ts, close_ts=close_ts, strike=strike, analyzed=False,
                                                   eligible_count=0, candidates=0, skip_reasons={}, **kw))

    def coverage(self, since: float) -> dict:
        with self.engine.connect() as c:
            total = c.execute(select(func.count()).select_from(S.windows).where(S.windows.c.open_ts >= since)).scalar() or 0
            analyzed = c.execute(select(func.count()).select_from(S.windows).where(and_(S.windows.c.open_ts >= since,
                                                                                        S.windows.c.analyzed.is_(True)))).scalar() or 0
        return {"discovered": int(total), "analyzed": int(analyzed)}

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.engine.connect() as c:
            r = c.execute(select(S.kv.c.value).where(S.kv.c.key == key)).first()
            return r[0] if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.engine.begin() as c:
            n = c.execute(update(S.kv).where(S.kv.c.key == key).values(value=value, updated_ts=time.time())).rowcount
            if n == 0:
                c.execute(insert(S.kv).values(key=key, value=value, updated_ts=time.time()))

    def live_authorization(self, user_id: int) -> dict | None:
        with self.engine.connect() as c:
            q = (select(S.live_authorizations).where(and_(S.live_authorizations.c.user_id == user_id,
                                                           S.live_authorizations.c.revoked_ts.is_(None)))
                 .order_by(S.live_authorizations.c.ts.desc()))
            r = c.execute(q).mappings().first()
            return dict(r) if r else None

    def add_live_authorization(self, user_id: int, operator: str, api_fp: str, report_sha: str, policy_fp: str) -> None:
        with self.engine.begin() as c:
            c.execute(insert(S.live_authorizations).values(user_id=user_id, ts=time.time(), operator=operator, api_fingerprint=api_fp,
                                                          validation_report_sha256=report_sha, policy_fingerprint=policy_fp))

    def add_push(self, user_id: int, endpoint: str, keys: dict) -> None:
        with self.engine.begin() as c:
            c.execute(delete(S.push_subscriptions).where(and_(S.push_subscriptions.c.user_id == user_id,
                                                              S.push_subscriptions.c.endpoint == endpoint)))
            c.execute(insert(S.push_subscriptions).values(user_id=user_id, endpoint=endpoint, keys=keys, created_ts=time.time()))

    def pushes(self, user_id: int) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(select(S.push_subscriptions).where(S.push_subscriptions.c.user_id == user_id)).mappings()]

    def remove_push(self, user_id: int, endpoint: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(S.push_subscriptions).where(and_(S.push_subscriptions.c.user_id == user_id,
                                                              S.push_subscriptions.c.endpoint == endpoint)))
