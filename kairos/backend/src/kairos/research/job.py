"""Offline shadow-learning job (spec p.9, p.42–45, p.58, p.83). Runs in ``kairos-research.service`` at low
priority (Nice=15, CPUQuota), never inside the trading process, never with exchange keys.

1. Load one-second feature rows (Parquet, written by the engine) and official settlements (Postgres).
2. Label each row with the official binary outcome of its window; unresolved windows are excluded.
3. Chronological, market-grouped split with purge/embargo; leakage assertions.
4. Train logistic regression and LightGBM challengers; compare with the frozen Monte Carlo champion
   on the untouched test block (Brier, log loss, ECE).
5. Register signed artifacts as *shadow*. Promotion requires the pre-registered gates of
   ``learning.evaluate_candidate`` including trading P&L evidence; this job never promotes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import numpy as np

from ..models.calibration import brier, ece, log_loss
from .learning import ModelCard, ModelRegistry, schema_hash, train_lightgbm, train_logistic
from .stats import assert_no_leakage, chronological_split

log = logging.getLogger("kairos.research")
FEATURES = ["seconds_left", "z_to_strike", "sigma", "ret_60s", "ret_300s", "flow_imb_15s", "k_spread", "k_depth_imb", "eth_corr", "sol_corr"]
MIN_WINDOWS = 200  # below this a challenger is not even trained (p.9: "Four hourly outcomes are not enough")


def load_rows(feature_dir: Path) -> list[dict]:
    import pyarrow.parquet as pq

    rows: list[dict] = []
    for f in sorted(Path(feature_dir).glob("date=*/*.parquet")):
        rows.extend(pq.ParquetFile(f).read().to_pylist())
    return rows


def build_dataset(rows: list[dict], settlements: dict[str, str]):
    X, y, t, g, p_champ, label_end = [], [], [], [], [], []
    for r in rows:
        res = settlements.get(r["ticker"])
        if res not in ("yes", "no") or r.get("p_model") is None:
            continue
        vec = [r.get(k) for k in FEATURES]
        X.append([np.nan if v is None else float(v) for v in vec])
        y.append(1 if res == "yes" else 0)
        t.append(r["ts"])
        g.append(r["ticker"])
        p_champ.append(r["p_model"])
        label_end.append(r["ts"] + max(0.0, r.get("seconds_left") or 0.0))
    return (np.array(X, float), np.array(y), np.array(t, float), np.array(g), np.array(p_champ, float), np.array(label_end, float))


def run(feature_dir: Path, settlements: dict[str, str], registry: ModelRegistry) -> dict:
    rows = load_rows(feature_dir)
    X, y, t, g, pc, le = build_dataset(rows, settlements)
    n_windows = len(set(g.tolist()))
    report: dict = {"ts": time.time(), "rows": int(len(y)), "windows": n_windows, "trained": []}
    if n_windows < MIN_WINDOWS:
        report["status"] = f"insufficient labels: {n_windows} settled windows < {MIN_WINDOWS}"
        return report
    sp = chronological_split(t, g, le)
    assert_no_leakage(t[sp.test], t[sp.test], g[sp.train], g[sp.test])
    med = np.nanmedian(X[sp.train], axis=0)
    Xf = np.where(np.isnan(X), med, X)  # imputation fitted on training partition only
    champ = {"brier": brier(pc[sp.test], y[sp.test]), "log_loss": log_loss(pc[sp.test], y[sp.test]), "ece": ece(pc[sp.test], y[sp.test])}
    report["champion_test"] = champ
    data_hash = hashlib.sha256(Xf.tobytes() + y.tobytes()).hexdigest()[:16]
    commit = os.environ.get("KAIROS_COMMIT") or _git_commit()
    for kind, trainer in (("logistic", train_logistic), ("lightgbm", train_lightgbm)):
        m = trainer(Xf[sp.train], y[sp.train])
        p = m.predict_proba(Xf[sp.test])[:, 1]
        metrics = {"brier": brier(p, y[sp.test]), "log_loss": log_loss(p, y[sp.test]), "ece": ece(p, y[sp.test]),
                   "n_test": int(len(sp.test)), "beats_champion_brier": brier(p, y[sp.test]) < champ["brier"],
                   "gates_passed": False, "note": "probability-only evaluation; promotion requires trading evidence (p.44)"}
        card = ModelCard(f"{kind}-{int(time.time())}", kind, "settlement", "settle", FEATURES, schema_hash(FEATURES), data_hash, commit,
                         {"imputation_medians": med.tolist()}, time.time(), metrics)
        registry.register(m, card)
        report["trained"].append({"model_id": card.model_id, **metrics})
    report["status"] = "shadow models registered"
    return report


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def main() -> None:
    from ..config import load_settings
    from ..db.schema import make_engine
    from ..db.store import Store

    logging.basicConfig(level="INFO")
    os.nice(10) if hasattr(os, "nice") else None
    s = load_settings()
    store = Store(make_engine(s.database_url))
    sets = {r["ticker"]: r["result"] for r in store.settlements_recent(100000)}
    key = os.environ.get("KAIROS_REGISTRY_KEY", "").encode() or hashlib.sha256(b"kairos-registry-" + s.fingerprint().encode()).digest()
    reg = ModelRegistry(Path(s.data_dir) / "models", key)
    rep = run(Path(s.data_dir) / "features", sets, reg)
    out = Path(s.data_dir) / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"research-{int(time.time())}.json").write_text(json.dumps(rep, indent=1, default=str))
    store.kv_set("last_research_report", rep)
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
