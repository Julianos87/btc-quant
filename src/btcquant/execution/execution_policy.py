"""Politique d'exécution testable avant son activation sur le broker.

Elle ne modifie pas les signaux de trading. Elle décide seulement si un
ajustement doit être regroupé, différé, envoyé en post-only ou exécuté au
marché. Les sorties urgentes restent toujours prioritaires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExecutionAction = Literal[
    "HOLD",
    "WAIT_SPREAD",
    "WAIT_FUNDING",
    "POST_ONLY",
    "MARKET",
]


@dataclass(frozen=True)
class ExecutionSnapshot:
    bid: float
    ask: float
    funding_rate_8h: float = 0.0
    seconds_to_funding: float | None = None

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("Carnet invalide : bid/ask doivent être positifs et ask >= bid")
        if self.seconds_to_funding is not None and self.seconds_to_funding < 0:
            raise ValueError("seconds_to_funding doit être positif")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000.0


@dataclass(frozen=True)
class ExecutionDecision:
    action: ExecutionAction
    reason: str
    limit_price: float | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ExecutionPolicy:
    max_entry_spread_bps: float = 2.0
    min_adjustment_notional: float = 50.0
    maker_timeout_seconds: float = 30.0
    funding_avoidance_window_seconds: float = 300.0
    adverse_funding_threshold_8h: float = 0.0001

    def decide(
        self,
        *,
        side: Literal["BUY", "SELL"],
        notional: float,
        snapshot: ExecutionSnapshot,
        urgent: bool = False,
    ) -> ExecutionDecision:
        normalized_side = side.upper()
        if normalized_side not in ("BUY", "SELL"):
            raise ValueError(f"Côté invalide : {side!r}")
        if notional < 0:
            raise ValueError("Le notionnel doit être positif")
        if urgent:
            return ExecutionDecision("MARKET", "sortie ou réduction urgente")
        if notional < self.min_adjustment_notional:
            return ExecutionDecision("HOLD", "ajustement inférieur au seuil minimal")
        if snapshot.spread_bps > self.max_entry_spread_bps:
            return ExecutionDecision("WAIT_SPREAD", "spread d'entrée excessif")
        if self._would_pay_imminent_funding(normalized_side, snapshot):
            return ExecutionDecision("WAIT_FUNDING", "funding défavorable imminent")
        limit_price = snapshot.bid if normalized_side == "BUY" else snapshot.ask
        return ExecutionDecision(
            "POST_ONLY",
            "entrée non urgente au meilleur prix passif",
            limit_price=limit_price,
            timeout_seconds=self.maker_timeout_seconds,
        )

    def _would_pay_imminent_funding(
        self,
        side: str,
        snapshot: ExecutionSnapshot,
    ) -> bool:
        seconds = snapshot.seconds_to_funding
        if seconds is None or seconds > self.funding_avoidance_window_seconds:
            return False
        threshold = self.adverse_funding_threshold_8h
        long_would_pay = side == "BUY" and snapshot.funding_rate_8h >= threshold
        short_would_pay = side == "SELL" and snapshot.funding_rate_8h <= -threshold
        return long_would_pay or short_would_pay


@dataclass
class RebalanceBuffer:
    """Regroupe des changements signés et laisse les mouvements opposés se compenser."""

    min_notional: float
    pending_notional: float = 0.0

    def add(self, delta_notional: float) -> float | None:
        self.pending_notional += delta_notional
        if abs(self.pending_notional) < self.min_notional:
            return None
        released = self.pending_notional
        self.pending_notional = 0.0
        return released


@dataclass(frozen=True)
class ExecutionQualificationPolicy:
    """Seuil minimal avant de remplacer les hypothèses de coûts du backtest."""

    min_observation_days: float = 30.0
    min_eligible_intents: int = 50
    min_post_only_fill_rate: float = 0.70
    max_fallback_rate: float = 0.30
    max_p95_fill_seconds: float = 30.0
    max_mean_all_in_cost_bps: float = 7.5
    max_p95_slippage_bps: float = 5.0


@dataclass(frozen=True)
class ExecutionEvidence:
    observation_days: float
    eligible_intents: int
    post_only_fills: int
    fallback_orders: int
    p95_fill_seconds: float | None
    mean_all_in_cost_bps: float | None
    p95_slippage_bps: float | None


def evaluate_execution_evidence(
    evidence: ExecutionEvidence,
    policy: ExecutionQualificationPolicy | None = None,
) -> dict:
    """Évalue une campagne shadow/testnet sans traiter une absence comme zéro parfait."""

    target = policy or ExecutionQualificationPolicy()
    if evidence.eligible_intents > 0:
        fill_rate = evidence.post_only_fills / evidence.eligible_intents
        fallback_rate = evidence.fallback_orders / evidence.eligible_intents
    else:
        fill_rate = None
        fallback_rate = None
    checks = {
        "observation_days": evidence.observation_days >= target.min_observation_days,
        "eligible_intents": evidence.eligible_intents >= target.min_eligible_intents,
        "post_only_fill_rate": (
            fill_rate is not None and fill_rate >= target.min_post_only_fill_rate
        ),
        "fallback_rate": (fallback_rate is not None and fallback_rate <= target.max_fallback_rate),
        "p95_fill_seconds": (
            evidence.p95_fill_seconds is not None
            and evidence.p95_fill_seconds <= target.max_p95_fill_seconds
        ),
        "mean_all_in_cost_bps": (
            evidence.mean_all_in_cost_bps is not None
            and evidence.mean_all_in_cost_bps <= target.max_mean_all_in_cost_bps
        ),
        "p95_slippage_bps": (
            evidence.p95_slippage_bps is not None
            and evidence.p95_slippage_bps <= target.max_p95_slippage_bps
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "post_only_fill_rate": fill_rate,
        "fallback_rate": fallback_rate,
    }
