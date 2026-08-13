"""Exécuteur générique de folds gouvernés.

L'évaluateur reçoit une fonction de fixture ou de moteur de recherche. Il ne
connaît aucun paramètre Trend/Carry et ne choisit jamais une grille implicite.
Chaque candidat évalué sur chaque fold est enregistré, y compris les échecs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pandas as pd

from .governance import (
    ExperimentRegistry,
    ExperimentSpec,
    GovernanceError,
    TimeSeriesFold,
    TrialRegistry,
    fold_data,
    generate_time_folds,
    parameter_fingerprint,
    sha256_canonical,
)

from .search_gates import require_diagnostic_label, validate_search_ready


@dataclass(frozen=True)
class CandidateFoldResult:
    fold: int
    selected_parameters: dict[str, Any]
    selection_score: float
    evaluation_metrics: dict[str, Any]


@dataclass(frozen=True)
class GovernedWalkForwardResult:
    folds: tuple[CandidateFoldResult, ...]
    split_definitions: tuple[TimeSeriesFold, ...]
    trial_registry: TrialRegistry
    experiment_fingerprint: str

    @property
    def trials_attempted(self) -> int:
        return self.trial_registry.attempted


Evaluator = Callable[[Mapping[str, Any], pd.DataFrame, pd.DataFrame], Mapping[str, Any]]


def governed_walk_forward(
    frame: pd.DataFrame,
    candidates: Sequence[Mapping[str, Any]],
    *,
    spec: ExperimentSpec,
    train_duration: timedelta,
    evaluation_duration: timedelta,
    warmup_duration: timedelta,
    purge_duration: timedelta,
    embargo_duration: timedelta,
    evaluator: Evaluator,
    code_sha: str | None = None,
    run_mode: str = "search",
    diagnostic_label: str | None = None,
) -> GovernedWalkForwardResult:
    """Évalue un espace pré-enregistré avec sélection et essai exhaustifs.

    ``evaluator`` doit retourner au minimum ``spec.selection_metric`` pour la
    sélection dans le train et un mapping ``evaluation_metrics`` pour le fold
    OOS. La politique impose un fold économiquement flat : l'état de position
    n'est jamais transmis entre deux appels.
    """

    if run_mode == "diagnostic":
        require_diagnostic_label(diagnostic_label)
        ExperimentRegistry().register(spec)
    elif run_mode == "search":
        validate_search_ready(spec)
        raise GovernanceError("durable search adapter required; real selection is fail-closed")
    else:
        raise GovernanceError("run_mode inconnu")
    if not candidates:
        raise GovernanceError("aucun candidat pré-enregistré")
    registry = TrialRegistry(spec)
    splits = generate_time_folds(
        frame.index,
        train_duration=train_duration,
        evaluation_duration=evaluation_duration,
        warmup_duration=warmup_duration,
        purge_duration=purge_duration,
        embargo_duration=embargo_duration,
        mode=str(spec.split_policy.get("type", "expanding")),
    )
    if not splits:
        raise GovernanceError("aucun fold temporel exploitable")

    selected: list[CandidateFoldResult] = []
    for split in splits:
        context, evaluation = fold_data(frame, split)
        train_start = pd.Timestamp(split.train_start)
        train_end = pd.Timestamp(split.train_end)
        train = frame.loc[(frame.index >= train_start) & (frame.index <= train_end)].copy()
        split_fingerprint = sha256_canonical(split.to_dict())
        ranked: list[tuple[float, str, Mapping[str, Any], Mapping[str, Any]]] = []
        for parameters in candidates:
            try:
                outcome = dict(evaluator(parameters, train, evaluation))
                score = outcome.get(spec.selection_metric)
                evaluation_metrics = outcome.get("evaluation_metrics", {})
                if not isinstance(score, (int, float)) or not pd.notna(score):
                    registry.record_trial(
                        parameters,
                        status="REJECTED",
                        result={"reason": "non_finite_selection_metric"},
                        split_fingerprint=split_fingerprint,
                        code_sha=code_sha,
                    )
                    continue
                if not isinstance(evaluation_metrics, Mapping):
                    raise GovernanceError("evaluation_metrics doit être un mapping")
                registry.record_trial(
                    parameters,
                    status="COMPLETED",
                    metrics={spec.selection_metric: float(score)},
                    result={"evaluation_metrics": dict(evaluation_metrics)},
                    split_fingerprint=split_fingerprint,
                    code_sha=code_sha,
                )
                ranked.append(
                    (
                        float(score),
                        parameter_fingerprint(parameters),
                        parameters,
                        evaluation_metrics,
                    )
                )
            except Exception as exc:
                # L'échec du candidat est auditable et ne disparaît pas du registre.
                registry.record_trial(
                    parameters,
                    status="FAILED",
                    result={"error": str(exc)},
                    split_fingerprint=split_fingerprint,
                    code_sha=code_sha,
                )
        if not ranked:
            raise GovernanceError(f"aucun candidat exploitable au fold {split.fold}")
        # Tie-break déterministe par empreinte; l'ordre d'entrée ne décide pas.
        score, _, parameters, evaluation_metrics = max(ranked, key=lambda item: (item[0], item[1]))
        selected.append(
            CandidateFoldResult(
                fold=split.fold,
                selected_parameters=dict(parameters),
                selection_score=score,
                evaluation_metrics=dict(evaluation_metrics),
            )
        )

    return GovernedWalkForwardResult(
        folds=tuple(selected),
        split_definitions=tuple(splits),
        trial_registry=registry,
        experiment_fingerprint=spec.fingerprint,
    )
