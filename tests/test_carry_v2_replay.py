from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pandas as pd
import pytest

from scripts.acquire_carry_v2_data import _normalize_funding
from btcquant.research.carry_v2_replay import (
    ReplayInputError,
    ReplayPolicy,
    basis_summary,
    load_candle_csv,
    load_funding_csv,
    prepare_replay_frame,
    replay_policy,
    synchronize_price_frames,
)


def _prices(count: int = 72) -> tuple[pd.DataFrame, pd.DataFrame]:
    timestamps = pd.date_range("2030-01-01", periods=count, freq="h", tz="UTC")
    spot = pd.DataFrame({"timestamp": timestamps, "price": 100.0})
    perp = pd.DataFrame({"timestamp": timestamps, "price": 101.0})
    return spot, perp


def _funding(count: int = 72, rate: float = 0.04) -> pd.DataFrame:
    timestamps = pd.date_range("2030-01-01", periods=count, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "native_rate": rate,
            "reference_price": 100.0,
            "reference_price_timestamp": timestamps - pd.Timedelta("1ms"),
        }
    )


def test_synchronization_pairs_exact_hourly_data() -> None:
    paired, report = synchronize_price_frames(*_prices())
    assert len(paired) == 72
    assert report["status"] == "PASS"
    assert report["max_timestamp_skew_seconds"] == 0.0
    assert report["missing_periods"] == 0


def test_synchronization_rejects_unsynchronized_prices() -> None:
    spot, perp = _prices(3)
    perp["timestamp"] = perp["timestamp"] + pd.Timedelta("2min")
    with pytest.raises(ReplayInputError, match="synchronized"):
        synchronize_price_frames(spot, perp)


def test_synchronization_rejects_long_gaps() -> None:
    spot, perp = _prices(3)
    spot = spot.drop(index=1).reset_index(drop=True)
    perp = perp.drop(index=1).reset_index(drop=True)
    with pytest.raises(ReplayInputError, match="missing"):
        synchronize_price_frames(spot, perp)


def test_candle_loader_rejects_invalid_timestamps_and_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    pd.DataFrame(
        {
            "timestamp": ["2030-01-01T00:00:00Z", "not-a-time"],
            "open_timestamp": ["2030-01-01T00:00:00Z", "not-a-time"],
            "close_timestamp": ["2030-01-01T00:59:59.999Z", "not-a-time"],
            "open": [100, 100],
            "high": [100, 100],
            "low": [100, 100],
            "close": [100, 100],
        }
    ).to_csv(path, index=False)
    with pytest.raises(Exception, match="timestamp"):
        load_candle_csv(path, label="fixture")

    duplicate = tmp_path / "duplicate.csv"
    pd.DataFrame(
        {
            "timestamp": ["2030-01-01T00:00:00Z", "2030-01-01T00:00:00Z"],
            "open_timestamp": ["2030-01-01T00:00:00Z", "2030-01-01T00:00:00Z"],
            "close_timestamp": ["2030-01-01T00:59:59.999Z", "2030-01-01T00:59:59.999Z"],
            "open": [100, 100],
            "high": [100, 100],
            "low": [100, 100],
            "close": [100, 100],
        }
    ).to_csv(duplicate, index=False)
    with pytest.raises(ReplayInputError, match="duplicate"):
        load_candle_csv(duplicate, label="fixture")


def test_funding_loader_rejects_missing_hourly_period(tmp_path: Path) -> None:
    path = tmp_path / "funding.csv"
    frame = _funding(3)
    frame.loc[2, "timestamp"] += pd.Timedelta("1h")
    frame.to_csv(path, index=False)
    with pytest.raises(ReplayInputError, match="missing"):
        load_funding_csv(path)


def test_price_lookup_is_backward_only() -> None:
    spot, perp = _prices(3)
    prices, _ = synchronize_price_frames(spot, perp)
    funding = _funding(3)
    funding.loc[1, "timestamp"] = pd.Timestamp("2030-01-01T00:30:00Z")
    prepared, report = prepare_replay_frame(prices, funding)
    assert report["lookahead"] == "forbidden"
    assert prepared.loc[1, "spot_price"] == 100.0
    assert prepared.loc[1, "perp_price"] == 101.0


