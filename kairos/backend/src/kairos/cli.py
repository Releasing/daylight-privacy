"""Kairos command line (administrative actions stay server-side; spec p.4, p.66, p.100, p.121).

  kairos serve                 run engine + API (systemd: kairos-web.service)
  kairos create-user NAME      create owner/test account (prints TOTP enrollment URI once)
  kairos verify-api            probe official public endpoints, record response shapes and fingerprint
  kairos accept-rules          record the verified settlement-rules template (operator reviewed)
  kairos set-kalshi-key USER   store a user's Kalshi key encrypted at rest
  kairos authorize-live USER   administrative live enablement (validation report + api fingerprint)
  kairos status                print health, coverage and authorization state
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path


def _setup():
    from .config import load_settings
    from .db.schema import make_engine
    from .db.store import Store
    from .security.vault import Vault, install_log_redaction

    logging.basicConfig(level=os.environ.get("KAIROS_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_log_redaction()
    s = load_settings()
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    store = Store(make_engine(s.database_url))
    key_file = os.environ.get("KAIROS_MASTER_KEY_FILE", str(Path(s.data_dir) / "secrets" / "master.key"))
    vault = Vault.from_file(key_file, create=True)
    return s, store, vault


def _signer_from_env():
    from .kalshi.auth import KalshiSigner

    kid, path = os.environ.get("KAIROS_KALSHI_KEY_ID"), os.environ.get("KAIROS_KALSHI_KEY_FILE")
    if kid and path and Path(path).exists():
        return KalshiSigner.from_file(kid, path)
    return None


def cmd_serve(args) -> None:
    import uvicorn

    from .api.app import create_app
    from .engine.core import Engine
    from .engine.market import MarketService
    from .execution.orders import LiveAdapter
    from .kalshi.rest import KalshiREST
    from .kalshi.ws import KalshiWS
    from .storage.recorder import Recorder

    s, store, vault = _setup()
    signer = _signer_from_env()
    rest = KalshiREST(s.kalshi.rest_base, signer, s.kalshi.read_rate_per_s, s.kalshi.write_rate_per_s)
    ws_factory = (lambda on_msg: KalshiWS(s.kalshi.ws_url, signer, on_msg)) if signer else None
    recorder = Recorder(Path(s.data_dir) / "market") if not args.no_record else None
    market = MarketService(s, store, recorder, rest, ws_factory)
    from .storage.recorder import FeatureRecorder
    market.feature_recorder = FeatureRecorder(Path(s.data_dir) / "features")
    engine = Engine(s, store, market)

    def live_factory(uid: int):
        from .kalshi.auth import KalshiSigner

        cred = store.credential(uid, "kalshi_live")
        if cred is None:
            return None
        us = KalshiSigner.from_pem(vault.decrypt(cred["key_id_enc"], f"kid:{uid}".encode()).decode(),
                                   vault.decrypt(cred["private_key_enc"], f"pk:{uid}".encode()))
        urest = KalshiREST(s.kalshi.rest_base, us, s.kalshi.read_rate_per_s, s.kalshi.write_rate_per_s)
        fp = store.kv_get("verified_api_fingerprint")

        def authorized():
            a = store.live_authorization(uid)
            if s.mode != "live":
                return False, "server_in_paper_mode"
            if a is None:
                return False, "no_live_authorization"
            if not fp or a["api_fingerprint"] != fp.get("fingerprint"):
                return False, "api_not_verified"
            if a["policy_fingerprint"] != s.risk.fingerprint():
                return False, "risk_policy_changed_since_authorization"
            return True, "ok"

        return LiveAdapter(urest, fp.get("order_path", s.kalshi.order_path) if fp else s.kalshi.order_path, authorized)

    engine.load_accounts(live_factory)
    from .api.push import Notifier
    engine.notifier = Notifier(store, Path(s.data_dir) / "secrets" / "vapid_private.pem", s.public_origin)
    app = create_app(engine, vault, dev=args.dev)

    @app.on_event("startup")
    async def _start():
        await engine.run(use_external=not args.no_external)

    @app.on_event("shutdown")
    async def _stop():
        await engine.stop()

    if args.static and Path(args.static).exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=args.static, html=True), name="static")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")


def cmd_create_user(args) -> None:
    import pyotp

    from .security.vault import hash_password, new_totp_secret

    s, store, vault = _setup()
    if store.user_by_name(args.username):
        sys.exit("user exists")
    if len(store.users()) >= s.max_users:
        sys.exit(f"user cap {s.max_users} reached (p.137: cap users until measured headroom exists)")
    pw = os.environ.get("KAIROS_NEW_PASSWORD") or getpass.getpass("Password (≥12 chars): ")
    secret = new_totp_secret()
    uid = store.create_user(args.username, hash_password(pw), "pending", args.role, Decimal(args.paper_equity))
    from sqlalchemy import update

    from .db import schema as S
    with store.engine.begin() as c:
        c.execute(update(S.users).where(S.users.c.id == uid).values(totp_secret_enc=vault.encrypt(secret.encode(), aad=f"totp:{uid}".encode())))
    store.audit(uid, "user_created", {"role": args.role})
    print(f"user id {uid} created. Add this to your authenticator app now (shown once):")
    print(pyotp.TOTP(secret).provisioning_uri(name=args.username, issuer_name="Kairos"))


def cmd_set_key(args) -> None:
    s, store, vault = _setup()
    u = store.user_by_name(args.username)
    if not u:
        sys.exit("no such user")
    pem = Path(args.key_file).read_bytes()
    from .kalshi.auth import KalshiSigner

    KalshiSigner.from_pem(args.key_id, pem)  # validate before storing
    store.put_credential(u["id"], args.kind, vault.encrypt(args.key_id.encode(), f"kid:{u['id']}".encode()),
                         vault.encrypt(pem, f"pk:{u['id']}".encode()))
    store.audit(u["id"], "credential_stored", {"kind": args.kind})
    print("stored (encrypted). You may now securely delete the plaintext key file.")


def cmd_verify_api(args) -> None:
    from .kalshi.parse import parse_market, parse_orderbook
    from .kalshi.rest import KalshiREST
    from .market.rules import parse_rule

    s, store, _ = _setup()

    async def run():
        rest = KalshiREST(s.kalshi.rest_base, _signer_from_env())
        out: dict = {"ts": time.time(), "base": s.kalshi.rest_base, "checks": {}}
        try:
            st = await rest.exchange_status()
            out["checks"]["exchange_status"] = sorted(st.keys())
            ser = await rest.series(s.kalshi.series_ticker)
            sobj = ser.get("series", ser)
            out["checks"]["series_fields"] = sorted(sobj.keys())
            out["fee"] = {"fee_type": sobj.get("fee_type"), "fee_multiplier": sobj.get("fee_multiplier")}
            mk = await rest.markets(series_ticker=s.kalshi.series_ticker, status="open", limit=5)
            raw = (mk.get("markets") or [None])[0]
            if raw:
                out["checks"]["market_fields"] = sorted(raw.keys())
                m = parse_market(raw)
                out["market_variant"] = m.raw_variant
                out["rules_primary"] = m.rules_primary
                out["strike_field_present"] = m.strike is not None
                try:
                    r = parse_rule(m)
                    out["rule"] = r.__dict__
                except Exception as exc:
                    out["rule_error"] = str(exc)
                ob = await rest.orderbook(m.ticker)
                out["checks"]["orderbook_keys"] = sorted(ob.keys())
                out["orderbook_variant"] = parse_orderbook(ob).variant
            if _signer_from_env():
                bal = await rest.balance()
                out["checks"]["balance_fields"] = sorted(bal.keys())  # values not stored
        finally:
            await rest.close()
        fp = hashlib.sha256(json.dumps(out["checks"], sort_keys=True).encode()).hexdigest()[:24]
        out["fingerprint"] = fp
        out["order_path"] = args.order_path
        d = Path(s.data_dir) / "fixtures" / "verified"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"verify-{int(time.time())}.json").write_text(json.dumps(out, indent=1, default=str))
        store.kv_set("verified_api_fingerprint", {"fingerprint": fp, "ts": out["ts"], "order_path": args.order_path})
        print(json.dumps(out, indent=1, default=str))
        print("\nReview rules_primary and rule above. If correct, run: kairos accept-rules")

    asyncio.run(run())


def cmd_accept_rules(args) -> None:
    from .kalshi.parse import parse_market
    from .kalshi.rest import KalshiREST
    from .market.rules import parse_rule

    s, store, _ = _setup()

    async def run():
        rest = KalshiREST(s.kalshi.rest_base)
        try:
            mk = await rest.markets(series_ticker=s.kalshi.series_ticker, status="open", limit=1)
        finally:
            await rest.close()
        m = parse_market(mk["markets"][0])
        r = parse_rule(m)
        print(m.rules_primary, "\n", m.rules_secondary)
        print(f"\nParsed: YES if average({r.averaging_seconds}s) {r.comparator} strike; source={r.source}; template={r.template}")
        if input("Type ACCEPT if this matches the official contract terms: ").strip() != "ACCEPT":
            sys.exit("not accepted")
        cur = set(store.kv_get("verified_rules_templates", []) or [])
        cur.add(r.template)
        store.kv_set("verified_rules_templates", sorted(cur))
        store.audit(None, "rules_template_accepted", {"template": r.template})
        print("accepted")

    asyncio.run(run())


def cmd_authorize_live(args) -> None:
    s, store, _ = _setup()
    u = store.user_by_name(args.username)
    if not u:
        sys.exit("no such user")
    fp = store.kv_get("verified_api_fingerprint")
    if not fp:
        sys.exit("run kairos verify-api first")
    if store.credential(u["id"], "kalshi_live") is None:
        sys.exit("no live Kalshi credential stored for this user")
    rep = Path(args.validation_report)
    if not rep.exists():
        sys.exit("validation report required (docs/VALIDATION_REPORT.md with paper evidence)")
    text = rep.read_text()
    if "LIVE GATE: PASS" not in text:
        sys.exit("validation report does not record 'LIVE GATE: PASS' — live stays disabled (p.17, p.44, p.65)")
    print("Live trading risks real money. No profit is guaranteed. Infrastructure and fees may exceed gains.")
    if input(f"Type AUTHORIZE {args.username} to record administrative live enablement: ").strip() != f"AUTHORIZE {args.username}":
        sys.exit("aborted")
    store.add_live_authorization(u["id"], getpass.getuser(), fp["fingerprint"], hashlib.sha256(text.encode()).hexdigest(), s.risk.fingerprint())
    store.audit(u["id"], "live_authorized", {"operator": getpass.getuser()})
    print("recorded. Live orders additionally require KAIROS_MODE=live in the server environment and a restart.")


def cmd_status(args) -> None:
    s, store, _ = _setup()
    day = time.time() - 86400
    print(json.dumps({"mode": s.mode, "policy": s.risk.fingerprint(), "users": [u["username"] for u in store.users()],
                      "coverage_24h": store.coverage(day), "api": store.kv_get("verified_api_fingerprint"),
                      "rules_templates": store.kv_get("verified_rules_templates")}, indent=1, default=str))


def cmd_vapid(args) -> None:
    from .api.push import generate_vapid

    s, store, _ = _setup()
    pub = generate_vapid(Path(s.data_dir) / "secrets")
    store.kv_set("vapid_public_key", pub)
    print("VAPID key pair created; public key stored for the PWA.")


def cmd_storage(args) -> None:
    """Apply the 60/70/80/90 % pressure policy (p.41) — run from a systemd timer."""
    from .storage.recorder import disk_status, pressure, prune_raw

    s, store, _ = _setup()
    total, free = disk_status(s.data_dir)
    act = pressure(total, free, None)
    removed = []
    if act.level in ("rotate", "prune", "pause_ml", "halt_entry"):
        hours = s.storage.raw_retention_hours if act.level == "rotate" else 24
        removed = prune_raw(Path(s.data_dir) / "market", hours, time.time())
    store.kv_set("storage_pressure", {"level": act.level, "usage": act.usage, "removed": removed, "ts": time.time()})
    print(json.dumps({"level": act.level, "usage": round(act.usage, 4), "removed_days": removed}))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="kairos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--dev", action="store_true")
    p.add_argument("--static", default="")
    p.add_argument("--no-external", action="store_true")
    p.add_argument("--no-record", action="store_true")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("create-user")
    p.add_argument("username")
    p.add_argument("--role", default="owner", choices=["owner", "user", "test"])
    p.add_argument("--paper-equity", default="100")
    p.set_defaults(fn=cmd_create_user)
    p = sub.add_parser("set-kalshi-key")
    p.add_argument("username")
    p.add_argument("--key-id", required=True)
    p.add_argument("--key-file", required=True)
    p.add_argument("--kind", default="kalshi_live", choices=["kalshi_live", "kalshi_demo"])
    p.set_defaults(fn=cmd_set_key)
    p = sub.add_parser("verify-api")
    p.add_argument("--order-path", default="/portfolio/orders")
    p.set_defaults(fn=cmd_verify_api)
    sub.add_parser("accept-rules").set_defaults(fn=cmd_accept_rules)
    p = sub.add_parser("authorize-live")
    p.add_argument("username")
    p.add_argument("--validation-report", default="docs/VALIDATION_REPORT.md")
    p.set_defaults(fn=cmd_authorize_live)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("vapid-keys").set_defaults(fn=cmd_vapid)
    sub.add_parser("research").set_defaults(fn=lambda a: __import__("kairos.research.job", fromlist=["main"]).main())
    p = sub.add_parser("storage-maintain")
    p.set_defaults(fn=cmd_storage)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
