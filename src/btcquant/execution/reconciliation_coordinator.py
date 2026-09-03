"""Explicit durable reconciliation orchestration without network acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .financial_fill_application import (
    FinancialApplicationLedgerConflict,
    FinancialFillApplicationError,
    FinancialFillCommitResult,
)
from .paper_execution_evidence import (
    PaperExecutionEvidence,
    PaperExecutionEvidencePersistenceResult,
)
from .resolution_projection import (
    PersistedResolutionAssessment,
    ProjectionStatus,
    assess_persisted_resolution,
)
from .state_store import StateStore


class ReconciliationStatus(StrEnum):
    """Closed outcome taxonomy for one explicit reconciliation pass.

    None of these outcomes finalizes an order or permits a subsequent broker
    attempt.  The coordinator only reports the durable evidence and the E3
    applications that it actually invoked.
    """

    NOT_READY = "NOT_READY"
    NO_FINANCIALLY_APPLICABLE_FILL = "NO_FINANCIALLY_APPLICABLE_FILL"
    NO_IRREVERSIBLY_AUTHORIZED_FILL = "NO_IRREVERSIBLY_AUTHORIZED_FILL"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    APPLIED = "APPLIED"
    APPLICATION_BLOCKED = "APPLICATION_BLOCKED"


@dataclass(frozen=True)
class ReconciliationResult:
    """Typed record of one finite ``PERSIST → PROJECT → ASSESS → APPLY`` pass."""

    local_order_id: int
    status: ReconciliationStatus | str
    evidence_persistence: PaperExecutionEvidencePersistenceResult | None
    before: PersistedResolutionAssessment
    after: PersistedResolutionAssessment
    financially_applicable_fill_keys: tuple[str, ...]
    irreversibly_authorized_fill_keys: tuple[str, ...]
    unapplied_fill_keys: tuple[str, ...]
    commits: tuple[FinancialFillCommitResult, ...]
    block_code: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        object.__setattr__(self, "status", ReconciliationStatus(self.status))
        for name in (
            "financially_applicable_fill_keys",
            "irreversibly_authorized_fill_keys",
            "unapplied_fill_keys",
        ):
            keys = getattr(self, name)
            if tuple(sorted(set(keys))) != keys or any(
                not isinstance(key, str) or not key for key in keys
            ):
                raise ValueError(f"{name} must be sorted, unique non-empty strings")
        if not isinstance(self.before, PersistedResolutionAssessment):
            raise TypeError("before must be a PersistedResolutionAssessment")
        if not isinstance(self.after, PersistedResolutionAssessment):
            raise TypeError("after must be a PersistedResolutionAssessment")
        if any(not isinstance(commit, FinancialFillCommitResult) for commit in self.commits):
            raise TypeError("commits must contain FinancialFillCommitResult values")
        if self.block_code is not None and (
            not isinstance(self.block_code, str) or not self.block_code.strip()
        ):
            raise ValueError("block_code must be None or a non-empty string")
        if self.status == ReconciliationStatus.APPLICATION_BLOCKED:
            if self.block_code is None:
                raise ValueError("APPLICATION_BLOCKED requires block_code")
        elif self.block_code is not None:
            raise ValueError("only APPLICATION_BLOCKED may carry block_code")


class OrderReconciliationCoordinator:
    """One finite durable reconciliation pass for an already-identified order.

    Evidence acquisition is deliberately outside this class.  A caller may
    provide the already-authoritative local PAPER execution evidence for the
    explicit persistence step; the coordinator never calls a broker, reader,
    or network API.  E3 remains the in-transaction authority for each
    financial application and therefore rechecks eligibility under its own
    SQLite transaction.
    """

    def __init__(self, store: StateStore) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        self._store = store

    @staticmethod
    def _local_order_id(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("local_order_id must be a positive integer")
        return value

    @staticmethod
    def _assessment_fill_keys(assessment: PersistedResolutionAssessment) -> tuple[str, ...]:
        if assessment.projection.status != ProjectionStatus.READY:
            return ()
        assert assessment.assessment is not None
        return assessment.assessment.financially_applicable_fill_keys

    @staticmethod
    def _irreversibly_authorized_fill_keys(
        assessment: PersistedResolutionAssessment,
    ) -> tuple[str, ...]:
        if assessment.projection.status != ProjectionStatus.READY:
            return ()
        bundle = assessment.projection.bundle
        assert bundle is not None
        fills_by_key = {fill.fill_key: fill for fill in bundle.fills if fill.fill_key is not None}
        return tuple(
            key
            for key in OrderReconciliationCoordinator._assessment_fill_keys(assessment)
            if fills_by_key.get(key) is not None and fills_by_key[key].venue_fill_id is not None
        )

    def reconcile(
        self,
        local_order_id: int,
        *,
        paper_evidence: PaperExecutionEvidence | None = None,
    ) -> ReconciliationResult:
        """Persist optional PAPER evidence, apply safe fills once, then reassess.

        This method deliberately has no retry loop: each initially safe,
        unapplied fill is offered to the E3 writer at most once in this pass.
        Any writer or ledger refusal stops further applications and is returned
        as a typed fail-closed result.
        """

        local_order_id = self._local_order_id(local_order_id)
        persistence: PaperExecutionEvidencePersistenceResult | None = None
        if paper_evidence is not None:
            if not isinstance(paper_evidence, PaperExecutionEvidence):
                raise TypeError("paper_evidence must be a PaperExecutionEvidence")
            if paper_evidence.context.local_order_id != local_order_id:
                raise ValueError("paper_evidence local_order_id differs from reconciliation target")
            persistence = self._store.persist_paper_execution_evidence(paper_evidence)

        before = assess_persisted_resolution(self._store, local_order_id)
        applicable = self._assessment_fill_keys(before)
        authorized = self._irreversibly_authorized_fill_keys(before)
        if before.projection.status != ProjectionStatus.READY:
            return ReconciliationResult(
                local_order_id,
                ReconciliationStatus.NOT_READY,
                persistence,
                before,
                before,
                applicable,
                authorized,
                (),
                (),
            )
        if not applicable:
            return ReconciliationResult(
                local_order_id,
                ReconciliationStatus.NO_FINANCIALLY_APPLICABLE_FILL,
                persistence,
                before,
                before,
                applicable,
                authorized,
                (),
                (),
            )
        if not authorized:
            return ReconciliationResult(
                local_order_id,
                ReconciliationStatus.NO_IRREVERSIBLY_AUTHORIZED_FILL,
                persistence,
                before,
                before,
                applicable,
                authorized,
                (),
                (),
            )

        try:
            applied_fill_keys = {
                record.fill_key
                for record in self._store.read_financial_fill_application_chain(local_order_id)
            }
        except FinancialApplicationLedgerConflict as error:
            after = assess_persisted_resolution(self._store, local_order_id)
            return ReconciliationResult(
                local_order_id,
                ReconciliationStatus.APPLICATION_BLOCKED,
                persistence,
                before,
                after,
                applicable,
                authorized,
                (),
                (),
                str(error),
            )
        unapplied = tuple(key for key in authorized if key not in applied_fill_keys)
        if not unapplied:
            return ReconciliationResult(
                local_order_id,
                ReconciliationStatus.ALREADY_APPLIED,
                persistence,
                before,
                before,
                applicable,
                authorized,
                (),
                (),
            )

        commits: list[FinancialFillCommitResult] = []
        block_code: str | None = None
        for fill_key in unapplied:
            try:
                commits.append(
                    self._store.apply_financial_fill_atomically(
                        local_order_id=local_order_id,
                        fill_key=fill_key,
                    )
                )
            except FinancialFillApplicationError as error:
                block_code = str(error)
                break
        after = assess_persisted_resolution(self._store, local_order_id)
        if block_code is not None:
            return ReconciliationResult(
                local_order_id,
                ReconciliationStatus.APPLICATION_BLOCKED,
                persistence,
                before,
                after,
                applicable,
                authorized,
                unapplied,
                tuple(commits),
                block_code,
            )
        status = (
            ReconciliationStatus.APPLIED
            if any(commit.applied for commit in commits)
            else ReconciliationStatus.ALREADY_APPLIED
        )
        return ReconciliationResult(
            local_order_id,
            status,
            persistence,
            before,
            after,
            applicable,
            authorized,
            unapplied,
            tuple(commits),
        )


__all__ = [
    "OrderReconciliationCoordinator",
    "ReconciliationResult",
    "ReconciliationStatus",
]
