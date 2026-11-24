"""Optional Claude research analyst with hard budget enforcement (spec p.19, p.45, p.93–95, p.99).

Disabled by default. It never receives keys, account identifiers, balances or order authority. Output is
untrusted: it must validate against an allowlisted JSON schema and may only propose bounded configuration
changes for allowlisted strategies; it can never place orders, change risk limits or execute code.
Budget: $10 monthly hard cap, $8 projected cutoff, $2 reserve, worst-case pre-reservation; unknown pricing
fails closed; no automatic retry on exhaustion.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from ..config import ResearchConfig

ALLOWED_PARAMS = {"scalp_target_cents": (5, 40), "scalp_stop_cents": (3, 30), "scalp_timeout_seconds": (30, 600),
                  "mc_paths": (1000, 20000)}
FORBIDDEN_KEYS = {"order", "orders", "submit", "risk", "limit", "limits", "api_key", "key", "secret", "shell", "code",
                  "withdraw", "transfer", "policy", "mode"}


class BudgetExceeded(RuntimeError):
    pass


class AnalystDisabled(RuntimeError):
    pass


@dataclass
class ClaudeBudget:
    cfg: ResearchConfig
    month: str = ""
    spent: Decimal = Decimal("0")
    reserved: Decimal = Decimal("0")
    ledger: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _roll(self, now: datetime) -> None:
        m = now.strftime("%Y-%m")
        if m != self.month:
            self.month, self.spent, self.reserved = m, Decimal("0"), Decimal("0")

    def estimate_max_cost(self, input_tokens: int, max_output_tokens: int) -> Decimal:
        if self.cfg.claude_input_usd_per_mtok is None or self.cfg.claude_output_usd_per_mtok is None:
            raise BudgetExceeded("unknown pricing — fail closed (p.94)")
        return (Decimal(input_tokens) * self.cfg.claude_input_usd_per_mtok
                + Decimal(max_output_tokens) * self.cfg.claude_output_usd_per_mtok) / Decimal(1_000_000)

    def try_reserve(self, amount: Decimal, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._roll(now)
            projected = self.spent + self.reserved + amount
            if projected > self.cfg.claude_cutoff_usd or projected > self.cfg.claude_monthly_cap_usd - self.cfg.claude_reserve_usd:
                raise BudgetExceeded(f"projected {projected} exceeds cutoff")
            self.reserved += amount
            rid = f"r{len(self.ledger) + 1}"
            self.ledger.append({"id": rid, "reserved": str(amount), "ts": now.isoformat(), "actual": None})
            return rid

    def reconcile(self, rid: str, actual: Decimal | None) -> None:
        with self._lock:
            for e in self.ledger:
                if e["id"] == rid and e["actual"] is None:
                    res = Decimal(e["reserved"])
                    self.reserved -= res
                    # unknown actual cost (timeout / provider error) is charged at the full reservation
                    self.spent += actual if actual is not None else res
                    e["actual"] = str(actual if actual is not None else res)
                    return

    def status(self) -> dict:
        return {"month": self.month, "spent": str(self.spent), "reserved": str(self.reserved),
                "cap": str(self.cfg.claude_monthly_cap_usd), "cutoff": str(self.cfg.claude_cutoff_usd),
                "enabled": self.cfg.claude_enabled}


def validate_output(text: str) -> dict:
    """Validate analyst output against the allowlisted schema. Anything else is rejected."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed JSON") from exc
    required = {"observations": list, "evidence_ids": list, "hypotheses": list, "proposed_tests": list, "caveats": list}
    if not isinstance(data, dict) or set(data) - set(required) - {"proposed_parameters"}:
        raise ValueError("unexpected keys")
    for k, t in required.items():
        if not isinstance(data.get(k), t):
            raise ValueError(f"missing/invalid {k}")
        for item in data[k]:
            if not isinstance(item, str) or len(item) > 2000:
                raise ValueError(f"invalid item in {k}")
    props = data.get("proposed_parameters", {})
    if not isinstance(props, dict):
        raise ValueError("proposed_parameters must be object")
    for k, v in props.items():
        if k.lower() in FORBIDDEN_KEYS or k not in ALLOWED_PARAMS:
            raise ValueError(f"parameter {k} not allowlisted")
        lo, hi = ALLOWED_PARAMS[k]
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"parameter {k} out of bounds")
    return data


_SECRETISH = re.compile(r"(?i)(api[_-]?key|secret|private|password|token|balance|account)")


def redact_report(report: dict) -> dict:
    """Strip anything secret-ish from a compact daily report before it could be sent."""
    out = {}
    for k, v in report.items():
        if _SECRETISH.search(k):
            continue
        out[k] = redact_report(v) if isinstance(v, dict) else v
    return out


@dataclass
class Hypothesis:
    hypothesis_id: str
    evidence_window: str
    provenance: str
    proposed_changes: dict
    model_version: str
    test_plan: str
    status: str = "proposed"  # proposed | testing | shadow | eligible | rejected | expired
    results: dict = field(default_factory=dict)
    cost_usd: str = "0"
    expires_ts: float | None = None


class Analyst:
    """No network client is constructed unless explicitly enabled with a key file and pricing."""

    def __init__(self, cfg: ResearchConfig, budget: ClaudeBudget):
        self.cfg = cfg
        self.budget = budget

    def ready(self) -> tuple[bool, str]:
        if not self.cfg.claude_enabled:
            return False, "disabled"
        if self.cfg.claude_input_usd_per_mtok is None or not self.cfg.claude_model:
            return False, "pricing_or_model_unset"
        return True, "ok"

    def prepare(self, report: dict) -> tuple[str, Decimal]:
        ok, why = self.ready()
        if not ok:
            raise AnalystDisabled(why)
        payload = json.dumps(redact_report(report), separators=(",", ":"))[: self.cfg.claude_max_input_tokens * 3]
        est_tokens = len(payload) // 3 + 400
        return payload, self.budget.estimate_max_cost(est_tokens, self.cfg.claude_max_output_tokens)
