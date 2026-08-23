"""Causal, research-only replay for the current Carry V1 policy.

This module is deliberately separate from the production Carry runner.  It
consumes public historical observations and returns an explicit two-leg
accounting decomposition.  It never reads a database or submits an exchange
action.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from btcquant.carry import SECONDS_PER_YEAR, smooth_funding_events
from btcquant.domain.carry_decision import decide_carry_payment


class ReplayInputError(ValueError):
    """A replay input violates the causal or provenance contract."""


SYNC_TOLERANCE = pd.Timedelta("1min")
EXPECTED_HOURLY = pd.Timedelta("1h")


def _finite_series(values: pd.Series, name: str) -> None:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.isna().any() or not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ReplayInputError(f"{name} must be finite")


def _parse_timestamps(values: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_datetime(values, utc=True, format="mixed", errors="coerce")
    if parsed.isna().any():
        raise ReplayInputError(f"{name} contains invalid timestamps")
    if parsed.duplicated().any():
        raise ReplayInputError(f"{name} contains duplicate timestamps")
    delta = parsed.diff().dropna()
    if (delta < pd.Timedelta(0)).any():
        raise ReplayInputError(f"{name} is out of order")
    return parsed


def load_candle_csv(path: Path, *, label: str) -> pd.DataFrame:
    """Load candles with close availability at the exchange-reported end time."""

    frame = pd.read_csv(path, compression="infer")
    required = {"open_timestamp", "close_timestamp", "timestamp", "open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ReplayInputError(f"{label} missing columns: {sorted(missing)}")
    for column in ("open_timestamp", "close_timestamp", "timestamp"):
        frame[column] = _parse_timestamps(frame[column], f"{label} {column}")
    if not (frame["timestamp"] == frame["close_timestamp"]).all():
        raise ReplayInputError(f"{label} timestamp must equal close_timestamp / available_at")
    if (frame["close_timestamp"] < frame["open_timestamp"]).any():
        raise ReplayInputError(f"{label} close_timestamp precedes open_timestamp")
    for column in ("open", "high", "low", "close"):
        _finite_series(frame[column], f"{label} {column}")
        if (frame[column].astype(float) <= 0).any():
            raise ReplayInputError(f"{label} {column} must be positive")
    frame["close"] = frame["close"].astype(float)
    delta = frame["timestamp"].diff().dropna()
    if not delta.empty and (delta != EXPECTED_HOURLY).any():
        raise ReplayInputError(f"{label} has missing or non-hourly periods")
    frame["available_at"] = frame["close_timestamp"]
    return frame[
        ["open_timestamp", "close_timestamp", "available_at", "timestamp", "close"]
    ].rename(columns={"close": "price"})


def load_funding_csv(path: Path) -> pd.DataFrame:
    """Load native hourly funding and its completed-candle approximation."""

    frame = pd.read_csv(path, compression="infer")
    required = {"timestamp", "native_rate", "reference_price", "reference_price_timestamp"}
    missing = required.difference(frame.columns)
    if missing:
        raise ReplayInputError(f"funding missing columns: {sorted(missing)}")
    frame["timestamp"] = _parse_timestamps(frame["timestamp"], "funding timestamps")
    frame["reference_price_timestamp"] = _parse_timestamps(
        frame["reference_price_timestamp"], "funding reference timestamps"
    )
    if (frame["reference_price_timestamp"] > frame["timestamp"]).any():
        raise ReplayInputError("funding reference candle is not completed at the funding event")
    _finite_series(frame["native_rate"], "funding native_rate")
    _finite_series(frame["reference_price"], "funding reference_price")
    if (frame["reference_price"].astype(float) <= 0).any():
        raise ReplayInputError("funding reference_price must be positive")
    slots = frame["timestamp"].dt.floor("h")
    if slots.duplicated().any():
        raise ReplayInputError("funding contains duplicate hourly slots")
    nominal_delta = slots.diff().dropna()
    if not nominal_delta.empty and (nominal_delta != EXPECTED_HOURLY).any():
        raise ReplayInputError("funding has missing or non-hourly periods")
    frame["native_rate"] = frame["native_rate"].astype(float)
    frame["reference_price"] = frame["reference_price"].astype(float)
    return frame


def synchronize_price_frames(
    spot: pd.DataFrame,
    perp: pd.DataFrame,
    *,
    tolerance: pd.Timedelta = SYNC_TOLERANCE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Causally pair spot/perp closes and reject long gaps."""

    if tolerance <= pd.Timedelta(0):
        raise ReplayInputError("synchronization tolerance must be positive")
    spot_values = spot[["timestamp", "price"]].rename(
        columns={"timestamp": "spot_timestamp", "price": "spot_price"}
    )
    perp_values = perp[["timestamp", "price"]].rename(
        columns={"timestamp": "perp_timestamp", "price": "perp_price"}
    )
    paired = pd.merge_asof(
        spot_values.sort_values("spot_timestamp"),
        perp_values.sort_values("perp_timestamp"),
        left_on="spot_timestamp",
        right_on="perp_timestamp",
        direction="backward",
        tolerance=tolerance,
    )
    missing = paired["perp_timestamp"].isna()
    dropped_spot = int(missing.sum())
    paired = paired.loc[~missing].copy()
    if paired.empty:
        raise ReplayInputError("spot/perp observations are not synchronized")
    if (paired["perp_timestamp"] > paired["spot_timestamp"]).any():
        raise ReplayInputError("future perp observation accepted by synchronization")
    if paired["perp_timestamp"].duplicated().any():
        raise ReplayInputError("synchronization reused a perp observation")
    paired["timestamp_skew_seconds"] = (
        (paired["spot_timestamp"] - paired["perp_timestamp"]).abs().dt.total_seconds()
    )
    if (paired["timestamp_skew_seconds"] > tolerance.total_seconds()).any():
        raise ReplayInputError("spot/perp timestamp skew exceeds tolerance")
    timestamps = paired["spot_timestamp"]
    gaps = timestamps.diff().dropna()
    long_gaps = gaps[gaps != EXPECTED_HOURLY]
    if not long_gaps.empty:
        raise ReplayInputError("synchronized prices contain missing or non-hourly periods")
    result = paired.rename(columns={"spot_timestamp": "timestamp"})[
        ["timestamp", "perp_timestamp", "spot_price", "perp_price", "timestamp_skew_seconds"]
    ]
    report = {
        "status": "PASS",
        "matching": "backward_only",
        "pairs": int(len(result)),
        "coverage_start": result["timestamp"].iloc[0].isoformat(),
        "coverage_end": result["timestamp"].iloc[-1].isoformat(),
        "dropped_spot_rows": dropped_spot,
        "dropped_perp_rows": int(len(perp) - len(result)),
        "max_timestamp_skew_seconds": float(result["timestamp_skew_seconds"].max()),
        "missing_periods": 0,
        "tolerance": str(tolerance),
    }
    return result, report