def test_equal_btc_hedge_and_equity_identity() -> None:
    spot, perp = _prices()
    perp["price"] = 101.0
    prices, _ = synchronize_price_frames(spot, perp)
    frame, _ = prepare_replay_frame(prices, _funding())
    result = replay_policy(frame, ReplayPolicy(smooth_days=1))
    assert result["entries"] == 1
    assert result["identity_residual_max_abs"] == 0.0
    assert result["cycles"][0]["qty"] > 0
    assert result["pnl"]["net_price_basis_pnl"] == 0.0


def test_future_prices_do_not_change_entry_fill() -> None:
    spot, perp = _prices()
    prices, _ = synchronize_price_frames(spot, perp)
    frame, _ = prepare_replay_frame(prices, _funding())
    changed = frame.copy()
    changed.loc[40:, "spot_price"] = 150.0
    changed.loc[40:, "perp_price"] = 151.0
    policy = ReplayPolicy(smooth_days=1)
    first = replay_policy(frame, policy)
    second = replay_policy(changed, policy)
    assert first["cycles"][0]["entry_timestamp"] == second["cycles"][0]["entry_timestamp"]
    assert first["cycles"][0]["spot_entry_price"] == second["cycles"][0]["spot_entry_price"]
    assert first["cycles"][0]["perp_entry_price"] == second["cycles"][0]["perp_entry_price"]


def test_basis_summary_reports_signed_distribution_and_signs() -> None:
    spot = pd.DataFrame(
        {
            "timestamp": pd.date_range("2030-01-01", periods=3, freq="h", tz="UTC"),
            "price": [100, 100, 100],
        }
    )
    perp = pd.DataFrame({"timestamp": spot["timestamp"], "price": [101, 99, 100]})
    prices, _ = synchronize_price_frames(spot, perp)
    report = basis_summary(prices)
    assert report["basis_abs"]["median"] == 0.0
    assert report["positive_basis_pct"] == pytest.approx(1 / 3)
    assert report["negative_basis_pct"] == pytest.approx(1 / 3)


def test_candle_close_is_unavailable_at_candle_open(tmp_path: Path) -> None:
    path = tmp_path / "causal.csv"
    frame = pd.DataFrame(
        {
            "open_timestamp": ["2030-01-01T01:00:00Z", "2030-01-01T02:00:00Z"],
            "close_timestamp": ["2030-01-01T01:59:59.999Z", "2030-01-01T02:59:59.999Z"],
            "timestamp": ["2030-01-01T01:59:59.999Z", "2030-01-01T02:59:59.999Z"],
            "open": [90, 100],
            "high": [90, 100],
            "low": [90, 100],
            "close": [90, 100],
        }
    )
    frame.to_csv(path, index=False)
    loaded = load_candle_csv(path, label="causal")
    assert loaded.loc[1, "timestamp"] == pd.Timestamp("2030-01-01T02:59:59.999Z")
    prices, _ = synchronize_price_frames(loaded, loaded)
    funding = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2030-01-01T02:00:00.050Z")],
            "native_rate": [0.01],
            "reference_price": [90.0],
            "reference_price_timestamp": [pd.Timestamp("2030-01-01T01:59:59.999Z")],
        }
    )
    prepared, _ = prepare_replay_frame(prices, funding)
    assert prepared.loc[0, "spot_price"] == 90.0
    assert prepared.loc[0, "perp_price"] == 90.0


def test_funding_reference_uses_previous_completed_candle() -> None:
    def milliseconds(value: str) -> int:
        return int(pd.Timestamp(value).timestamp() * 1000)

    perp_rows = [
        {
            "t": milliseconds("2030-01-01T01:00:00Z"),
            "T": milliseconds("2030-01-01T01:59:59.999Z"),
            "c": 90.0,
        },
        {
            "t": milliseconds("2030-01-01T02:00:00Z"),
            "T": milliseconds("2030-01-01T02:59:59.999Z"),
            "c": 100.0,
        },
    ]
    normalized = _normalize_funding(
        [
            {
                "time": milliseconds("2030-01-01T02:00:00.050Z"),
                "fundingRate": 0.01,
                "premium": 0.0,
            }
        ],
        perp_rows,
    )
    assert normalized[0]["reference_price"] == 90.0
    assert normalized[0]["reference_price_timestamp"] == "2030-01-01T01:59:59.999000Z"


