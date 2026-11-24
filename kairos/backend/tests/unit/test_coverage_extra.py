import logging
import time
from decimal import Decimal

import pytest

from kairos.config import RiskPolicy
from kairos.contracts import Candidate
from kairos.engine.market import settlement_mismatch
from kairos.features.crossasset import CrossAsset
from kairos.models.horizons import forecast
from kairos.risk.allocator import AccountState, RiskGate
from kairos.risk.mode import Mode
from kairos.security.vault import RedactingFilter, Vault, VaultError, hash_password, verify_password
from kairos.storage.recorder import prune_raw


def test_vault_roundtrip_and_tamper(tmp_path):
    v = Vault.from_file(tmp_path / "m.key", create=True)
    assert (tmp_path / "m.key").stat().st_mode & 0o077 == 0
    tok = v.encrypt(b"secret-pem", aad=b"pk:1")
    assert v.decrypt(tok, aad=b"pk:1") == b"secret-pem"
    with pytest.raises(VaultError):
        v.decrypt(tok, aad=b"pk:2")  # cross-tenant AAD mismatch
    assert "secret" not in repr(v)
    h = hash_password("correct horse battery")
    assert verify_password(h, "correct horse battery") and not verify_password(h, "nope")
    with pytest.raises(VaultError):
        hash_password("short")


def test_no_secret_in_logs():
    rec = logging.LogRecord("x", logging.INFO, "", 0, "headers KALSHI-ACCESS-SIGNATURE: abc123 password=hunter2 -----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----", (), None)
    RedactingFilter().filter(rec)
    out = rec.getMessage()
    assert "abc123" not in out and "hunter2" not in out and "MIIE" not in out


def test_crossasset_fallback():
    ca = CrossAsset()
    now = time.time()
    for i in range(200):
        ca.update("BTC", now - 200 + i, 100000 + i)
    f = ca.features(now)
    assert f["eth_corr"] is None and f["btc_only_fallback"] == 1.0  # auxiliary outage → BTC-only, not a block


def test_horizons_respect_remaining_time():
    hs = forecast(0.0, 1e-4, 40)
    assert [h.horizon_s for h in hs] == [1, 5, 15, 30] and all(abs(h.p_up - 0.5) < 1e-9 for h in hs)
    assert forecast(0.0, None, 100) == []


def test_prune_order_oldest_first(tmp_path):
    for d in ("2026-11-01", "2026-11-20", "2026-11-24"):
        (tmp_path / "raw" / f"date={d}").mkdir(parents=True)
    now = time.mktime((2026, 11, 24, 12, 0, 0, 0, 0, 0))
    removed = prune_raw(tmp_path, 72, now, protect={"2026-11-01"})
    assert removed == ["2026-11-20"] and (tmp_path / "raw" / "date=2026-11-01").exists()


def test_eth_market_rejected_at_execution_boundary():
    g = RiskGate(RiskPolicy())
    c = Candidate("settlement", "KXETH15M-X", "yes", "buy", 10, Decimal("0.5"), 0.7, 0.66, Decimal("0.1"), Decimal("0.5"), 90.0, {}, 0.3, 600, "v", "h")
    a = AccountState(Decimal(100), Decimal(100), reconciled=True, peak_equity=Decimal(100))
    series_ok = c.ticker.split("-")[0] in RiskPolicy().allowed_series
    assert "market_not_allowlisted" in g.hard_checks(c, a, Mode.STANDARD, [], [], series_ok, [], 600, 10.0)


def test_settlement_mismatch_rule():
    assert not settlement_mismatch(100005.0, 100000.0, "no", ">=", 15.0)  # within proxy uncertainty
    assert settlement_mismatch(100200.0, 100000.0, "no", ">=", 15.0)  # clear disagreement → disable model
    assert not settlement_mismatch(None, 100000.0, "no", ">=", 15.0)
