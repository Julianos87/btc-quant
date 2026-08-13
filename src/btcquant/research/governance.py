"""Gouvernance déterministe des expériences quantitatives.

Ce module ne choisit aucun paramètre de stratégie. Il fournit les garde-fous
qui doivent entourer une future recherche : spécification immuable, registre
exhaustif des essais, splits temporels, rôles de données et scellement d'un
holdout. Les décisions statistiques dont les seuils ne sont pas encore
validés restent explicitement ``DECISION_REQUIRED``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, TypeVar, cast

import numpy as np
import pandas as pd


class GovernanceError(ValueError):
    """Violation d'un contrat de gouvernance ; l'expérience doit être bloquée."""


class ExperimentInvalidated(GovernanceError):
    """Une expérience existante a été mutée ou sa provenance ne correspond plus."""


class TrialBudgetExceeded(GovernanceError):
    """Un nouvel essai dépasserait le budget pré-enregistré."""


class GovernanceIncomplete(GovernanceError):
    """Une recherche réelle est bloquée par une décision non résolue."""


class HoldoutInvalidated(GovernanceError):
    """Le candidat ou le protocole ne correspond plus au holdout scellé."""


class DatasetRole(StrEnum):
    SEEN_RESEARCH_DATA = "SEEN_RESEARCH_DATA"
    SEEN_EXECUTION_PARITY_DATA = "SEEN_EXECUTION_PARITY_DATA"
    BACKTEST_OOS = "BACKTEST_OOS"
    BLIND_FORWARD_OOS = "BLIND_FORWARD_OOS"


class CandidateState(StrEnum):
    DRAFT = "DRAFT"
    REGISTERED = "REGISTERED"
    DEVELOPMENT = "DEVELOPMENT"
    FROZEN = "FROZEN"
    BLIND_OOS_PENDING = "BLIND_OOS_PENDING"
    BLIND_OOS_EVALUATED = "BLIND_OOS_EVALUATED"
    QUANT_RESEARCH_PASS = "QUANT_RESEARCH_PASS"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"


class HoldoutStatus(StrEnum):
    UNSEEN = "UNSEEN"
    PENDING = "PENDING"
    SPENT = "SPENT"
    INVALIDATED = "INVALIDATED"


DIAGNOSTIC_LABEL = "DIAGNOSTIC GOVERNANCE HARNESS ONLY — NOT STRATEGY VALIDATION"


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _utc(value: Any, field_name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise GovernanceError(f"{field_name} doit être timezone-aware en UTC")
    return timestamp.tz_convert("UTC")


def _utc_iso(value: Any, field_name: str = "timestamp") -> str:
    return _utc(value, field_name).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    """Convertit les valeurs usuelles en une forme JSON canonique."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, pd.Timestamp):
        return _utc_iso(value)
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        raise GovernanceError("Une valeur non finie ne peut pas entrer dans une empreinte")
    return value


def canonical_json(value: Any) -> str:
    """Sérialisation stable, sans NaN implicite ni dépendance à l'OS."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parameter_fingerprint(parameters: Mapping[str, Any]) -> str:
    return sha256_canonical(dict(parameters))


def cost_model_fingerprint(cost_assumptions: Mapping[str, Any]) -> str:
    return sha256_canonical(dict(cost_assumptions))


@dataclass(frozen=True)
class DatasetProvenance:
    """Identité complète d'une source utilisée par un résultat."""

    dataset_id: str
    venue: str
    network: str
    symbol: str
    role: DatasetRole
    start: str
    end: str
    rows_or_events: int
    sha256: str
    cutoff: str
    manifest: str
    already_seen: bool = False

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.venue or not self.symbol:
            raise GovernanceError("dataset_id, venue et symbol sont obligatoires")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise GovernanceError("sha256 dataset invalide")
        if self.rows_or_events < 0:
            raise GovernanceError("rows_or_events ne peut pas être négatif")
        start = _utc(self.start, "dataset.start")
        end = _utc(self.end, "dataset.end")
        if end < start:
            raise GovernanceError("la fin du dataset précède son début")
        _utc(self.cutoff, "dataset.cutoff")
        if self.role is DatasetRole.BLIND_FORWARD_OOS and self.already_seen:
            raise GovernanceError("un dataset déjà vu ne peut pas être BLIND_FORWARD_OOS")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def validate_dataset_role(
    dataset: DatasetProvenance,
    required_role: DatasetRole,
    *,
    target_venue: str | None = None,
    purpose: str | None = None,
) -> None:
    """Refuse une promotion avec une donnée déjà vue ou d'une autre venue."""

    if dataset.role is not required_role:
        raise GovernanceError(f"rôle incompatible: {dataset.role.value} != {required_role.value}")
    if required_role is DatasetRole.BLIND_FORWARD_OOS and dataset.already_seen:
        raise GovernanceError("BLIND_FORWARD_OOS exige une donnée réellement non vue")
    if target_venue is not None and dataset.venue.lower() != target_venue.lower():
        raise GovernanceError("venue incompatible avec la qualification demandée")
    if purpose == "HYPERLIQUID_FINAL_OOS_PASS":
        if dataset.venue.lower() != "hyperliquid":
            raise GovernanceError("HYPERLIQUID_FINAL_OOS_PASS exige Hyperliquid")
        if required_role is not DatasetRole.BLIND_FORWARD_OOS:
            raise GovernanceError("HYPERLIQUID_FINAL_OOS_PASS exige un rôle blind")


