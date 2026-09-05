from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from btcquant.execution.external_capability_profile import hyperliquid_testnet_trend_ioc_v1
from btcquant.execution.readiness import ReadinessPolicy, paper_maturity_status, start_campaign
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore
from btcquant.execution.testnet_preflight import evaluate_testnet_preflight


def _pass_record() -> dict[str, str]:
    return {"status": "PASS", "source": "qualified-protocol"}


def test_preflight_is_fail_closed_and_read_only(tmp_path) -> None:
    report = evaluate_testnet_preflight(tmp_path)

    assert report["status"] == "FAIL"
    assert report["ready"] is False
    assert report["technical_ready"] is False
    assert report["activation_ready"] is False
    assert report["activation_performed"] is False
    assert report["authenticated_exchange_call"] is False
    assert "PAPER_TECHNICAL_QUALIFICATION_MISSING" in report["reason_codes"]
    assert "TESTNET_SECRET_PROVISIONING_REQUIRED" in report["reason_codes"]
    assert not (tmp_path / "state").exists()


def test_preflight_does_not_echo_secret_values(tmp_path) -> None:
    secret_values = {
        "HYPERLIQUID_WALLET_ADDRESS": "0x" + "a" * 40,
        "HYPERLIQUID_PRIVATE_KEY": "0x" + "b" * 64,
        "TELEGRAM_BOT_TOKEN": "token-value",
        "TELEGRAM_CHAT_ID": "1234",
    }
    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in secret_values.items()),
        encoding="utf-8",
    )

    report = evaluate_testnet_preflight(tmp_path)
    rendered = str(report)

    assert (
        next(item["value"] for item in report["checks"] if item["key"] == "secret_format")
        == "present_and_format_valid"
    )
    for value in secret_values.values():
        assert value not in rendered


def test_preflight_reads_canonical_release_manifest_tree(tmp_path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    expected_sha = "a" * 40
    expected_tree = "b" * 40
    (current / "release-manifest.json").write_text(
        json.dumps({"git_sha": expected_sha, "git_tree": expected_tree}),
        encoding="utf-8",
    )

    report = evaluate_testnet_preflight(
        tmp_path,
        expected_git_sha=expected_sha,
        expected_tree=expected_tree,
    )

    checks = {item["key"]: item for item in report["checks"]}
    assert checks["qualified_code_sha"]["passed"] is True
    assert checks["qualified_code_tree"]["passed"] is True


def test_preflight_exposes_independent_fail_closed_gate_summary(tmp_path) -> None:
    report = evaluate_testnet_preflight(tmp_path)

    gates = {item["name"]: item for item in report["gate_summary"]}
    assert set(gates) == {
        "CODE",
        "PAPER_HEALTH",
        "PAPER_TECHNICAL_QUALIFICATION",
        "EXTERNAL_CAPABILITY_PROFILE",
        "SUBMISSION_COMMITMENT",
        "ORDER_EVIDENCE",
        "FILL_EVIDENCE",
        "COORDINATOR",
        "STATUS_TIMESTAMP",
        "SETTLEMENT_COMPLETENESS",
        "SETTLEMENT_PERSISTENCE",
        "SETTLEMENT_APPLICATION",
        "EXTERNAL_FINALIZATION",
        "STARTUP_RECOVERY",
        "STOP_RECOVERY",
        "SAFE_RETRY",
        "TESTNET_DB",
        "TESTNET_ENDPOINT",
        "SERVICE_DEFINITION",
        "TESTNET_SECRET",
        "SERVICE_STATE",
        "BACKUP",
        "MAINNET_LOCK",
        "PAPER_MATURITY",
        "ACTIVATION_MARKER",
    }
    assert gates["EXTERNAL_CAPABILITY_PROFILE"]["status"] == "PASS"
    assert gates["ORDER_EVIDENCE"]["status"] == "PASS"
    assert gates["STATUS_TIMESTAMP"]["status"] == "PASS"
    assert gates["PAPER_TECHNICAL_QUALIFICATION"]["status"] == "FAIL"
    assert gates["SERVICE_STATE"]["reason"] == "SERVICE_STATE_NOT_INSPECTED"
    assert report["activation_performed"] is False


def test_capability_profile_is_explicit_and_non_activating() -> None:
    profile = hyperliquid_testnet_trend_ioc_v1()

    assert profile.name == "HYPERLIQUID_TESTNET_TREND_IOC_V1"
    assert profile.venue == "hyperliquid"
    assert profile.environment == "testnet"
    assert profile.engines == ("trend",)
    assert profile.order_style == "IOC_MARKET"
    assert profile.accounting_mode.value == "TERMINAL_IOC_SETTLEMENT"
    assert profile.supported_fee_assets == ("USDC",)
    assert profile.automatic_retry_enabled is False
    assert profile.protective_stop_qualified is True
    assert profile.technical_contract_passed is True


def test_durable_paper_technical_qualification_requires_structured_pass(tmp_path) -> None:
    store = StateStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="structured PASS"):
        store.record_paper_technical_qualification(
            release_sha="a" * 40,
            release_tree="b" * 40,
            schema_version=SCHEMA_VERSION,
            full_test_results={"status": "FAIL"},
            staging_run=_pass_record(),
            migration=_pass_record(),
            rollback_rehearsal=_pass_record(),
            production_health=_pass_record(),
            backup_verification=_pass_record(),
        )

    row_id = store.record_paper_technical_qualification(
        release_sha="a" * 40,
        release_tree="b" * 40,
        schema_version=SCHEMA_VERSION,
        full_test_results=_pass_record(),
        staging_run=_pass_record(),
        migration=_pass_record(),
        rollback_rehearsal=_pass_record(),
        production_health=_pass_record(),
        backup_verification=_pass_record(),
        qualified_at="2026-09-05T12:00:00+02:00",
    )
    payload = store.latest_paper_technical_qualification()

    assert row_id > 0
    assert payload is not None
    assert payload["kind"] == "PAPER_TECHNICAL_QUALIFICATION"
    assert payload["status"] == "PAPER_TECHNICAL_QUALIFIED"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["release_sha"] == "a" * 40
    assert payload["qualified_at"] == "2026-09-05T10:00:00+00:00"


