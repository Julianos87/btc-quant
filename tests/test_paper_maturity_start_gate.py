from __future__ import annotations

import json
from pathlib import Path

import pytest

from btcquant.execution import paper_technical_qualification as technical
from btcquant.execution.readiness import (
    ReadinessPolicy,
    canonical_service_component_profile,
    paper_maturity_status,
    start_paper_maturity_campaign,
)
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore


def _technical_evidence(sha: str, tree: str) -> dict[str, object]:
    return {
        "release_sha": sha,
        "release_tree": tree,
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
    }


def _prepare_bound_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, StateStore]:
    root = tmp_path / "runtime"
    database = root / "state" / "btcquant.db"
    database.parent.mkdir(parents=True)
    (root / ".env").write_text("# non-secret deployment profile\nBTCQUANT_REQUIRED_ENGINES=trend\n")
    store = StateStore(database)
    store.save_engine_state("trend", {"slots": {}, "halted": False})
    sha = "a" * 40
    tree = "b" * 40
    evidence = _technical_evidence(sha, tree)
    store.record_paper_technical_qualification(
        release_sha=sha,
        release_tree=tree,
        schema_version=SCHEMA_VERSION,
        full_test_results={"status": "PASS"},
        staging_run={"status": "PASS"},
        migration={"status": "PASS"},
        rollback_rehearsal={"status": "PASS"},
        production_health={"status": "PASS"},
        backup_verification={"status": "PASS"},
    )
    release = root / "releases" / sha
    release.mkdir(parents=True)
    manifest = {
        "git_sha": sha,
        "git_tree": tree,
        "schema_version_required": SCHEMA_VERSION,
        "config_file_sha256": "c" * 64,
    }
    monkeypatch.setattr(technical, "active_paper_release", lambda _root: (release, manifest))
    monkeypatch.setattr(technical, "_active_release", lambda _root: (release, manifest))
    monkeypatch.setattr(technical, "_paper_database", lambda _root, _release: database)
    monkeypatch.setattr(
        technical,
        "collect_paper_technical_evidence",
        lambda _root, probes=None: dict(evidence),
    )
    return root, store


