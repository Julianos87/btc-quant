"""Fail-closed identities for the research-only Carry V2 model."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from tests.carry_v2_git import require_git_worktree
from btcquant.research.carry_v2 import (
    BorrowObservation,
    CarryLeg,
    CarryPosition,
    CarryV2InputError,
    ExecutionCostAssumptions,
    FundingObservation,
    PriceObservation,
    basis_diagnostics,
    execution_stress,
    fully_loaded_funding_break_even,
    mark_to_market,
    recurring_funding_break_even,
    validate_synchronized_prices,
)


def _position(*, spot_current: float = 110.0, perp_current: float = 110.0) -> CarryPosition:
    return CarryPosition(
        entry_timestamp="2030-01-01T00:00:00Z",
        mark_timestamp="2030-01-02T00:00:00Z",
        capital_at_entry=1_000.0,
        spot=CarryLeg("BTC-SPOT", 1.0, 100.0, spot_current),
        perp=CarryLeg("BTC-PERP", 1.0, 100.0, perp_current),
        borrow_principal=0.0,
        costs=ExecutionCostAssumptions.symmetric(
            spot_fee_rate=0.0,
            perp_fee_rate=0.0,
            spot_slippage_bps=0.0,
            perp_slippage_bps=0.0,
        ),
    )


def _funding(rate: float = 0.0) -> tuple[FundingObservation, ...]:
    return (
        FundingObservation(
            "2030-01-01T12:00:00Z",
            rate,
            100.0,
            venue="test-venue",
            symbol="BTC-PERP",
            price_source="test-mark",
        ),
    )


def test_equal_leg_prices_cancel_price_pnl() -> None:
    result = mark_to_market(_position(), funding_events=_funding(), borrow_events=())
    assert result.spot_price_pnl == pytest.approx(10.0)
    assert result.perp_price_pnl == pytest.approx(-10.0)
    assert result.net_price_pnl == pytest.approx(0.0)
    assert result.total_pnl == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("spot_current", "perp_current", "expected"),
    [(110.0, 111.0, -1.0), (110.0, 109.0, 1.0)],
)
def test_basis_movement_is_the_net_price_pnl(
    spot_current: float, perp_current: float, expected: float
) -> None:
    result = mark_to_market(
        _position(spot_current=spot_current, perp_current=perp_current),
        funding_events=_funding(),
        borrow_events=(),
    )
    assert result.net_price_pnl == pytest.approx(expected)
    assert result.total_pnl == pytest.approx(expected)
    assert result.basis_change == pytest.approx(-expected)


def test_funding_only_and_borrow_only_are_separate_components() -> None:
    funding_position = _position()
    funding = _funding(rate=0.01)
    funding_result = mark_to_market(funding_position, funding_events=funding, borrow_events=())
    assert funding_result.funding_pnl == pytest.approx(1.0)
    assert funding_result.total_pnl == pytest.approx(1.0)

    borrow_position = CarryPosition(
        **{
            **funding_position.__dict__,
            "borrow_principal": 1_000.0,
        }
    )
    borrow_result = mark_to_market(
        borrow_position,
        funding_events=(),
        borrow_events=(BorrowObservation("2030-01-02T00:00:00Z", 0.36525, "test-borrow"),),
    )
    assert borrow_result.borrow_cost == pytest.approx(1.0)
    assert borrow_result.total_pnl == pytest.approx(-1.0)


def test_separate_fees_and_slippage_are_exactly_decomposed() -> None:
    position = CarryPosition(
        **{
            **_position().__dict__,
            "costs": ExecutionCostAssumptions(
                spot_entry_fee_rate=0.01,
                perp_entry_fee_rate=0.02,
                spot_exit_fee_rate=0.03,
                perp_exit_fee_rate=0.04,
                spot_entry_slippage_bps=10.0,
                perp_entry_slippage_bps=20.0,
                spot_exit_slippage_bps=30.0,
                perp_exit_slippage_bps=40.0,
            ),
        }
    )
    result = mark_to_market(
        position, funding_events=_funding(), borrow_events=(), include_exit_costs=True
    )
    assert result.spot_fees == pytest.approx(4.3)
    assert result.perp_fees == pytest.approx(6.4)
    assert result.spot_slippage == pytest.approx(0.43)
    assert result.perp_slippage == pytest.approx(0.64)
    assert result.total_pnl == pytest.approx(-11.77)


def test_missing_financial_source_fails_closed() -> None:
    with pytest.raises(CarryV2InputError, match="required"):
        mark_to_market(_position(), funding_events=None, borrow_events=())
    with pytest.raises(CarryV2InputError, match="required"):
        mark_to_market(_position(), funding_events=(), borrow_events=None)


def test_non_finite_and_non_positive_price_inputs_fail_closed() -> None:
    with pytest.raises(CarryV2InputError):
        CarryLeg("BTC", 1.0, float("nan"), 100.0)
    with pytest.raises(CarryV2InputError):
        CarryLeg("BTC", 1.0, 100.0, float("inf"))
    with pytest.raises(CarryV2InputError):
        CarryLeg("BTC", 0.0, 100.0, 100.0)


def test_price_contract_rejects_naive_duplicates_and_out_of_order() -> None:
    def item(timestamp: str, price: float = 100.0) -> PriceObservation:
        return PriceObservation(timestamp, "venue", "BTC", "close", "fixture", price)

    with pytest.raises(CarryV2InputError, match="timezone"):
        PriceObservation("2030-01-01", "venue", "BTC", "close", "fixture", 100.0)
    with pytest.raises(CarryV2InputError, match="duplicate"):
        validate_synchronized_prices(
            (item("2030-01-01T00:00Z"), item("2030-01-01T00:00Z")),
            (item("2030-01-01T00:00Z"), item("2030-01-01T00:00Z")),
        )
    with pytest.raises(CarryV2InputError, match="out-of-order"):
        validate_synchronized_prices(
            (item("2030-01-01T01:00Z"), item("2030-01-01T00:00Z")),
            (item("2030-01-01T01:00Z"), item("2030-01-01T00:00Z")),
        )


def test_synchronization_rejects_long_unsynchronised_gap() -> None:
    def item(timestamp: str) -> PriceObservation:
        return PriceObservation(timestamp, "venue", "BTC", "close", "fixture", 100.0)

    with pytest.raises(CarryV2InputError, match="synchronization"):
        validate_synchronized_prices(
            (item("2030-01-01T00:00Z"),),
            (item("2030-01-01T00:10Z"),),
            tolerance=pd.Timedelta("1min"),
        )


def test_break_even_matches_current_policy_known_values() -> None:
    assert recurring_funding_break_even(3.0, 0.10) == pytest.approx(0.0666666667)
    kwargs = {
        "leverage": 3.0,
        "borrow_rate_ann": 0.10,
        "spot_fee_rate": 0.0005,
        "perp_fee_rate": 0.0005,
        "spot_slippage_bps": 5.0,
        "perp_slippage_bps": 5.0,
    }
    assert fully_loaded_funding_break_even(**kwargs, holding_days=90) == pytest.approx(0.0828888889)
    assert fully_loaded_funding_break_even(**kwargs, holding_days=180) == pytest.approx(
        0.0747777778
    )


def test_basis_diagnostic_does_not_double_count_basis() -> None:
    def price(timestamp: str, value: float, symbol: str) -> PriceObservation:
        return PriceObservation(timestamp, "venue", symbol, "close", "fixture", value)

    spot = tuple(
        price(timestamp, value, "BTC-SPOT")
        for timestamp, value in (("2030-01-01T00:00Z", 100.0), ("2030-01-01T01:00Z", 110.0))
    )
    perp = tuple(
        price(timestamp, value, "BTC-PERP")
        for timestamp, value in (("2030-01-01T00:00Z", 100.0), ("2030-01-01T01:00Z", 111.0))
    )
    result = basis_diagnostics(spot, perp)
    assert result["basis_pnl_identity_max_abs_error"] == pytest.approx(0.0)
    assert result["net_price_pnl"]["worst"] == pytest.approx(0.0)


def test_mismatched_quantity_reports_delta() -> None:
    position = CarryPosition(
        **{
            **_position().__dict__,
            "perp": CarryLeg("BTC-PERP", 0.9, 100.0, 100.0),
        }
    )
    result = mark_to_market(position, funding_events=_funding(), borrow_events=())
    assert result.delta_qty == pytest.approx(0.1)
    assert result.delta_qty_pct == pytest.approx(0.1 / 1.0)


def test_execution_stress_exposes_temporary_delta_without_fake_margin_number() -> None:
    result = execution_stress(
        desired_spot_qty=1.0,
        desired_perp_qty=1.0,
        spot_fill_ratio=1.0,
        perp_fill_ratio=0.5,
        spot_entry_price=100.0,
        perp_entry_price=100.0,
        spot_fill_price=101.0,
        perp_fill_price=100.0,
    )
    assert result["temporary_delta_qty"] == pytest.approx(0.5)
    assert result["margin_impact"] is None


def _borrow_position(*, principal: float = 1_000.0) -> CarryPosition:
    return CarryPosition(
        entry_timestamp="2030-01-01T00:00:00Z",
        mark_timestamp="2030-01-02T00:00:00Z",
        capital_at_entry=1_000.0,
        spot=CarryLeg("BTC-SPOT", 1.0, 100.0, 100.0),
        perp=CarryLeg("BTC-PERP", 1.0, 100.0, 100.0),
        borrow_principal=principal,
        costs=ExecutionCostAssumptions.symmetric(
            spot_fee_rate=0.0,
            perp_fee_rate=0.0,
            spot_slippage_bps=0.0,
            perp_slippage_bps=0.0,
        ),
    )


def _borrow(timestamp: str, annualized_rate: float) -> BorrowObservation:
    return BorrowObservation(timestamp, annualized_rate, "test-borrow")


def test_borrow_requires_terminal_mark_coverage() -> None:
    result = mark_to_market(
        _borrow_position(),
        funding_events=(),
        borrow_events=(_borrow("2030-01-02T00:00:00Z", 0.36525),),
    )
    assert result.borrow_cost == pytest.approx(1.0)


def test_borrow_multiple_intervals_accrue_exactly() -> None:
    events = (
        _borrow("2030-01-01T08:00:00Z", 0.36525),
        _borrow("2030-01-01T16:00:00Z", 0.7305),
        _borrow("2030-01-02T00:00:00Z", 1.09575),
    )
    result = mark_to_market(_borrow_position(), funding_events=(), borrow_events=events)
    assert result.borrow_cost == pytest.approx(2.0)


def test_borrow_terminal_gap_fails_closed() -> None:
    with pytest.raises(CarryV2InputError, match="BORROW_COVERAGE_INCOMPLETE"):
        mark_to_market(
            _borrow_position(),
            funding_events=(),
            borrow_events=(_borrow("2030-01-01T12:00:00Z", 0.36525),),
        )


def test_borrow_observation_after_mark_is_refused() -> None:
    with pytest.raises(CarryV2InputError, match="outside position interval"):
        mark_to_market(
            _borrow_position(),
            funding_events=(),
            borrow_events=(_borrow("2030-01-02T01:00:00Z", 0.36525),),
        )


@pytest.mark.parametrize(
    "events",
    [
        (
            _borrow("2030-01-01T12:00:00Z", 0.36525),
            _borrow("2030-01-01T12:00:00Z", 0.36525),
        ),
        (
            _borrow("2030-01-01T18:00:00Z", 0.36525),
            _borrow("2030-01-01T12:00:00Z", 0.36525),
            _borrow("2030-01-02T00:00:00Z", 0.36525),
        ),
    ],
)
def test_borrow_duplicate_or_out_of_order_is_refused(
    events: tuple[BorrowObservation, ...],
) -> None:
    with pytest.raises(CarryV2InputError):
        mark_to_market(_borrow_position(), funding_events=(), borrow_events=events)


def test_borrow_empty_with_principal_fails_and_zero_principal_is_valid() -> None:
    with pytest.raises(CarryV2InputError, match="BORROW_COVERAGE_INCOMPLETE"):
        mark_to_market(_borrow_position(), funding_events=(), borrow_events=())
    result = mark_to_market(_borrow_position(principal=0.0), funding_events=(), borrow_events=())
    assert result.borrow_cost == pytest.approx(0.0)


def test_spot_perp_synchronization_accepts_independent_valid_pairs() -> None:
    def item(timestamp: str, symbol: str) -> PriceObservation:
        return PriceObservation(timestamp, "venue", symbol, "close", "fixture", 100.0)

    spot, perp = validate_synchronized_prices(
        (item("2030-01-01T00:00Z", "BTC-SPOT"),),
        (item("2030-01-01T00:00Z", "BTC-PERP"),),
    )
    assert len(spot) == len(perp) == 1


def test_unknown_cadence_does_not_fabricate_hourly_gaps(tmp_path) -> None:
    from scripts.qualify_carry_v2 import _data_inventory

    path = tmp_path / "borrow.csv"
    path.write_text("timestamp,rate\n2030-01-01T00:00:00Z,0.1\n2030-01-01T03:00:00Z,0.2\n")
    result = _data_inventory(
        path,
        timestamp_column="timestamp",
        venue="fixture",
        symbol="USDC",
        purpose="borrow",
        expected_frequency=None,
    )
    assert result["cadence_status"] == "NOT_DECLARED"
    assert result["missing_rows"] is None
    assert result["suitable_for_research"] is True


def test_artifact_provenance_uses_immediately_preceding_source_commit() -> None:
    root = Path(__file__).resolve().parents[1]
    artifact_path = root / "audit" / "carry_v2_economic_qualification.json"
    artifact_name = "audit/carry_v2_economic_qualification.json"
    if not artifact_path.exists():
        pytest.skip("artifact is generated in Commit B")
    require_git_worktree(root)
    if subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"HEAD:{artifact_name}"],
        check=False,
    ).returncode:
        pytest.skip("artifact is generated in Commit B")
    current_files = subprocess.check_output(
        ["git", "-C", str(root), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        text=True,
    ).splitlines()
    if current_files != [artifact_name]:
        pytest.skip("current commit is not the artifact-only commit")
    artifact = json.loads(artifact_path.read_text())
    source_sha = artifact["qualification_source_sha"]
    source_available = (
        subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{source_sha}^{{commit}}"],
            check=False,
        ).returncode
        == 0
    )
    if not source_available:
        shallow = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--is-shallow-repository"],
            text=True,
        ).strip()
        if shallow == "true":
            pytest.skip("source commit unavailable in shallow CI checkout")
        raise AssertionError("qualification source SHA is not available in full clone")

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    assert git("rev-parse", f"{source_sha}^{{commit}}") == source_sha
    assert git("rev-parse", f"{source_sha}^{{tree}}") == artifact["qualification_source_tree"]
    assert git("rev-parse", "HEAD^") == source_sha
    assert git("diff", "--name-only", "HEAD^", "HEAD").splitlines() == [
        "audit/carry_v2_economic_qualification.json"
    ]
    for path, expected_blob in artifact["qualification_source_files"].items():
        assert git("rev-parse", f"{source_sha}:{path}") == expected_blob


def test_invalid_qualification_source_sha_fails_closed() -> None:
    from scripts.qualify_carry_v2 import _source_provenance

    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        _source_provenance("0" * 40)