def test_synchronization_rejects_future_observation() -> None:
    spot = pd.DataFrame({"timestamp": [pd.Timestamp("2030-01-01T02:00:00Z")], "price": [100.0]})
    perp = pd.DataFrame({"timestamp": [pd.Timestamp("2030-01-01T02:00:30Z")], "price": [101.0]})
    with pytest.raises(ReplayInputError, match="synchronized"):
        synchronize_price_frames(spot, perp, tolerance=pd.Timedelta("1min"))


def test_synchronization_allows_backward_observation_within_tolerance() -> None:
    spot = pd.DataFrame({"timestamp": [pd.Timestamp("2030-01-01T02:00:30Z")], "price": [100.0]})
    perp = pd.DataFrame({"timestamp": [pd.Timestamp("2030-01-01T02:00:00Z")], "price": [101.0]})
    paired, report = synchronize_price_frames(spot, perp, tolerance=pd.Timedelta("1min"))
    assert len(paired) == 1
    assert paired.loc[0, "perp_timestamp"] == pd.Timestamp("2030-01-01T02:00:00Z")
    assert report["matching"] == "backward_only"


def test_terminal_open_position_reports_mark_and_hypothetical_close() -> None:
    spot, perp = _prices(72)
    prices, _ = synchronize_price_frames(spot, perp)
    frame, _ = prepare_replay_frame(prices, _funding(72))
    result = replay_policy(frame, ReplayPolicy(smooth_days=1))
    assert result["terminal_position_open"] is True
    assert result["terminal_position_marked_to_market"] is True
    assert result["terminal_exit_costs_included"] is False
    valuation = result["terminal_valuation"]
    assert valuation["strategy_mark_to_market_equity"] == result["final_equity"]
    assert (
        valuation["hypothetical_terminal_liquidation_value_if_closed_at_final_mark"]
        < result["final_equity"]
    )
    assert valuation["is_strategy_exit"] is False


def test_real_data_artifact_has_reproducible_source_and_data_provenance() -> None:
    root = Path(__file__).resolve().parents[1]
    if subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain"], text=True
    ).strip():
        pytest.skip("the worktree is not the clean artifact commit")
    artifact_name = "audit/carry_v2_real_data_replay.json"
    artifact_path = root / artifact_name
    if (
        not artifact_path.exists()
        or subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"HEAD:{artifact_name}"],
            check=False,
        ).returncode
    ):
        pytest.skip("real-data artifact is generated in the artifact commit")
    current_files = subprocess.check_output(
        ["git", "-C", str(root), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        text=True,
    ).splitlines()
    if current_files != [artifact_name]:
        pytest.skip("the current commit is not the artifact-only commit")

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    source_sha = artifact["qualification_source_sha"]
    assert len(source_sha) == 40
    assert git("rev-parse", f"{source_sha}^{{commit}}") == source_sha
    assert git("rev-parse", "HEAD^") == source_sha
    assert git("rev-parse", f"{source_sha}^{{tree}}") == artifact["qualification_source_tree"]
    assert "artifact_commit_sha" not in artifact
    assert git("diff", "--name-only", "HEAD^", "HEAD").splitlines() == [artifact_name]

    for path, expected_blob in artifact["qualification_source_files"].items():
        assert git("rev-parse", f"{source_sha}:{path}") == expected_blob

    for source in artifact["data"]["source_files"].values():
        if source.get("path") is None:
            continue
        path = root / source["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
    metadata = artifact["data"]["metadata"]
    assert hashlib.sha256((root / metadata["path"]).read_bytes()).hexdigest() == metadata["sha256"]