def prepare_replay_frame(
    prices: pd.DataFrame,
    funding: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attach only already-observed prices to each funding event."""

    merged = pd.merge_asof(
        funding.sort_values("timestamp").rename(columns={"timestamp": "funding_timestamp"}),
        prices.sort_values("timestamp").rename(columns={"timestamp": "price_timestamp"}),
        left_on="funding_timestamp",
        right_on="price_timestamp",
        direction="backward",
    )
    missing = merged["spot_price"].isna() | merged["perp_price"].isna()
    dropped = int(missing.sum())
    merged = merged.loc[~missing].copy()
    if merged.empty:
        raise ReplayInputError("no funding event has a causal synchronized price")
    if (merged["price_timestamp"] > merged["funding_timestamp"]).any():
        raise ReplayInputError("future price close accepted for replay event")
    if (merged["perp_timestamp"] > merged["funding_timestamp"]).any():
        raise ReplayInputError("future perp close accepted for replay event")
    merged = merged.rename(columns={"funding_timestamp": "timestamp"})
    return merged, {
        "status": "PASS",
        "matching": "backward_only",
        "funding_events_input": int(len(funding)),
        "funding_events_replayed": int(len(merged)),
        "dropped_before_first_price": dropped,
        "price_lookup": "last synchronized close at or before funding timestamp",
        "lookahead": "forbidden",
    }


@dataclass(frozen=True)
class ReplayPolicy:
    capital: float = 4000.0
    leverage: float = 3.0
    smooth_days: int = 14
    enter_ann: float = 0.03
    exit_ann: float = 0.0
    spot_fee_rate: float = 0.0005
    perp_fee_rate: float = 0.0005
    slippage_bps: float = 5.0
    borrow_rate_ann: float = 0.10

    def __post_init__(self) -> None:
        if self.capital <= 0 or self.leverage < 1:
            raise ReplayInputError("capital and leverage must be positive")
        if self.smooth_days < 1 or self.enter_ann < self.exit_ann:
            raise ReplayInputError("invalid Carry V1 policy thresholds")
        for name in (
            "spot_fee_rate",
            "perp_fee_rate",
            "slippage_bps",
            "borrow_rate_ann",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ReplayInputError(f"{name} must be finite and non-negative")


@dataclass
class _OpenPosition:
    entry_timestamp: pd.Timestamp
    spot_entry_price: float
    perp_entry_price: float
    qty: float
    borrow_principal: float
    last_accrual_timestamp: pd.Timestamp


def _costs(
    *,
    qty: float,
    spot_price: float,
    perp_price: float,
    policy: ReplayPolicy,
) -> tuple[float, float, float, float]:
    spot_notional = qty * spot_price
    perp_notional = qty * perp_price
    spot_fee = spot_notional * policy.spot_fee_rate
    perp_fee = perp_notional * policy.perp_fee_rate
    spot_slippage = spot_notional * policy.slippage_bps / 10_000.0
    perp_slippage = perp_notional * policy.slippage_bps / 10_000.0
    return spot_fee, perp_fee, spot_slippage, perp_slippage


def _percentile_summary(values: np.ndarray) -> dict[str, float]:
    if len(values) == 0:
        raise ReplayInputError("cannot summarize empty values")
    return {
        "min": float(np.min(values)),
        "p10": float(np.percentile(values, 10)),
        "median": float(np.median(values)),
        "p90": float(np.percentile(values, 90)),
        "p95": float(np.percentile(values, 95)),
        "p99": float(np.percentile(values, 99)),
        "max": float(np.max(values)),
    }


def basis_summary(prices: pd.DataFrame) -> dict[str, Any]:
    basis = (prices["perp_price"] - prices["spot_price"]).to_numpy(dtype=float)
    basis_pct = (prices["perp_price"] / prices["spot_price"] - 1.0).to_numpy(dtype=float)
    changes = np.diff(basis)
    return {
        "basis_abs": _percentile_summary(basis),
        "basis_pct": _percentile_summary(basis_pct),
        "basis_change": _percentile_summary(changes) if len(changes) else {},
        "positive_basis_pct": float((basis > 0).mean()),
        "negative_basis_pct": float((basis < 0).mean()),
        "zero_basis_pct": float((basis == 0).mean()),
    }


def _daily_sharpe(equity: pd.Series) -> float | None:
    daily = equity.resample("1D").last().dropna().pct_change().dropna()
    if len(daily) < 2 or float(daily.std(ddof=1)) == 0:
        return None
    return float(np.sqrt(365.0) * daily.mean() / daily.std(ddof=1))


def _cycle_basis_ratio(cycles: list[dict[str, Any]]) -> dict[str, float | None]:
    completed = [cycle for cycle in cycles if cycle["completed"]]
    if not completed:
        return {
            "completed_positions": 0,
            "gross_abs_spot_pnl": 0.0,
            "gross_abs_perp_pnl": 0.0,
            "net_price_pnl": 0.0,
            "residual_ratio": None,
        }
    spot = float(sum(abs(cycle["spot_price_pnl"]) for cycle in completed))
    perp = float(sum(abs(cycle["perp_price_pnl"]) for cycle in completed))
    net = float(sum(cycle["net_price_pnl"] for cycle in completed))
    denominator = (spot + perp) / 2.0
    return {
        "completed_positions": len(completed),
        "gross_abs_spot_pnl": spot,
        "gross_abs_perp_pnl": perp,
        "net_price_pnl": net,
        "residual_ratio": abs(net) / denominator if denominator else 0.0,
    }


def replay_policy(frame: pd.DataFrame, policy: ReplayPolicy) -> dict[str, Any]:
    """Replay one fixed policy with causal prices and explicit equity identity."""

    if frame.empty:
        raise ReplayInputError("replay frame is empty")
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    rates = pd.Series(frame["native_rate"].to_numpy(dtype=float), index=timestamps)
    smoothing = smooth_funding_events(rates, smooth_days=policy.smooth_days, funding_interval="1h")
    capital = float(policy.capital)
    realized_spot = 0.0
    realized_perp = 0.0
    funding_total = 0.0
    borrow_total = 0.0
    fees_total = 0.0
    slippage_total = 0.0
    position: _OpenPosition | None = None
    cycles: list[dict[str, Any]] = []
    current_cycle: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    entry_edges: list[dict[str, float]] = []

    for row_number, (_, row) in enumerate(frame.reset_index(drop=True).iterrows()):
        timestamp = pd.Timestamp(row["timestamp"])
        spot_price = float(row["spot_price"])
        perp_price = float(row["perp_price"])
        funding_rate = float(row["native_rate"])
        reference_price = float(row["reference_price"])
        event_funding = 0.0
        event_borrow = 0.0
        event_spot_fee = 0.0
        event_perp_fee = 0.0
        event_spot_slippage = 0.0
        event_perp_slippage = 0.0
        spot_mark_pnl = 0.0
        perp_mark_pnl = 0.0
        if position is not None:
            elapsed = (timestamp - position.last_accrual_timestamp).total_seconds()
            if elapsed <= 0:
                raise ReplayInputError("replay timestamps must advance")
            event_funding = position.qty * reference_price * funding_rate
            event_borrow = (
                position.borrow_principal * policy.borrow_rate_ann * elapsed / SECONDS_PER_YEAR
            )
            spot_mark_pnl = position.qty * (spot_price - position.spot_entry_price)
            perp_mark_pnl = position.qty * (position.perp_entry_price - perp_price)
            position.last_accrual_timestamp = timestamp

        funding_total += event_funding
        borrow_total += event_borrow
        smooth_ann = float(smoothing.annualized.iloc[row_number])
        decision = decide_carry_payment(
            in_position=position is not None,
            smooth_ann=smooth_ann,
            enter_ann=policy.enter_ann,
            exit_ann=policy.exit_ann,
        )
        action = str(decision.action)
        if position is not None and action == "CLOSE":
            realized_spot += spot_mark_pnl
            realized_perp += perp_mark_pnl
            (
                spot_fee,
                perp_fee,
                spot_slippage,
                perp_slippage,
            ) = _costs(
                qty=position.qty, spot_price=spot_price, perp_price=perp_price, policy=policy
            )
            event_spot_fee += spot_fee
            event_perp_fee += perp_fee
            event_spot_slippage += spot_slippage
            event_perp_slippage += perp_slippage
            fees_total += spot_fee + perp_fee
            slippage_total += spot_slippage + perp_slippage
            if current_cycle is None:
                raise ReplayInputError("close without current cycle")
            current_cycle.update(
                {
                    "exit_timestamp": timestamp.isoformat(),
                    "spot_price_pnl": spot_mark_pnl,
                    "perp_price_pnl": perp_mark_pnl,
                    "net_price_pnl": spot_mark_pnl + perp_mark_pnl,
                    "completed": True,
                    "holding_seconds": (
                        timestamp - pd.Timestamp(current_cycle["entry_timestamp"])
                    ).total_seconds(),
                }
            )
            cycles.append(current_cycle)
            current_cycle = None
            position = None
            spot_mark_pnl = 0.0
            perp_mark_pnl = 0.0
        elif position is None and action == "OPEN":
            equity_before_entry = (
                capital
                + realized_spot
                + realized_perp
                + funding_total
                - borrow_total
                - fees_total
                - slippage_total
            )
            reference_entry_price = (spot_price + perp_price) / 2.0
            qty = equity_before_entry * policy.leverage / reference_entry_price
            if not math.isfinite(qty) or qty <= 0:
                raise ReplayInputError("entry quantity is invalid")
            (
                spot_fee,
                perp_fee,
                spot_slippage,
                perp_slippage,
            ) = _costs(qty=qty, spot_price=spot_price, perp_price=perp_price, policy=policy)
            event_spot_fee += spot_fee
            event_perp_fee += perp_fee
            event_spot_slippage += spot_slippage
            event_perp_slippage += perp_slippage
            fees_total += spot_fee + perp_fee
            slippage_total += spot_slippage + perp_slippage
            position = _OpenPosition(
                entry_timestamp=timestamp,
                spot_entry_price=spot_price,
                perp_entry_price=perp_price,
                qty=qty,
                borrow_principal=equity_before_entry * (policy.leverage - 1.0),
                last_accrual_timestamp=timestamp,
            )
            current_cycle = {
                "entry_timestamp": timestamp.isoformat(),
                "spot_entry_price": spot_price,
                "perp_entry_price": perp_price,
                "qty": qty,
                "borrow_principal": position.borrow_principal,
                "completed": False,
            }
            entry_edges.append(
                {
                    "timestamp": timestamp.timestamp(),
                    "smooth_ann": smooth_ann,
                    "expected_recurring_net_on_equity": (
                        policy.leverage * smooth_ann
                        - (policy.leverage - 1.0) * policy.borrow_rate_ann
                    ),
                }
            )
        if position is not None:
            spot_mark_pnl = position.qty * (spot_price - position.spot_entry_price)
            perp_mark_pnl = position.qty * (position.perp_entry_price - perp_price)
        spot_total = realized_spot + spot_mark_pnl
        perp_total = realized_perp + perp_mark_pnl
        total_pnl = (
            spot_total + perp_total + funding_total - borrow_total - fees_total - slippage_total
        )
        equity = capital + total_pnl
        residual = equity - (capital + total_pnl)
        records.append(
            {
                "timestamp": timestamp.isoformat(),
                "action": action,
                "in_position": position is not None,
                "smooth_ann": smooth_ann,
                "spot_price": spot_price,
                "perp_price": perp_price,
                "spot_price_pnl": spot_total,
                "perp_price_pnl": perp_total,
                "net_price_pnl": spot_total + perp_total,
                "funding_pnl": funding_total,
                "borrow_cost": borrow_total,
                "fees": fees_total,
                "slippage": slippage_total,
                "total_pnl": total_pnl,
                "equity": equity,
                "identity_residual": residual,
                "event_funding_pnl": event_funding,
                "event_borrow_cost": event_borrow,
                "event_fees": event_spot_fee + event_perp_fee,
                "event_slippage": event_spot_slippage + event_perp_slippage,
            }
        )

    if current_cycle is not None:
        current_cycle = dict(current_cycle)
        current_cycle["mark_timestamp"] = timestamps[-1].isoformat()
        current_cycle["spot_price_pnl"] = records[-1]["spot_price_pnl"] - realized_spot
        current_cycle["perp_price_pnl"] = records[-1]["perp_price_pnl"] - realized_perp
        current_cycle["net_price_pnl"] = (
            current_cycle["spot_price_pnl"] + current_cycle["perp_price_pnl"]
        )
        current_cycle["holding_seconds"] = (
            timestamps[-1] - pd.Timestamp(current_cycle["entry_timestamp"])
        ).total_seconds()
        cycles.append(current_cycle)

    terminal_position_open = position is not None
    strategy_mark_to_market_equity = float(records[-1]["equity"])
    if position is not None:
        (
            terminal_spot_fee,
            terminal_perp_fee,
            terminal_spot_slippage,
            terminal_perp_slippage,
        ) = _costs(
            qty=position.qty,
            spot_price=float(records[-1]["spot_price"]),
            perp_price=float(records[-1]["perp_price"]),
            policy=policy,
        )
    else:
        terminal_spot_fee = terminal_perp_fee = 0.0
        terminal_spot_slippage = terminal_perp_slippage = 0.0
    terminal_exit_costs = (
        terminal_spot_fee + terminal_perp_fee + terminal_spot_slippage + terminal_perp_slippage
    )
    hypothetical_terminal_liquidation_equity = strategy_mark_to_market_equity - terminal_exit_costs

    equity_series = pd.Series(
        [record["equity"] for record in records],
        index=pd.DatetimeIndex([record["timestamp"] for record in records]),
    )
    elapsed_seconds = (timestamps[-1] - timestamps[0]).total_seconds()
    completed = [cycle for cycle in cycles if cycle["completed"]]
    durations = [cycle["holding_seconds"] / 86_400.0 for cycle in completed]
    max_equity = equity_series.cummax()
    drawdown = (equity_series / max_equity - 1.0).min()
    years = elapsed_seconds / SECONDS_PER_YEAR
    positive_entries = sum(
        1 for edge in entry_edges if edge["expected_recurring_net_on_equity"] > 0
    )
    return {
        "policy": asdict(policy),
        "terminal_position_open": terminal_position_open,
        "terminal_position_marked_to_market": terminal_position_open,
        "terminal_exit_costs_included": False,
        "terminal_valuation": {
            "strategy_mark_to_market_equity": strategy_mark_to_market_equity,
            "hypothetical_terminal_liquidation_value_if_closed_at_final_mark": hypothetical_terminal_liquidation_equity,
            "exit_spot_fee": float(terminal_spot_fee),
            "exit_perp_fee": float(terminal_perp_fee),
            "exit_spot_slippage": float(terminal_spot_slippage),
            "exit_perp_slippage": float(terminal_perp_slippage),
            "hypothetical_exit_costs": float(terminal_exit_costs),
            "is_strategy_exit": False,
        },
        "entries": sum(1 for record in records if record["action"] == "OPEN"),
        "exits": sum(1 for record in records if record["action"] == "CLOSE"),
        "exposure_fraction": float(
            sum(cycle["holding_seconds"] for cycle in cycles) / elapsed_seconds
            if elapsed_seconds
            else 0.0
        ),
        "holding_period_days": {
            "count": len(durations),
            "median": float(np.median(durations)) if durations else None,
            "mean": float(np.mean(durations)) if durations else None,
        },
        "total_return": float(equity_series.iloc[-1] / capital - 1.0),
        "cagr": float((equity_series.iloc[-1] / capital) ** (1.0 / years) - 1.0)
        if years > 0 and equity_series.iloc[-1] > 0
        else None,
        "sharpe": _daily_sharpe(equity_series),
        "max_drawdown": float(drawdown),
        "pnl": {
            "spot_price_pnl": float(records[-1]["spot_price_pnl"]),
            "perp_price_pnl": float(records[-1]["perp_price_pnl"]),
            "net_price_basis_pnl": float(records[-1]["net_price_pnl"]),
            "funding_pnl": float(funding_total),
            "borrow_cost": float(borrow_total),
            "fees": float(fees_total),
            "slippage": float(slippage_total),
            "total": float(records[-1]["total_pnl"]),
        },
        "final_equity": float(records[-1]["equity"]),
        "identity_residual_max_abs": float(
            max(abs(record["identity_residual"]) for record in records)
        ),
        "entry_edge": {
            "entries_with_smooth_above_threshold": len(entry_edges),
            "net_recurring_positive": positive_entries,
            "net_recurring_non_positive": len(entry_edges) - positive_entries,
            "net_recurring_positive_fraction": (
                positive_entries / len(entry_edges) if entry_edges else None
            ),
            "observations": entry_edges,
        },
        "basis_hedge": _cycle_basis_ratio(cycles),
        "cycles": cycles,
        "records": records,
    }


def sensitivity(
    frame: pd.DataFrame,
    policy: ReplayPolicy,
    *,
    borrow_rates: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20),
    slippage_bps: tuple[float, ...] = (0.0, 5.0, 10.0, 20.0),
) -> dict[str, Any]:
    return {
        "fixed_borrow_sensitivity": {
            f"{rate:.0%}": replay_policy(frame, replace(policy, borrow_rate_ann=rate))["pnl"]
            for rate in borrow_rates
        },
        "slippage_sensitivity": {
            f"{bps:g}bps": replay_policy(frame, replace(policy, slippage_bps=bps))["pnl"]
            for bps in slippage_bps
        },
    }
