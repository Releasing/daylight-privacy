"""Champion–challenger shadow learning, model registry and promotion gates (spec p.9, p.42–45, p.58,
p.94–96, p.109).

Runs in the separate low-priority research process. Models are CPU-only, bounded (feature count, depth).
Artifacts are immutable, hashed and signed with an HMAC key held by the registry; production loads only
approved, signature-valid, schema-compatible artifacts. Promotion is evidence-gated and never automatic
after a winning streak; rollback is explicit and atomic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pickle
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..models.calibration import brier, ece, log_loss
from .stats import block_bootstrap_mean, max_drawdown


class RegistryError(RuntimeError):
    pass


def schema_hash(names: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(names)).encode()).hexdigest()[:16]


def train_logistic(X: np.ndarray, y: np.ndarray, C: float = 1.0):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import RobustScaler

    m = make_pipeline(RobustScaler(), LogisticRegression(C=C, max_iter=500))
    m.fit(X, y)
    return m


def train_lightgbm(X: np.ndarray, y: np.ndarray, seed: int = 7):
    import lightgbm as lgb

    m = lgb.LGBMClassifier(n_estimators=200, max_depth=4, num_leaves=15, learning_rate=0.05, subsample=0.8,
                           colsample_bytree=0.8, min_child_samples=50, n_jobs=1, random_state=seed, verbose=-1)
    m.fit(X, y)
    return m


@dataclass
class ModelCard:
    model_id: str
    kind: str
    target: str
    horizon: str
    feature_names: list[str]
    feature_schema: str
    data_hash: str
    code_commit: str
    hyperparameters: dict
    created_ts: float
    metrics: dict = field(default_factory=dict)
    status: str = "shadow"  # shadow | approved | rejected | retired
    artifact_sha256: str = ""
    signature: str = ""


class ModelRegistry:
    def __init__(self, root: Path, signing_key: bytes, keep: int = 5):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.key = signing_key
        self.keep = keep
        self.index_path = self.root / "registry.json"
        self.index = json.loads(self.index_path.read_text()) if self.index_path.exists() else {"models": {}, "active": {}, "rollback": {}}

    def _sign(self, digest: str) -> str:
        return hmac.new(self.key, digest.encode(), hashlib.sha256).hexdigest()

    def _save_index(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, indent=1, sort_keys=True))
        os.replace(tmp, self.index_path)

    def register(self, model, card: ModelCard) -> ModelCard:
        blob = pickle.dumps(model)
        digest = hashlib.sha256(blob).hexdigest()
        card.artifact_sha256, card.signature = digest, self._sign(digest)
        (self.root / f"{card.model_id}.pkl").write_bytes(blob)
        self.index["models"][card.model_id] = asdict(card)
        self._save_index()
        return card

    def load(self, model_id: str, expected_schema: str):
        card = self.index["models"].get(model_id)
        if card is None:
            raise RegistryError("unknown model")
        blob = (self.root / f"{model_id}.pkl").read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != card["artifact_sha256"] or not hmac.compare_digest(self._sign(digest), card["signature"]):
            raise RegistryError("unsigned or tampered artifact")
        if card["feature_schema"] != expected_schema:
            raise RegistryError("incompatible feature schema")
        return pickle.loads(blob)  # noqa: S301 — only signature-verified local artifacts

    def approve(self, model_id: str, target: str, operator_or_gate: str) -> None:
        card = self.index["models"][model_id]
        if card["status"] != "shadow" or not card["metrics"].get("gates_passed"):
            raise RegistryError("model has not passed promotion gates")
        prev = self.index["active"].get(target)
        self.index["rollback"][target] = prev
        self.index["active"][target] = model_id
        card["status"] = "approved"
        card["approved_by"] = operator_or_gate
        self._save_index()  # atomic switch

    def rollback(self, target: str) -> str | None:
        prev = self.index["rollback"].get(target)
        cur = self.index["active"].get(target)
        if cur:
            self.index["models"][cur]["status"] = "retired"
        self.index["active"][target] = prev
        self._save_index()
        return prev


@dataclass(frozen=True)
class PromotionGates:
    min_days: int = 30
    min_windows: int = 1000
    min_trades: int = 300
    max_ece: float = 0.05
    max_stressed_drawdown_ratio: float = 1.0  # candidate stressed DD ≤ champion stressed DD × ratio


def evaluate_candidate(p_cand: np.ndarray, p_champ: np.ndarray, y: np.ndarray, pnl_cand: np.ndarray, pnl_champ: np.ndarray,
                       blocks: np.ndarray, days: int, windows: int, stressed_pnl_cand: np.ndarray, stressed_pnl_champ: np.ndarray,
                       leakage_detected: bool, n_comparisons: int = 1, gates: PromotionGates = PromotionGates()) -> dict:
    """Pre-registered promotion evaluation (p.44, p.96). Bonferroni-adjusted α for multiple comparisons."""
    alpha = 0.05 / max(1, n_comparisons)
    mean, lcb, ucb = block_bootstrap_mean(pnl_cand, blocks, alpha=alpha * 2)
    mean_ch, _, _ = block_bootstrap_mean(pnl_champ, blocks, alpha=alpha * 2)
    reasons = []
    if leakage_detected:
        reasons.append("leakage")
    if days < gates.min_days or windows < gates.min_windows or len(pnl_cand) < gates.min_trades:
        reasons.append("insufficient_sample")
    if not (lcb > 0):
        reasons.append("lcb_not_positive")
    if not (mean > mean_ch):
        reasons.append("no_improvement_vs_champion")
    e = ece(p_cand, y)
    if not (e <= gates.max_ece):
        reasons.append("calibration")
    dd_c, dd_ch = max_drawdown(stressed_pnl_cand), max_drawdown(stressed_pnl_champ)
    if dd_c > dd_ch * gates.max_stressed_drawdown_ratio + 1e-12:
        reasons.append("stressed_drawdown_worse")
    win_rate = float(np.mean(pnl_cand > 0)) if len(pnl_cand) else 0.0
    return {"gates_passed": not reasons, "reasons": reasons, "net_mean": mean, "net_lcb": lcb, "net_ucb": ucb,
            "champion_mean": mean_ch, "brier": brier(p_cand, y), "log_loss": log_loss(p_cand, y), "ece": e,
            "stressed_dd": dd_c, "champion_stressed_dd": dd_ch, "win_rate": win_rate, "alpha": alpha,
            "evaluated_ts": time.time()}