def test_paper_maturity_status_is_observed_and_separate(tmp_path) -> None:
    store = StateStore(tmp_path / "paper.db")
    now = datetime(2026, 9, 5, tzinfo=UTC)

    before = paper_maturity_status(store, now=now)
    assert before["status"] == "NOT_STARTED"
    assert before["qualified"] is False
    assert before["earliest_time_criterion"] is None

    start_campaign(
        store,
        ReadinessPolicy(
            min_observation_days=90,
            min_closed_trades=30,
            min_terminal_orders=50,
            min_terminal_orders_per_engine=5,
        ),
        started_at="2026-09-01T00:00:00Z",
    )
    after = paper_maturity_status(store, now=now)
    assert after["status"] == "PAPER_MATURITY_IN_PROGRESS"
    assert after["qualified"] is False
    assert after["time_criterion_met"] is False
    assert after["earliest_time_criterion"] == "2026-11-30T00:00:00+00:00"


def test_preflight_separates_technical_and_activation_blockers(tmp_path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    release_sha = "a" * 40
    release_tree = "b" * 40
    (current / "release-manifest.json").write_text(
        json.dumps({"git_sha": release_sha, "git_tree": release_tree}),
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state" / "btcquant.db")
    store.record_paper_technical_qualification(
        release_sha=release_sha,
        release_tree=release_tree,
        schema_version=SCHEMA_VERSION,
        full_test_results=_pass_record(),
        staging_run=_pass_record(),
        migration=_pass_record(),
        rollback_rehearsal=_pass_record(),
        production_health=_pass_record(),
        backup_verification=_pass_record(),
        qualified_at="2026-09-05T12:00:00Z",
    )
    report = evaluate_testnet_preflight(
        tmp_path,
        expected_git_sha=release_sha,
        expected_tree=release_tree,
    )
    checks = {item["key"]: item for item in report["checks"]}

    assert checks["paper_technical_qualification"]["passed"] is True
    assert "TESTNET_SECRET_PROVISIONING_REQUIRED" not in report["technical_reason_codes"]
    assert "PAPER_MATURITY_IN_PROGRESS" not in report["technical_reason_codes"]
    assert "TESTNET_SECRET_PROVISIONING_REQUIRED" in report["activation_blockers"]
    assert "PAPER_MATURITY_IN_PROGRESS" in report["activation_blockers"]
    assert report["technical_ready"] is False
    assert report["activation_ready"] is False
