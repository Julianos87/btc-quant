from __future__ import annotations

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