@dataclass(frozen=True)
class ExperimentSpec:
    """Contrat immuable à enregistrer avant le premier essai."""

    protocol_version: str
    experiment_id: str
    created_at: str
    base_git_sha: str
    strategy_family: str
    target_venue: str
    target_network: str
    dataset_ids: tuple[str, ...]
    dataset_roles: dict[str, DatasetRole]
    dataset_hashes: dict[str, str]
    data_cutoffs: dict[str, str]
    feature_policy: dict[str, Any]
    warmup_policy: dict[str, Any]
    split_policy: dict[str, Any]
    purge_policy: dict[str, Any]
    embargo_policy: dict[str, Any]
    cost_assumptions: dict[str, Any]
    fee_assumptions: dict[str, Any]
    slippage_assumptions: dict[str, Any]
    impact_assumptions: dict[str, Any]
    parameter_space: dict[str, Any]
    search_method: str
    random_seed: int | None
    maximum_trial_budget: int
    selection_metric: str
    secondary_metrics: tuple[str, ...]
    acceptance_rules: dict[str, Any]
    stress_tests: dict[str, Any]
    holdout_policy: dict[str, Any]
    code_provenance: dict[str, Any]
    sample_sufficiency_policy: dict[str, Any] | None = None
    candidate_selection_rule: str | None = None
    multiple_testing_policy: dict[str, Any] | None = None
    promotion_gates: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.protocol_version or not self.experiment_id:
            raise GovernanceError("protocol_version et experiment_id sont obligatoires")
        if not _GIT_SHA_RE.fullmatch(self.base_git_sha):
            raise GovernanceError("base_git_sha doit être un SHA Git complet")
        _utc(self.created_at, "created_at")
        if self.maximum_trial_budget <= 0:
            raise GovernanceError("maximum_trial_budget doit être positif")
        if not self.dataset_ids:
            raise GovernanceError("au moins un dataset est requis")
        if set(self.dataset_ids) != set(self.dataset_roles):
            raise GovernanceError("dataset_ids et dataset_roles doivent correspondre")
        if set(self.dataset_ids) != set(self.dataset_hashes):
            raise GovernanceError("dataset_ids et dataset_hashes doivent correspondre")
        for dataset_hash in self.dataset_hashes.values():
            if not _SHA256_RE.fullmatch(dataset_hash):
                raise GovernanceError("hash de dataset invalide")
        for cutoff in self.data_cutoffs.values():
            _utc(cutoff, "data_cutoff")
        if self.random_seed is not None and not isinstance(self.random_seed, int):
            raise GovernanceError("random_seed doit être un entier ou null")
        if self.split_policy.get("shuffle"):
            raise GovernanceError("les splits quantitatifs ne peuvent pas être mélangés")
        split_type = str(self.split_policy.get("type", "expanding")).lower()
        if split_type not in {"expanding", "rolling"}:
            raise GovernanceError("split type inconnu ou non chronologique")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))

    @property
    def semantic_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("created_at", None)
        return payload

    @property
    def fingerprint(self) -> str:
        return sha256_canonical(self.semantic_dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExperimentSpec:
        values = dict(payload)
        values["dataset_ids"] = tuple(values["dataset_ids"])
        values["secondary_metrics"] = tuple(values.get("secondary_metrics", ()))
        values["dataset_roles"] = {
            key: DatasetRole(value) for key, value in values["dataset_roles"].items()
        }
        return cls(**values)


class ExperimentRegistry:
    """Registre en mémoire; un backend durable peut sérialiser ``to_dict``."""

    def __init__(self) -> None:
        self._experiments: dict[str, tuple[str, ExperimentSpec]] = {}

    def register(self, spec: ExperimentSpec) -> str:
        current = self._experiments.get(spec.experiment_id)
        if current is not None and current[0] != spec.fingerprint:
            raise ExperimentInvalidated(
                "la specification a changé: créer un nouvel experiment_id/protocol_version"
            )
        self._experiments[spec.experiment_id] = (spec.fingerprint, spec)
        return spec.fingerprint

    def get(self, experiment_id: str) -> ExperimentSpec:
        try:
            return self._experiments[experiment_id][1]
        except KeyError as exc:
            raise GovernanceError("expérience non enregistrée") from exc


@dataclass(frozen=True)
class TrialRecord:
    experiment_id: str
    experiment_fingerprint: str
    trial_id: str
    sequence: int
    parameter_fingerprint: str
    parameters: dict[str, Any]
    status: str
    result: dict[str, Any]
    metrics: dict[str, Any]
    split_fingerprint: str
    code_sha: str
    cost_model_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


class TrialRegistry:
    """Tous les essais, y compris les erreurs et les résultats rejetés."""

    def __init__(self, spec: ExperimentSpec) -> None:
        self.spec = spec
        self._records: list[TrialRecord] = []

    @property
    def attempted(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[TrialRecord, ...]:
        return tuple(self._records)

    def record_trial(
        self,
        parameters: Mapping[str, Any],
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        split_fingerprint: str = "",
        code_sha: str | None = None,
        cost_assumptions: Mapping[str, Any] | None = None,
    ) -> TrialRecord:
        if self.attempted >= self.spec.maximum_trial_budget:
            raise TrialBudgetExceeded(
                f"trial budget dépassé: {self.attempted + 1} > {self.spec.maximum_trial_budget}"
            )
        sequence = self.attempted + 1
        record = TrialRecord(
            experiment_id=self.spec.experiment_id,
            experiment_fingerprint=self.spec.fingerprint,
            trial_id=f"{self.spec.experiment_id}:trial:{sequence:06d}",
            sequence=sequence,
            parameter_fingerprint=parameter_fingerprint(parameters),
            parameters=dict(parameters),
            status=status,
            result=dict(result or {}),
            metrics=dict(metrics or {}),
            split_fingerprint=split_fingerprint,
            code_sha=code_sha or self.spec.base_git_sha,
            cost_model_fingerprint=cost_model_fingerprint(
                cost_assumptions or self.spec.cost_assumptions
            ),
        )
        # A failure is still appended before it can be returned to callers.
        self._records.append(record)
        return record

    def to_list(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self._records]


def validate_time_index(index: Iterable[Any]) -> pd.DatetimeIndex:
    """Valide la grille temporelle sans trier ni normaliser silencieusement."""

    result = pd.DatetimeIndex(list(index))
    if result.tz is None:
        raise GovernanceError("les données de gouvernance doivent être UTC aware")
    result = result.tz_convert("UTC")
    if result.has_duplicates:
        raise GovernanceError("timestamps dupliqués dans une série temporelle")
    if not result.is_monotonic_increasing:
        raise GovernanceError("timestamps hors ordre dans une série temporelle")
    return result


def reject_non_temporal_split(*, split_type: str, shuffle: bool = False) -> None:
    if shuffle or split_type.lower() in {"random", "kfold", "random_kfold", "shuffle"}:
        raise GovernanceError("random/shuffled split interdit pour une série temporelle")


@dataclass(frozen=True)
class TimeSeriesFold:
    fold: int
    train_start: str
    train_end: str
    warmup_start: str
    evaluation_start: str
    evaluation_end: str
    purge_duration_seconds: float
    embargo_duration_seconds: float
    train_points: int
    warmup_points: int
    evaluation_points: int
    train_elapsed_seconds: float
    evaluation_elapsed_seconds: float
    initial_position: str = "FLAT"

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def generate_time_folds(
    index: Iterable[Any],
    *,
    train_duration: timedelta,
    evaluation_duration: timedelta,
    warmup_duration: timedelta,
    purge_duration: timedelta = timedelta(0),
    embargo_duration: timedelta = timedelta(0),
    mode: str = "expanding",
) -> list[TimeSeriesFold]:
    """Crée des folds UTC sur des durées, jamais sur des numéros de ligne."""

    timestamps = validate_time_index(index)
    reject_non_temporal_split(split_type=mode)
    if min(train_duration, evaluation_duration) <= timedelta(0):
        raise GovernanceError("train/evaluation duration doivent être positives")
    if min(warmup_duration, purge_duration, embargo_duration) < timedelta(0):
        raise GovernanceError("durées de contexte/purge/embargo invalides")
    if mode not in {"expanding", "rolling"}:
        raise GovernanceError("mode de fold non supporté")

    first = timestamps[0]
    fold = 0
    evaluation_start = first + train_duration + purge_duration
    out: list[TimeSeriesFold] = []
    while evaluation_start < timestamps[-1] + pd.Timedelta(1, unit="ns"):
        evaluation_end = evaluation_start + evaluation_duration
        train_end = evaluation_start - purge_duration
        train_start = first if mode == "expanding" else train_end - train_duration
        warmup_start = max(first, evaluation_start - warmup_duration)
        train_mask = (timestamps >= train_start) & (timestamps < train_end)
        warmup_mask = (timestamps >= warmup_start) & (timestamps < evaluation_start)
        evaluation_mask = (timestamps >= evaluation_start) & (timestamps < evaluation_end)
        if not train_mask.any() or not evaluation_mask.any():
            break
        train_values = timestamps[train_mask]
        warmup_values = timestamps[warmup_mask]
        evaluation_values = timestamps[evaluation_mask]
        out.append(
            TimeSeriesFold(
                fold=fold,
                train_start=_utc_iso(train_values[0], "train_start"),
                train_end=_utc_iso(train_values[-1], "train_end"),
                warmup_start=_utc_iso(warmup_values[0], "warmup_start")
                if len(warmup_values)
                else _utc_iso(evaluation_start, "warmup_start"),
                evaluation_start=_utc_iso(evaluation_values[0], "evaluation_start"),
                evaluation_end=_utc_iso(evaluation_values[-1], "evaluation_end"),
                purge_duration_seconds=purge_duration.total_seconds(),
                embargo_duration_seconds=embargo_duration.total_seconds(),
                train_points=len(train_values),
                warmup_points=len(warmup_values),
                evaluation_points=len(evaluation_values),
                train_elapsed_seconds=(train_values[-1] - train_values[0]).total_seconds(),
                evaluation_elapsed_seconds=(
                    evaluation_values[-1] - evaluation_values[0]
                ).total_seconds(),
            )
        )
        fold += 1
        evaluation_start = evaluation_end + embargo_duration
    return out


def fold_data(
    frame: pd.DataFrame,
    fold: TimeSeriesFold,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Retourne le contexte warmup et l'évaluation, avec une position plate."""

    validate_time_index(frame.index)
    warmup_start = _utc(fold.warmup_start, "warmup_start")
    evaluation_start = _utc(fold.evaluation_start, "evaluation_start")
    evaluation_end = _utc(fold.evaluation_end, "evaluation_end")
    context = frame.loc[(frame.index >= warmup_start) & (frame.index < evaluation_start)].copy()
    evaluation = frame.loc[
        (frame.index >= evaluation_start) & (frame.index <= evaluation_end)
    ].copy()
    if evaluation.empty:
        raise GovernanceError("fold sans données d'évaluation")
    return context, evaluation


def assert_flat_evaluation_start(initial_position: Any) -> None:
    if initial_position is not None:
        raise GovernanceError("un fold OOS doit démarrer flat; état du train détecté")


def derive_max_information_lookback(strategy: Any, timeframe: str) -> timedelta:
    """Dérive le contexte depuis le contrat de la stratégie, sans durée devinée."""

    if hasattr(strategy, "information_lookback"):
        bars = int(strategy.information_lookback())
    elif hasattr(strategy, "warmup_bars"):
        bars = int(strategy.warmup_bars())
    else:
        raise GovernanceError("la stratégie ne déclare aucun lookback exploitable")
    if bars <= 0:
        raise GovernanceError("lookback de stratégie invalide")
    deltas = {
        "1h": timedelta(hours=1),
        "2h": timedelta(hours=2),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }
    try:
        return bars * deltas[timeframe]
    except KeyError as exc:
        raise GovernanceError(f"timeframe sans durée de lookback: {timeframe}") from exc


T = TypeVar("T")


def _slice_result(result: Any, cutoff: pd.Timestamp) -> Any:
    if isinstance(result, (pd.Series, pd.DataFrame)):
        if not isinstance(result.index, pd.DatetimeIndex):
            raise GovernanceError("résultat temporel sans DatetimeIndex")
        return result.loc[result.index <= cutoff]
    if isinstance(result, Mapping):
        return {key: _slice_result(value, cutoff) for key, value in result.items()}
    return result


def _results_equal(left: Any, right: Any, *, atol: float, rtol: float) -> bool:
    if isinstance(left, (pd.Series, pd.DataFrame)) and isinstance(right, type(left)):
        try:
            pd.testing.assert_frame_equal(
                left.to_frame() if isinstance(left, pd.Series) else left,
                right.to_frame() if isinstance(right, pd.Series) else right,
                check_exact=atol == 0 and rtol == 0,
                atol=atol,
                rtol=rtol,
            )
            return True
        except AssertionError:
            return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(
            _results_equal(left[key], right[key], atol=atol, rtol=rtol) for key in left
        )
    return left == right


def assert_prefix_invariant(
    run: Callable[[Any], T],
    prefix: Any,
    extended: Any,
    cutoff: Any,
    *,
    atol: float = 0.0,
    rtol: float = 0.0,
) -> None:
    """Vérifie qu'ajouter le futur ne modifie pas le résultat jusqu'à ``cutoff``."""

    cutoff_ts = _utc(cutoff, "cutoff")
    prefix_result = run(prefix)
    extended_result = _slice_result(run(extended), cutoff_ts)
    if not _results_equal(
        _slice_result(prefix_result, cutoff_ts), extended_result, atol=atol, rtol=rtol
    ):
        raise GovernanceError("prefix invariance violée: une donnée future modifie le passé")


@dataclass(frozen=True)
class StabilityReport:
    candidate_score: float
    neighbor_scores: tuple[float, ...]
    candidate_rank: int | None
    score_dispersion: float
    worst_fold_score: float | None
    performance_concentration: float | None
    stable: bool | None

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


def evaluate_parameter_stability(
    candidate_score: float,
    neighbor_scores: Sequence[float],
    *,
    all_candidate_scores: Sequence[float] | None = None,
    candidate_fold_scores: Sequence[float] | None = None,
    max_neighbor_drop: float | None = None,
    max_fold_concentration: float | None = None,
) -> StabilityReport:
    """Mesure un plateau/effondrement sans décider de seuil par défaut."""

    values = tuple(float(value) for value in neighbor_scores)
    if not math.isfinite(candidate_score) or any(not math.isfinite(value) for value in values):
        raise GovernanceError("stabilité impossible avec une métrique non finie")
    rank = None
    if all_candidate_scores is not None:
        scores = sorted((float(value) for value in all_candidate_scores), reverse=True)
        rank = scores.index(float(candidate_score)) + 1 if candidate_score in scores else None
    dispersion = float(np.std(values, ddof=1)) if len(values) >= 2 else 0.0
    worst_fold = min(candidate_fold_scores) if candidate_fold_scores else None
    concentration = None
    if candidate_fold_scores:
        absolute = [abs(value) for value in candidate_fold_scores]
        total = sum(absolute)
        concentration = max(absolute) / total if total else 0.0
    stable: bool | None = None
    if max_neighbor_drop is not None or max_fold_concentration is not None:
        checks: list[bool] = []
        if max_neighbor_drop is not None:
            if not values:
                raise GovernanceError("un seuil de voisinage exige des voisins")
            checks.append(
                (candidate_score - min(values)) / max(abs(candidate_score), 1e-12)
                <= max_neighbor_drop
            )
        if max_fold_concentration is not None:
            if concentration is None:
                raise GovernanceError("un seuil de concentration exige des folds")
            checks.append(concentration <= max_fold_concentration)
        stable = all(checks)
    return StabilityReport(
        candidate_score=float(candidate_score),
        neighbor_scores=values,
        candidate_rank=rank,
        score_dispersion=dispersion,
        worst_fold_score=float(worst_fold) if worst_fold is not None else None,
        performance_concentration=concentration,
        stable=stable,
    )


_TRANSITIONS: dict[CandidateState, set[CandidateState]] = {
    CandidateState.DRAFT: {CandidateState.REGISTERED, CandidateState.INVALIDATED},
    CandidateState.REGISTERED: {
        CandidateState.DEVELOPMENT,
        CandidateState.FROZEN,
        CandidateState.INVALIDATED,
    },
    CandidateState.DEVELOPMENT: {
        CandidateState.FROZEN,
        CandidateState.REJECTED,
        CandidateState.INVALIDATED,
    },
    CandidateState.FROZEN: {CandidateState.BLIND_OOS_PENDING, CandidateState.INVALIDATED},
    CandidateState.BLIND_OOS_PENDING: {
        CandidateState.BLIND_OOS_EVALUATED,
        CandidateState.INVALIDATED,
    },
    CandidateState.BLIND_OOS_EVALUATED: {
        CandidateState.QUANT_RESEARCH_PASS,
        CandidateState.REJECTED,
    },
    CandidateState.QUANT_RESEARCH_PASS: set(),
    CandidateState.REJECTED: set(),
    CandidateState.INVALIDATED: set(),
}


@dataclass
class CandidateLifecycle:
    candidate_id: str
    parameter_fingerprint: str
    state: CandidateState = CandidateState.DRAFT

    def transition(
        self, target: CandidateState, *, parameter_fingerprint: str | None = None
    ) -> None:
        if target not in _TRANSITIONS[self.state]:
            raise GovernanceError(f"transition interdite {self.state.value} -> {target.value}")
        if self.state is CandidateState.FROZEN and parameter_fingerprint not in {
            None,
            self.parameter_fingerprint,
        }:
            self.state = CandidateState.INVALIDATED
            raise HoldoutInvalidated("candidat modifié après FROZEN: nouvelle identité requise")
        self.state = target

    def freeze(self) -> None:
        self.transition(CandidateState.FROZEN, parameter_fingerprint=self.parameter_fingerprint)


@dataclass
class HoldoutSeal:
    candidate_fingerprint: str
    parameter_fingerprint: str
    experiment_fingerprint: str
    code_sha: str
    dataset_policy: dict[str, Any]
    holdout_start_rule: str
    holdout_end_rule: str
    metrics: dict[str, Any]
    acceptance_gates: dict[str, Any]
    cost_assumptions: dict[str, Any]
    status: HoldoutStatus = HoldoutStatus.UNSEEN

    def open(self) -> None:
        if self.status is not HoldoutStatus.UNSEEN:
            raise HoldoutInvalidated("holdout déjà ouvert, dépensé ou invalidé")
        self.status = HoldoutStatus.PENDING

    def validate_identity(
        self,
        *,
        candidate_fingerprint: str,
        parameter_fingerprint: str,
        experiment_fingerprint: str,
        code_sha: str,
        cost_assumptions: Mapping[str, Any],
    ) -> None:
        expected_cost = cost_model_fingerprint(self.cost_assumptions)
        actual_cost = cost_model_fingerprint(cost_assumptions)
        identity_matches = (
            candidate_fingerprint == self.candidate_fingerprint
            and parameter_fingerprint == self.parameter_fingerprint
            and experiment_fingerprint == self.experiment_fingerprint
            and code_sha == self.code_sha
            and actual_cost == expected_cost
        )
        if self.status is HoldoutStatus.SPENT and identity_matches:
            return
        if not identity_matches:
            self.status = HoldoutStatus.INVALIDATED
            raise HoldoutInvalidated("identité du holdout scellé modifiée")

    def evaluate(self, metrics: Mapping[str, Any]) -> None:
        if self.status is not HoldoutStatus.PENDING:
            raise HoldoutInvalidated("un holdout doit être PENDING et n'est évaluable qu'une fois")
        validate_required_metrics(metrics, self.acceptance_gates)
        self.metrics = dict(metrics)
        self.status = HoldoutStatus.SPENT


def validate_required_metrics(metrics: Mapping[str, Any], gates: Mapping[str, Any]) -> None:
    """Les métriques manquantes/non finies bloquent une décision."""

    for metric, rule in gates.items():
        if rule is None:
            continue
        if metric not in metrics:
            raise GovernanceError(f"métrique requise absente: {metric}")
        value = metrics[metric]
        if not isinstance(value, (int, float, np.number)) or not math.isfinite(float(value)):
            raise GovernanceError(f"métrique requise non finie: {metric}")


@dataclass(frozen=True)
class SampleSufficiency:
    status: str
    rationale: str
    observations: int
    trades: int
    elapsed_seconds: float


def assess_sample_sufficiency(
    *,
    observations: int,
    trades: int,
    elapsed: timedelta,
    min_observations: int | None = None,
    min_trades: int | None = None,
    min_elapsed: timedelta | None = None,
) -> SampleSufficiency:
    if observations < 0 or trades < 0 or elapsed < timedelta(0):
        raise GovernanceError("échantillon invalide")
    rules_given = any(value is not None for value in (min_observations, min_trades, min_elapsed))
    if not rules_given:
        return SampleSufficiency(
            "DECISION_REQUIRED",
            "aucun seuil de suffisance n'est numériquement pré-enregistré",
            observations,
            trades,
            elapsed.total_seconds(),
        )
    insufficient = (
        (min_observations is not None and observations < min_observations)
        or (min_trades is not None and trades < min_trades)
        or (min_elapsed is not None and elapsed < min_elapsed)
    )
    return SampleSufficiency(
        "INSUFFICIENT_SAMPLE" if insufficient else "SUFFICIENT",
        "un ou plusieurs minimums pré-enregistrés ne sont pas atteints"
        if insufficient
        else "minimums pré-enregistrés atteints",
        observations,
        trades,
        elapsed.total_seconds(),
    )


def assess_family_sample_sufficiency(
    *,
    strategy_family: str,
    policies: Mapping[str, Any],
    observations: int,
    trades: int,
    elapsed: timedelta,
) -> SampleSufficiency:
    """Applique une politique de suffisance propre à une famille."""

    policy = policies.get(strategy_family)
    if not isinstance(policy, Mapping):
        return assess_sample_sufficiency(observations=observations, trades=trades, elapsed=elapsed)
    return assess_sample_sufficiency(
        observations=observations,
        trades=trades,
        elapsed=elapsed,
        min_observations=policy.get("min_observations"),
        min_trades=policy.get("min_trades"),
        min_elapsed=policy.get("min_elapsed"),
    )


def build_result_manifest(
    *,
    spec: ExperimentSpec,
    candidate_fingerprint: str,
    code_sha: str,
    datasets: Sequence[DatasetProvenance],
    splits: Sequence[TimeSeriesFold],
    trial_registry: TrialRegistry,
    selection_rule: str,
    metrics: Mapping[str, Any],
    stability: StabilityReport | None,
    holdout_status: HoldoutStatus,
    generated_at: str,
) -> dict[str, Any]:
    if code_sha != spec.base_git_sha:
        raise ExperimentInvalidated("code SHA différent de celui de la specification")
    payload: dict[str, Any] = {
        "experiment_fingerprint": spec.fingerprint,
        "protocol_version": spec.protocol_version,
        "candidate_fingerprint": candidate_fingerprint,
        "code_sha": code_sha,
        "datasets": [dataset.to_dict() for dataset in datasets],
        "splits": [split.to_dict() for split in splits],
        "trial_budget": spec.maximum_trial_budget,
        "trials_attempted": trial_registry.attempted,
        "trials": trial_registry.to_list(),
        "selection_rule": selection_rule,
        "metrics": dict(metrics),
        "cost_assumptions": spec.cost_assumptions,
        "stability": stability.to_dict() if stability else None,
        "holdout_status": holdout_status.value,
        "generated_at": _utc_iso(generated_at, "generated_at"),
    }
    # Le fingerprint du rapport est calculé sur le contenu stable; l'horodatage
    # est conservé pour l'audit mais ne rend pas une reproduction différente.
    stable_payload = dict(payload)
    stable_payload.pop("generated_at")
    payload["report_fingerprint"] = sha256_canonical(stable_payload)
    return payload


MULTIPLE_TESTING_ADVANCED_METRIC = "NOT_IMPLEMENTED"


def write_json_manifest(destination: str | Path, payload: Mapping[str, Any]) -> None:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8", newline="\n")