def test_generic_v3_insert_is_closed_and_legacy_fixture_is_explicit(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    with pytest.raises(RuntimeError, match="start_bound_paper_maturity_campaign"):
        store.start_qualification_campaign(protocol_version=3, policy={})
    legacy = store.start_qualification_campaign(
        protocol_version=2, policy=ReadinessPolicy().to_dict()
    )
    assert legacy["protocol_version"] == 2


def test_secure_paper_start_binds_release_qualification_and_config(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    campaign = start_paper_maturity_campaign(root, now_factory=lambda: "2026-09-13T10:00:00+00:00")

    assert campaign["protocol_version"] == 3
    assert campaign["started_at"] == "2026-09-13T10:00:00+00:00"
    assert campaign["policy"]["binding"]["environment"] == "paper"
    assert campaign["policy"]["binding"]["technical_qualification_id"] == 1
    assert campaign["policy"]["binding"]["release_sha"] == "a" * 40
    assert len(store.read_orders()) == 0


def test_bound_start_reads_qualification_on_its_atomic_connection(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    from btcquant.execution.qualification_repository import QualificationRepository

    observed_connections = []
    original = QualificationRepository.paper_technical_qualification_record

    def capture(repository, qualification_id: int, *, connection=None):
        observed_connections.append(connection)
        return original(repository, qualification_id, connection=connection)

    monkeypatch.setattr(QualificationRepository, "paper_technical_qualification_record", capture)
    start_paper_maturity_campaign(root, now_factory=lambda: "2026-09-13T10:00:00+00:00")

    assert len(observed_connections) == 1
    assert observed_connections[0] is not None


def test_missing_technical_qualification_writes_no_campaign(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    with store._transaction() as connection:
        connection.execute("DELETE FROM readiness_reports")
    with pytest.raises(RuntimeError, match="qualification technique"):
        start_paper_maturity_campaign(root)
    assert store.active_qualification_campaign() is None


def test_unsafe_database_states_fail_closed_without_row_delta(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    store.begin_order(
        "trend", "strategy", "pending", "MARKET", "BUY", 1.0, "test", reference_price=100.0
    )
    with pytest.raises(RuntimeError, match="non résolus"):
        start_paper_maturity_campaign(root)
    assert store.active_qualification_campaign() is None


def test_required_trend_must_be_flat(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    store.save_engine_state(
        "trend",
        {
            "slots": {"default": {"position": {"qty": 1.0}}},
            "halted": False,
        },
    )
    with pytest.raises(RuntimeError, match="Trend n'est pas FLAT"):
        start_paper_maturity_campaign(root)
    assert store.active_qualification_campaign() is None


def test_duplicate_bound_start_is_rejected(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)
    with pytest.raises(RuntimeError, match="déjà active"):
        start_paper_maturity_campaign(root)
    with store._read_connection(None) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM qualification_campaigns WHERE status='RUNNING'"
        ).fetchone()[0]
    assert count == 1


def test_binding_mismatch_blocks_maturity_status(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)
    changed = {
        "git_sha": "d" * 40,
        "git_tree": "b" * 40,
        "schema_version_required": 14,
        "config_file_sha256": "c" * 64,
    }
    monkeypatch.setattr(
        technical, "active_paper_release", lambda _root: (root / "releases" / ("a" * 40), changed)
    )
    status = paper_maturity_status(store, root=root)
    assert status["qualified"] is False
    assert status["reason_code"] == "PAPER_MATURITY_BINDING_MISMATCH"
    assert status["binding_status"] == "MISMATCH"


def test_legacy_campaign_is_readable_but_not_new_maturity_qualification(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    campaign = store.start_qualification_campaign(
        protocol_version=2, policy=ReadinessPolicy().to_dict()
    )
    status = paper_maturity_status(store)
    assert campaign["protocol_version"] == 2
    assert status["status"] == "PAPER_MATURITY_IN_PROGRESS"
    assert status["binding_status"] == "UNBOUND_LEGACY"
    assert status["reason_code"] == "PAPER_MATURITY_BINDING_MISMATCH"


def test_config_identity_is_deterministic_and_secret_free() -> None:
    manifest = {"config_file_sha256": "a" * 64}
    first = technical.paper_config_identity(manifest, required_engines=("trend",))
    second = technical.paper_config_identity(manifest, required_engines=("trend",))
    assert first == second
    assert len(first) == 64
    assert "SECRET" not in json.dumps(manifest)


def test_atomic_recheck_rejects_state_changed_after_fresh_collection(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    original = technical.collect_paper_technical_evidence

    def collect_then_make_unsafe(_root: Path, probes=None):
        store.begin_order(
            "trend", "strategy", "race", "MARKET", "BUY", 1.0, "race", reference_price=100.0
        )
        return original(_root, probes=probes)

    monkeypatch.setattr(technical, "collect_paper_technical_evidence", collect_then_make_unsafe)
    with pytest.raises(RuntimeError, match="non résolus"):
        start_paper_maturity_campaign(root)
    assert store.active_qualification_campaign() is None


def test_paper_cli_rejects_arbitrary_database(monkeypatch, tmp_path: Path) -> None:
    from btcquant.entrypoints import readiness as entrypoint

    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(entrypoint, "ROOT", root)
    monkeypatch.setattr(
        entrypoint.sys,
        "argv",
        [
            "btcquant-readiness",
            "start",
            "--profile",
            "paper",
            "--database",
            str(root / "state" / "btcquant-testnet.db"),
        ],
    )
    with pytest.raises(SystemExit, match="canonical PAPER database"):
        entrypoint.main()


def test_fresh_evidence_failure_leaves_campaign_table_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    from btcquant.execution.paper_technical_qualification import QualificationFailed

    def fail(_root: Path, probes=None):
        raise QualificationFailed("BACKUP_VERIFICATION_FAILED", "fixture failure")

    monkeypatch.setattr(technical, "collect_paper_technical_evidence", fail)
    with pytest.raises(QualificationFailed, match="BACKUP_VERIFICATION_FAILED"):
        start_paper_maturity_campaign(root)
    assert store.active_qualification_campaign() is None


def test_runtime_required_engine_change_invalidates_existing_binding(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)
    (root / ".env").write_text("BTCQUANT_REQUIRED_ENGINES=carry\n")
    monkeypatch.delenv("BTCQUANT_REQUIRED_ENGINES", raising=False)
    status = paper_maturity_status(store, root=root)
    assert status["qualified"] is False
    assert status["reason_code"] == "PAPER_MATURITY_BINDING_MISMATCH"
    assert status["binding"]["comparisons"]["required_engines"] is False


@pytest.mark.parametrize(
    ("contents", "required", "reason"),
    [
        ("OTHER_SECRET=ignored\n", ("trend",), ()),
        ("BTCQUANT_REQUIRED_ENGINES=trend\n", ("trend",), ()),
        ("BTCQUANT_REQUIRED_ENGINES=carry\n", ("carry",), ()),
        ("BTCQUANT_REQUIRED_ENGINES=trend,carry\n", ("trend", "carry"), ()),
        (
            "BTCQUANT_REQUIRED_ENGINES=trend\nBTCQUANT_REQUIRED_ENGINES=trend\n",
            ("trend",),
            ("AMBIGUOUS_REQUIRED_ENGINE_PROFILE",),
        ),
        (
            "BTCQUANT_REQUIRED_ENGINES=trend\nBTCQUANT_REQUIRED_ENGINES=carry\n",
            ("trend",),
            ("AMBIGUOUS_REQUIRED_ENGINE_PROFILE",),
        ),
        ("BTCQUANT_REQUIRED_ENGINES=unknown\n", ("trend",), ("INVALID_REQUIRED_ENGINE_PROFILE",)),
        ("BTCQUANT_REQUIRED_ENGINES=\n", ("trend",), ("INVALID_REQUIRED_ENGINE_PROFILE",)),
    ],
)
def test_canonical_profile_parser_is_strict_and_allow_listed(
    tmp_path: Path, contents: str, required: tuple[str, ...], reason: tuple[str, ...]
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / ".env").write_text(contents)
    profile = canonical_service_component_profile(root)
    assert profile.required == required
    assert profile.reason_codes == reason


def test_canonical_profile_ignores_secret_looking_lines(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / ".env").write_text(
        "BACKUP_ENCRYPTION_KEY=do-not-surface\n"
        "TELEGRAM_TOKEN=do-not-surface\n"
        "BTCQUANT_REQUIRED_ENGINES=carry\n"
    )
    profile = canonical_service_component_profile(root)
    assert profile.required == ("carry",)
    assert "do-not-surface" not in repr(profile)


def test_binding_uses_canonical_profile_when_ambient_shell_diverges(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)

    monkeypatch.setenv("BTCQUANT_REQUIRED_ENGINES", "carry")
    status = paper_maturity_status(store, root=root)
    assert status["binding_status"] == "PASS"

    (root / ".env").write_text("BTCQUANT_REQUIRED_ENGINES=carry\n")
    monkeypatch.delenv("BTCQUANT_REQUIRED_ENGINES", raising=False)
    status = paper_maturity_status(store, root=root)
    assert status["binding_status"] == "MISMATCH"
    assert status["reason_code"] == "PAPER_MATURITY_BINDING_MISMATCH"


def test_ambiguous_canonical_profile_refuses_new_start(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    (root / ".env").write_text("BTCQUANT_REQUIRED_ENGINES=trend\nBTCQUANT_REQUIRED_ENGINES=trend\n")
    with pytest.raises(RuntimeError, match="AMBIGUOUS_REQUIRED_ENGINE_PROFILE"):
        start_paper_maturity_campaign(root)
    assert store.active_qualification_campaign() is None


def test_ambiguous_canonical_profile_fails_closed_for_status_and_finalize(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)
    (root / ".env").write_text("BTCQUANT_REQUIRED_ENGINES=trend\nBTCQUANT_REQUIRED_ENGINES=carry\n")
    monkeypatch.delenv("BTCQUANT_REQUIRED_ENGINES", raising=False)
    status = paper_maturity_status(store, root=root)
    assert status["binding_status"] == "UNKNOWN"
    assert status["qualified"] is False

    from btcquant.execution.readiness import finalize_campaign

    with pytest.raises(RuntimeError, match="binding mismatch"):
        finalize_campaign(store)


def test_missing_canonical_profile_fails_closed(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)
    (root / ".env").unlink()
    status = paper_maturity_status(store, root=root)
    assert status["binding_status"] == "UNKNOWN"
    assert status["qualified"] is False


def test_same_release_reobservation_does_not_invalidate_bound_campaign(
    monkeypatch, tmp_path: Path
) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)
    sha = "a" * 40
    tree = "b" * 40
    store.record_paper_technical_qualification(
        release_sha=sha,
        release_tree=tree,
        schema_version=SCHEMA_VERSION,
        full_test_results={"status": "PASS"},
        staging_run={"status": "PASS"},
        migration={"status": "PASS"},
        rollback_rehearsal={"status": "PASS"},
        production_health={"status": "PASS"},
        backup_verification={"status": "PASS"},
    )
    status = paper_maturity_status(store, root=root)
    assert status["binding_status"] == "PASS"
    assert status["binding"]["comparisons"]["technical_qualification_id"] is True


def test_read_only_status_does_not_persist_a_report(monkeypatch, tmp_path: Path) -> None:
    root, store = _prepare_bound_fixture(monkeypatch, tmp_path)
    start_paper_maturity_campaign(root)
    before = store.latest_readiness_report()
    paper_maturity_status(store, root=root)
    assert store.latest_readiness_report() == before


def test_relative_paper_database_resolution_is_root_relative(monkeypatch, tmp_path: Path) -> None:
    from btcquant.entrypoints.readiness import _resolve_requested_database

    root = tmp_path / "runtime"
    unrelated = tmp_path / "unrelated"
    root.mkdir()
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    assert (
        _resolve_requested_database(root, "state/btcquant.db")
        == (root / "state/btcquant.db").resolve()
    )


def test_relative_testnet_database_is_rejected_for_paper_cli(monkeypatch, tmp_path: Path) -> None:
    from btcquant.entrypoints import readiness as entrypoint

    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(entrypoint, "ROOT", root)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(
        entrypoint.sys,
        "argv",
        [
            "btcquant-readiness",
            "start",
            "--profile",
            "paper",
            "--database",
            "state/btcquant-testnet.db",
        ],
    )
    with pytest.raises(SystemExit, match="canonical PAPER database"):
        entrypoint.main()


def test_absolute_canonical_database_is_root_independent(tmp_path: Path) -> None:
    from btcquant.entrypoints.readiness import _resolve_requested_database

    root = tmp_path / "runtime"
    canonical = root / "state" / "btcquant.db"
    assert _resolve_requested_database(root, str(canonical)) == canonical.resolve()


def test_relative_arbitrary_database_is_rejected_for_paper_cli(monkeypatch, tmp_path: Path) -> None:
    from btcquant.entrypoints import readiness as entrypoint

    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(entrypoint, "ROOT", root)
    monkeypatch.setattr(
        entrypoint.sys,
        "argv",
        [
            "btcquant-readiness",
            "start",
            "--profile",
            "paper",
            "--database",
            "state/arbitrary.db",
        ],
    )
    with pytest.raises(SystemExit, match="canonical PAPER database"):
        entrypoint.main()


def test_symlink_resolving_to_testnet_database_is_rejected(monkeypatch, tmp_path: Path) -> None:
    from btcquant.entrypoints import readiness as entrypoint

    root = tmp_path / "runtime"
    state = root / "state"
    state.mkdir(parents=True)
    (state / "btcquant-testnet.db").touch()
    (state / "paper-alias.db").symlink_to("btcquant-testnet.db")
    monkeypatch.setattr(entrypoint, "ROOT", root)
    monkeypatch.setattr(
        entrypoint.sys,
        "argv",
        [
            "btcquant-readiness",
            "start",
            "--profile",
            "paper",
            "--database",
            "state/paper-alias.db",
        ],
    )
    with pytest.raises(SystemExit, match="canonical PAPER database"):
        entrypoint.main()
