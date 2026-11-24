"""FastAPI application: authenticated same-origin API + WebSocket stream (spec p.49, p.115, p.121, p.134–137, p.142).

Security: Argon2id + TOTP MFA login; opaque session token in an HttpOnly Secure SameSite=Strict cookie
(stored hashed); CSRF double-submit header on every state-changing request; per-IP/user rate limits;
recent re-authentication for sensitive actions; Origin check on the WebSocket; tenant isolation on every
query; audit log. No secrets are ever returned. Service worker must not cache any /api response
(``Cache-Control: no-store``).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..community import leaderboard
from ..engine.core import Engine, dumps
from ..kalshi.parse import ParseError, parse_market, parse_orderbook
from ..market.book import OrderBook
from ..models.calibration import brier, ece, reliability
from ..money import FeeSchedule, MoneyError, price
from ..security.vault import Vault, token_hash, verify_password, verify_totp

SESSION_COOKIE = "kairos_session"
CSRF_COOKIE = "kairos_csrf"
REAUTH_WINDOW_S = 300


class RateLimiter:
    def __init__(self, limit: int, per_s: float):
        self.limit, self.per = limit, per_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str = Field(min_length=6, max_length=6)


class ReauthBody(BaseModel):
    password: str
    totp: str


class OrderBody(BaseModel):
    ticker: str = Field(min_length=3, max_length=64)
    side: str
    action: str
    quantity: int = Field(ge=1, le=100000)
    limit_price: str
    confirm: bool = False


class CommunityBody(BaseModel):
    enabled: bool
    alias: str | None = Field(default=None, max_length=32)


class PushBody(BaseModel):
    endpoint: str
    keys: dict


def create_app(engine: Engine, vault: Vault, dev: bool = False) -> FastAPI:
    app = FastAPI(title="Kairos", docs_url=None, redoc_url=None, openapi_url=None)
    store = engine.store
    s = engine.s
    login_rl = RateLimiter(5, 60)
    api_rl = RateLimiter(30, 1)
    market_cache: dict[str, tuple[float, Any]] = {}

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    def client_ip(request: Request) -> str:
        return request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()

    def current(request: Request) -> dict:
        tok = request.cookies.get(SESSION_COOKIE)
        if not tok:
            raise HTTPException(401, "not authenticated")
        sess = store.session(token_hash(tok))
        if sess is None:
            raise HTTPException(401, "session expired")
        if not api_rl.allow(f"s:{sess['token_hash']}"):
            raise HTTPException(429, "rate limited")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), sess["csrf"]):
                raise HTTPException(403, "csrf")
        user = store.user(sess["user_id"])
        if user is None or user["disabled"]:
            raise HTTPException(401, "disabled")
        return {"user": user, "session": sess}

    def require_reauth(ctx: dict) -> None:
        if time.time() - ctx["session"]["reauth_ts"] > REAUTH_WINDOW_S:
            raise HTTPException(428, "reauthentication required")

    def account(ctx: dict):
        a = engine.accounts.get(ctx["user"]["id"])
        if a is None:
            raise HTTPException(409, "account not loaded")
        return a

    # ---- auth --------------------------------------------------------------------------------------
    @app.post("/api/auth/login")
    async def login(body: LoginBody, request: Request, response: Response):
        ip = client_ip(request)
        if not login_rl.allow(f"ip:{ip}") or not login_rl.allow(f"u:{body.username.lower()}"):
            raise HTTPException(429, "too many attempts")
        u = store.user_by_name(body.username)
        ok = u is not None and not u["disabled"] and verify_password(u["password_hash"], body.password)
        if ok:
            totp_secret = vault.decrypt(u["totp_secret_enc"], aad=f"totp:{u['id']}".encode()).decode()
            ok = verify_totp(totp_secret, body.totp)
        if not ok:
            store.audit(u["id"] if u else None, "login_failed", {"username": body.username[:64]}, ip)
            raise HTTPException(401, "invalid credentials")
        tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        store.create_session(token_hash(tok), u["id"], csrf, s.session_ttl_minutes * 60, ip, request.headers.get("user-agent", ""))
        store.audit(u["id"], "login", {}, ip)
        secure = not dev
        response.set_cookie(SESSION_COOKIE, tok, httponly=True, secure=secure, samesite="strict", max_age=s.session_ttl_minutes * 60, path="/")
        response.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=secure, samesite="strict", max_age=s.session_ttl_minutes * 60, path="/")
        return {"ok": True, "csrf": csrf, "user": {"id": u["id"], "username": u["username"]}}

    @app.post("/api/auth/logout")
    async def logout(response: Response, ctx: dict = Depends(current)):
        store.revoke_session(ctx["session"]["token_hash"])
        store.audit(ctx["user"]["id"], "logout")
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/reauth")
    async def reauth(body: ReauthBody, request: Request, ctx: dict = Depends(current)):
        u = ctx["user"]
        if not login_rl.allow(f"reauth:{u['id']}"):
            raise HTTPException(429, "too many attempts")
        secret = vault.decrypt(u["totp_secret_enc"], aad=f"totp:{u['id']}".encode()).decode()
        if not (verify_password(u["password_hash"], body.password) and verify_totp(secret, body.totp)):
            store.audit(u["id"], "reauth_failed", {}, client_ip(request))
            raise HTTPException(401, "invalid credentials")
        store.touch_reauth(ctx["session"]["token_hash"])
        store.audit(u["id"], "reauth", {}, client_ip(request))
        return {"ok": True, "valid_for_s": REAUTH_WINDOW_S}

    @app.get("/api/auth/me")
    async def me(ctx: dict = Depends(current)):
        u = ctx["user"]
        return {"id": u["id"], "username": u["username"], "role": u["role"], "csrf": ctx["session"]["csrf"],
                "community_opt_in": u["community_opt_in"], "community_alias": u["community_alias"]}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "ts": time.time()}

    # ---- snapshot + stream ---------------------------------------------------------------------------
    def full_snapshot(uid: int) -> dict:
        v = engine.market.view()
        a = engine.accounts.get(uid)
        return {"market": engine.market_wire(v), "account": a.snapshot(v) if a else None, "chart": engine.market.chart_payload(),
                "system": system_wire(), "server_time": time.time()}

    def system_wire() -> dict:
        return {"policy_version": s.policy_version, "mode_setting": s.mode, "settings_fingerprint": s.fingerprint(),
                "uptime_s": time.time() - engine.started, "latency": engine.market.latency.summary(),
                "feeds": engine.market.feed_status, "claude": {"enabled": s.research.claude_enabled, "cap_usd": str(s.research.claude_monthly_cap_usd)},
                "events": list(engine.market.events)[-20:], "fixture_label": engine.fixture_label}

    @app.get("/api/snapshot")
    async def snapshot(ctx: dict = Depends(current)):
        return Response(dumps(full_snapshot(ctx["user"]["id"])), media_type="application/json")

    @app.websocket("/api/stream")
    async def stream(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        allowed = {s.public_origin} | ({"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"} if dev else set())
        tok = ws.cookies.get(SESSION_COOKIE)
        sess = store.session(token_hash(tok)) if tok else None
        if origin not in allowed or sess is None:
            await ws.close(code=4401)
            return
        uid = sess["user_id"]
        await ws.accept()
        q = engine.hub.connect(uid)
        seq = 0
        try:
            await ws.send_text(dumps({"seq": seq, "kind": "snapshot", "data": full_snapshot(uid)}))
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    msg = {"kind": "heartbeat", "data": {"server_time": time.time()}}
                if store.session(sess["token_hash"]) is None:
                    await ws.close(code=4401)  # session revoked/expired mid-stream
                    return
                seq += 1
                if msg["kind"] == "resync_required":
                    await ws.send_text(dumps({"seq": seq, "kind": "snapshot", "data": full_snapshot(uid)}))
                else:
                    await ws.send_text(dumps({"seq": seq, **msg}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            engine.hub.disconnect(uid, q)

    # ---- controls (p.16, p.115) ----------------------------------------------------------------------
    @app.post("/api/controls/pause")
    async def pause(ctx: dict = Depends(current)):
        a = account(ctx)
        a.pause_entries("operator")
        store.audit(ctx["user"]["id"], "pause_entries")
        return {"ok": True, "controls": a.controls.__dict__, "note": "Exit management continues."}

    @app.post("/api/controls/resume")
    async def resume(ctx: dict = Depends(current)):
        a = account(ctx)
        ok = a.resume_entries()
        store.audit(ctx["user"]["id"], "resume_entries", {"ok": ok})
        if not ok:
            raise HTTPException(409, "cannot resume: account not reconciled or orders in unknown state")
        return {"ok": True}

    @app.post("/api/controls/cancel-entries")
    async def cancel_entries(ctx: dict = Depends(current)):
        n = account(ctx).cancel_pending_entries()
        store.audit(ctx["user"]["id"], "cancel_entries", {"count": n})
        return {"ok": True, "canceled": n}

    @app.post("/api/controls/flatten")
    async def flatten(body: dict, ctx: dict = Depends(current)):
        require_reauth(ctx)
        if body.get("confirm") != "FLATTEN":
            raise HTTPException(400, "type FLATTEN to confirm")
        res = await account(ctx).emergency_flatten(engine.market.view())
        store.audit(ctx["user"]["id"], "emergency_flatten", {"result": res})
        return {"ok": True, "results": res}

    @app.post("/api/controls/acknowledge")
    async def acknowledge(body: dict, ctx: dict = Depends(current)):
        require_reauth(ctx)
        a = account(ctx)
        code = str(body.get("code", ""))
        ok = a.halts.acknowledge(code, time.time(), ctx["user"]["username"], cause_cleared=bool(body.get("cause_cleared")),
                                 reconciled=a.reconciled)
        store.audit(ctx["user"]["id"], "acknowledge_halt", {"code": code, "ok": ok})
        return {"ok": ok}

    # ---- trade (manual orders, p.135) ----------------------------------------------------------------
    async def cached(key: str, ttl: float, fn):
        hit = market_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = await fn()
        market_cache[key] = (time.time(), val)
        return val

    @app.get("/api/trade/markets")
    async def trade_markets(series: str | None = None, cursor: str | None = None, ctx: dict = Depends(current)):
        if series and (len(series) > 32 or not series.replace("-", "").isalnum()):
            raise HTTPException(400, "series")
        data = await cached(f"m:{series}:{cursor}", 5.0, lambda: engine.market.rest.markets(series_ticker=series, status="open", limit=100, cursor=cursor))
        out = []
        for raw in data.get("markets", []) or []:
            try:
                m = parse_market(raw)
            except ParseError:
                continue
            out.append({"ticker": m.ticker, "title": m.title, "subtitle": m.subtitle, "close_ts": m.close_time.timestamp() if m.close_time else None,
                        "yes_bid": _s(m.yes_bid), "yes_ask": _s(m.yes_ask), "volume": _s(m.volume), "series": m.series_ticker,
                        "model_supported": m.series_ticker in s.risk.allowed_series})
        return {"markets": out, "cursor": data.get("cursor")}

    async def book_for(ticker: str) -> tuple[OrderBook | None, Any]:
        if engine.market.active and ticker == engine.market.active.ticker:
            return engine.market.books.get(ticker), engine.market.active
        data = await cached(f"b:{ticker}", 1.0, lambda: engine.market.rest.orderbook(ticker))
        meta = await cached(f"mm:{ticker}", 10.0, lambda: engine.market.rest.market(ticker))
        b = OrderBook(ticker)
        try:
            b.apply_snapshot(parse_orderbook(data), None, time.time())
            m = parse_market(meta.get("market", meta))
        except (ParseError, AttributeError):
            return None, None
        return b, m

    @app.get("/api/trade/book/{ticker}")
    async def trade_book(ticker: str, ctx: dict = Depends(current)):
        b, m = await book_for(ticker)
        if b is None:
            raise HTTPException(404, "book unavailable")
        return {"book": b.to_wire(15), "market": {"ticker": m.ticker, "title": m.title, "subtitle": m.subtitle, "rules": m.rules_primary,
                                                    "close_ts": m.close_time.timestamp() if m.close_time else None, "status": m.status,
                                                    "model_supported": m.series_ticker in s.risk.allowed_series}}

    def _parse_order(body: OrderBody) -> tuple[str, str, Decimal]:
        if body.side not in ("yes", "no") or body.action not in ("buy", "sell"):
            raise HTTPException(400, "side/action")
        try:
            lim = price(body.limit_price)
        except (MoneyError, InvalidOperation):
            raise HTTPException(400, "limit price")
        if not (Decimal("0.01") <= lim <= Decimal("0.99")):
            raise HTTPException(400, "limit price out of range")
        return body.side, body.action, lim

    @app.post("/api/trade/preview")
    async def preview(body: OrderBody, ctx: dict = Depends(current)):
        side, action, lim = _parse_order(body)
        b, m = await book_for(body.ticker)
        if b is None or not b.valid:
            raise HTTPException(409, "book unavailable or stale")
        fees = engine.market.fees if m and m.series_ticker == engine.market.series else FeeSchedule("quadratic", source="general_schedule_estimate")
        q = Decimal(body.quantity)
        f = b.buy(side, q, lim) if action == "buy" else b.sell(side, q, lim)
        est_fee = fees.fee(f.quantity, f.vwap) if f.vwap is not None and f.quantity > 0 else Decimal("0")
        if action == "buy":
            max_loss = f.cost + est_fee
            payout = f.quantity
            res = {"fillable_now": str(f.quantity), "vwap": _s(f.vwap), "est_fee": str(est_fee), "max_cost": str(max_loss),
                   "max_loss": str(max_loss), "payout_if_win": str(payout), "net_if_win": str(payout - max_loss),
                   "unfilled_rests": False, "note": "IOC limit: any quantity not immediately fillable at ≤ limit is canceled."}
        else:
            res = {"fillable_now": str(f.quantity), "vwap": _s(f.vwap), "est_fee": str(est_fee), "proceeds": str(f.cost - est_fee)}
        res["fee_schedule"] = {"type": fees.fee_type, "source": fees.source}
        return res

    @app.post("/api/trade/order")
    async def place(body: OrderBody, ctx: dict = Depends(current)):
        if not body.confirm:
            raise HTTPException(400, "explicit confirmation required")
        side, action, lim = _parse_order(body)
        b, m = await book_for(body.ticker)
        a = account(ctx)
        fees = engine.market.fees if m and m.series_ticker == engine.market.series else FeeSchedule("quadratic", source="general_schedule_estimate")
        v = engine.market.view()
        return await a.manual_order(v, b, body.ticker, side, action, body.quantity, lim, fees)

    @app.get("/api/orders")
    async def orders(ctx: dict = Depends(current)):
        a = account(ctx)
        return {"orders": store.orders_for(ctx["user"]["id"], a.paper, 200)}

    @app.get("/api/fills")
    async def fills(ctx: dict = Depends(current)):
        a = account(ctx)
        return {"fills": store.fills_for(ctx["user"]["id"], a.paper, 0, 500)[-500:]}

    @app.get("/api/decisions")
    async def decisions(kind: str | None = None, ctx: dict = Depends(current)):
        a = account(ctx)
        acts = ("skip",) if kind == "skipped" else (("enter", "exit") if kind == "trades" else None)
        return {"decisions": store.decisions_for(ctx["user"]["id"], a.paper, 200, acts)}

    # ---- research (p.120) -------------------------------------------------------------------------------
    @app.get("/api/research")
    async def research(ctx: dict = Depends(current)):
        a = account(ctx)
        uid = ctx["user"]["id"]
        sets = [x for x in store.settlements_recent(2000) if x.get("model_p_yes") is not None]
        cal = None
        if sets:
            p = np.array([x["model_p_yes"] for x in sets])
            y = np.array([1 if x["result"] == "yes" else 0 for x in sets])
            cal = {"n": int(len(p)), "brier": brier(p, y), "ece": ece(p, y) if len(p) >= 10 else None, "bins": reliability(p, y)}
        day = time.time() - 86400
        return {"calibration": cal, "settlements": len(store.settlements_recent(5000)), "registry": {k: r.to_wire() for k, r in a.registry.records.items()},
                "skip_reasons_24h": store.skip_reason_counts(uid, a.paper, day), "coverage_24h": store.coverage(day),
                "markouts": a.markouts.summary(), "latency": engine.market.latency.summary(),
                "claude": {"enabled": s.research.claude_enabled, "cap_usd": str(s.research.claude_monthly_cap_usd),
                           "cutoff_usd": str(s.research.claude_cutoff_usd), "spent_usd": "0.00"},
                "replay": {"status": "no recorded history yet" if not sets else "available via CLI: kairos replay", "fidelity": "recorded REST/WS events"},
                "basis": {"samples": len(engine.market.basis.samples), "sd": engine.market.basis.sd},
                "disclaimer": "Paper/simulated results are not realized earnings."}

    # ---- community (p.136) ---------------------------------------------------------------------------
    @app.get("/api/community/leaderboard")
    async def community(period: str = "today", mode: str = "paper", ctx: dict = Depends(current)):
        if period not in leaderboard.PERIODS or mode not in ("paper", "live"):
            raise HTTPException(400, "period/mode")
        return leaderboard.build(store.users(), store.ledger_for, paper=(mode == "paper"), period=period)

    @app.post("/api/community/opt-in")
    async def opt_in(body: CommunityBody, ctx: dict = Depends(current)):
        uid = ctx["user"]["id"]
        if body.enabled:
            alias = (body.alias or "").strip()
            if not (3 <= len(alias) <= 32) or not alias.replace("_", "").replace("-", "").isalnum():
                raise HTTPException(400, "alias must be 3–32 letters, digits, - or _")
            if alias.lower() == ctx["user"]["username"].lower():
                raise HTTPException(400, "alias must not be your username")
            store.update_user(uid, community_opt_in=True, community_alias=alias)
        else:
            store.update_user(uid, community_opt_in=False)
        store.audit(uid, "community_opt_in", {"enabled": body.enabled})
        return {"ok": True}

    # ---- settings (p.121) ------------------------------------------------------------------------------
    @app.get("/api/settings")
    async def settings_view(ctx: dict = Depends(current)):
        a = account(ctx)
        r = s.risk
        auth = store.live_authorization(ctx["user"]["id"])
        return {"mode": "paper" if a.paper else "live", "server_mode": s.mode, "live_authorized": auth is not None,
                "live_authorization": {"ts": auth["ts"], "operator": auth["operator"]} if auth else None,
                "immutable_limits": {"max_entry_fraction": str(r.max_entry_fraction), "max_market_exposure_fraction": str(r.max_market_exposure_fraction),
                                     "max_portfolio_exposure_fraction": str(r.max_portfolio_exposure_fraction),
                                     "daily_loss_fraction": str(r.daily_loss_fraction), "max_drawdown_fraction": str(r.max_drawdown_fraction),
                                     "max_concurrent_positions": r.max_concurrent_positions, "entry_cutoff_seconds": r.entry_cutoff_seconds,
                                     "aggressive_enabled": r.aggressive_enabled, "allowed_series": list(r.allowed_series),
                                     "mode_ceilings": {k: str(v) for k, v in r.mode_ceilings.__dict__.items()}, "fingerprint": r.fingerprint()},
                "strategies": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in s.strategies.__dict__.items()},
                "bot_manages_manual": a.bot_manages_manual, "notifications": ctx["user"]["notify_prefs"],
                "audit": store.audit_tail(ctx["user"]["id"], 20), "push_public_key": store.kv_get("vapid_public_key")}

    @app.post("/api/settings/bot-manual")
    async def bot_manual(body: dict, ctx: dict = Depends(current)):
        require_reauth(ctx)
        val = bool(body.get("enabled"))
        store.update_user(ctx["user"]["id"], bot_manages_manual=val)
        account(ctx).bot_manages_manual = val
        store.audit(ctx["user"]["id"], "bot_manages_manual", {"enabled": val})
        return {"ok": True}

    @app.post("/api/settings/notifications")
    async def notif(body: dict, ctx: dict = Depends(current)):
        allowed = {"halts", "fills", "mode_changes", "profit_allocations", "settlements"}
        prefs = {k: bool(v) for k, v in body.items() if k in allowed}
        store.update_user(ctx["user"]["id"], notify_prefs=prefs)
        return {"ok": True, "prefs": prefs}

    @app.post("/api/push/subscribe")
    async def push_sub(body: PushBody, ctx: dict = Depends(current)):
        if not body.endpoint.startswith("https://"):
            raise HTTPException(400, "endpoint")
        store.add_push(ctx["user"]["id"], body.endpoint, {k: str(v)[:256] for k, v in body.keys.items()})
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})

    return app


def _s(x) -> str | None:
    return None if x is None else str(x)
