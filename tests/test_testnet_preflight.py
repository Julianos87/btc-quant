from __future__ import annotations

import json

from btcquant.execution.testnet_preflight import evaluate_testnet_preflight


def test_preflight_is_fail_closed_and_read_only(tmp_path) -> None:
    report = evaluate_testnet_preflight(tmp_path)

    assert report["status"] == "FAIL"
    assert report["ready"] is False
    assert report["activation_performed"] is False
    assert report["authenticated_exchange_call"] is False
    assert "EXTERNAL_FILL_IDENTITY_NOT_PROVEN" in report["reason_codes"]
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
        "PAPER",
        "EXTERNAL_IDENTITY",
        "ORDER_EVIDENCE",
        "STATUS_CHRONOLOGY",
        "FINANCIAL_APPLICATION",
        "FINALIZATION",
        "ZERO_EFFECT",
        "SAFE_RETRY",
        "TESTNET_DB",
        "TESTNET_CONFIG",
        "TESTNET_SECRET",
        "SERVICE_STATE",
        "BACKUP",
        "MAINNET_LOCK",
    }
    assert gates["EXTERNAL_IDENTITY"]["status"] == "FAIL"
    assert gates["ORDER_EVIDENCE"]["reason"] == "ORDER_EVIDENCE_NOT_PROVEN"
    assert gates["STATUS_CHRONOLOGY"]["reason"] == "STATUS_CHRONOLOGY_NOT_PROVEN"
    assert gates["SERVICE_STATE"]["reason"] == "SERVICE_STATE_NOT_INSPECTED"
    assert report["activation_performed"] is False
