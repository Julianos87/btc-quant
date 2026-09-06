"""Governed Trend strategy discovery and public Carry data qualification.

This is deliberately a research-only companion to ``run_research_benchmark``.
The previously observed 2026 holdout is reported only as a consumed diagnostic;
candidate selection uses nested temporal folds ending before that interval.
No live connector, wallet or PAPER state is imported or mutated.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_research_benchmark as reference  # noqa: E402
from btcquant.backtest.engine import _BacktestState  # noqa: E402
from btcquant.domain import funding_amount  # noqa: E402
from btcquant.domain.execution import MarketOrder, OrderSide  # noqa: E402
from btcquant.research.governance import ExperimentSpec, sha256_canonical  # noqa: E402
from btcquant.research.governance_store import GovernanceStore  # noqa: E402
from btcquant.risk import KillSwitch  # noqa: E402
from btcquant.strategies.base import Direction, Position, Strategy  # noqa: E402
from btcquant.strategies.trend_ls import TrendLS  # noqa: E402


LEGACY_HOLDOUT_START = pd.Timestamp("2026-01-01", tz="UTC")
DEVELOPMENT_START = pd.Timestamp("2020-01-01", tz="UTC")
DISCOVERY_END = LEGACY_HOLDOUT_START


class DiscoveryTrend(TrendLS):
    """TrendLS with explicit research-only switches for component ablations."""

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            **TrendLS.default_params(),
            "regime_mode": "ema",
            "signal_mode": "channel",
            "exit_mode": "ema",
            "stop_mode": "atr",
            "fixed_stop_pct": 0.08,
            "sizing_mode": "adaptive",
        }

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self.regime_mode = str(self.params["regime_mode"])
        self.signal_mode = str(self.params["signal_mode"])
        self.exit_mode = str(self.params["exit_mode"])
        self.stop_mode = str(self.params["stop_mode"])
        self.sizing_mode = str(self.params["sizing_mode"])
        if self.regime_mode not in {"ema", "none"}:
            raise ValueError("unsupported research regime mode")
        if self.signal_mode not in {"channel", "ema_cross"}:
            raise ValueError("unsupported research signal mode")
        if self.exit_mode not in {"ema", "channel"}:
            raise ValueError("unsupported research exit mode")
        if self.stop_mode not in {"atr", "none", "fixed"}:
            raise ValueError("unsupported research stop mode")
        if self.sizing_mode not in {"adaptive", "simple"}:
            raise ValueError("unsupported research sizing mode")

    def _filter_direction(self, row: pd.Series, direction: int) -> int:
        p = self.params
        if p["adx_min"] is not None and not (
            pd.notna(row.get("adx")) and float(row["adx"]) >= float(p["adx_min"])
        ):
            return 0
        funding = row.get("funding")
        if direction == 1 and p["funding_long_max"] is not None:
            if pd.notna(funding) and float(funding) > float(p["funding_long_max"]):
                return 0
        if direction == -1 and p["funding_short_min"] is not None:
            if pd.notna(funding) and float(funding) < float(p["funding_short_min"]):
                return 0
        return direction

    def entry_signal(self, row: pd.Series) -> int:
        if self.signal_mode == "ema_cross":
            if pd.isna(row.get("ema_fast")) or pd.isna(row.get("ema_slow")):
                return 0
            direction = 1 if float(row["ema_fast"]) > float(row["ema_slow"]) else -1
            return self._filter_direction(row, direction)
        if self.regime_mode == "ema":
            return super().entry_signal(row)
        if pd.isna(row.get("atr")) or pd.isna(row.get("donchian_high")):
            return 0
        buffer = float(self.params["entry_buffer_bps"]) / 10_000.0
        atr_buffer = float(self.params["entry_buffer_atr"]) * float(row["atr"])
        if float(row["close"]) > float(row["donchian_high"]) * (1.0 + buffer) + atr_buffer:
            return self._filter_direction(row, 1)
        if float(row["close"]) < float(row["donchian_low"]) * (1.0 - buffer) - atr_buffer:
            return self._filter_direction(row, -1)
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        if self.stop_mode == "fixed":
            return entry_price * (1.0 - direction * float(self.params["fixed_stop_pct"]))
        return super().initial_stop(row, entry_price, direction)

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        if self.stop_mode == "none":
            return None
        if self.stop_mode == "fixed":
            pct = float(self.params["fixed_stop_pct"])
            return position.best_close * (1.0 - position.direction * pct)
        return super().trailing_stop(row, position)

    def position_size_multiplier(self, row: pd.Series, direction: int) -> float:
        if self.sizing_mode == "simple":
            return 1.0
        return super().position_size_multiplier(row, direction)

    def pyramid_fraction(self, row: pd.Series, position: Position) -> float:
        if self.params["pyramid_atr_step"] is None:
            return 0.0
        return super().pyramid_fraction(row, position)

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        if self.exit_mode == "ema":
            return super().exit_signal(row, position)
        if pd.isna(row.get("donchian_high")) or pd.isna(row.get("donchian_low")):
            return False
        if position.direction == 1:
            return float(row["close"]) < float(row["donchian_low"])
        return float(row["close"]) > float(row["donchian_high"])


DEPLOYED = {
    "ema_fast": 50,
    "ema_slow": 200,
    "donchian": 55,
    "atr_period": 14,
    "atr_mult": 3.0,
    "adx_period": 14,
    "adx_min": 20,
    "funding_long_max": 0.0008,
    "funding_short_min": -0.0008,
    "funding_sizing_threshold": None,
    "funding_sizing_floor": 0.25,
    "entry_buffer_bps": 0.0,
    "entry_buffer_atr": 0.0,
    "strong_trend_adx": None,
    "strong_trend_atr_mult": None,
    "pyramid_atr_step": 0.5,
    "pyramid_add_fraction": 0.30,
    "pyramid_max_adds": 1,
    "adaptive_regime_enabled": True,
    "adaptive_efficiency_bars": 30,
    "adaptive_volatility_bars": 30,
    "adaptive_reference_bars": 540,
    "adaptive_smoothing_span": 12,
    "adaptive_min_multiplier": 0.50,
    "adaptive_max_multiplier": 1.00,
    "adaptive_volatility_shock_ratio": 2.00,
    "regime_mode": "ema",
    "signal_mode": "channel",
    "exit_mode": "ema",
    "stop_mode": "atr",
    "fixed_stop_pct": 0.08,
    "sizing_mode": "adaptive",
}


class DiscoveryEngine(reference.BacktestEngine):
    """Research-only engine switches that preserve all other engine semantics."""

    def __init__(
        self, *, disable_stops: bool = False, fixed_exposure_pct: float | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.disable_stops = disable_stops
        self.fixed_exposure_pct = fixed_exposure_pct

    def _open_pending(  # type: ignore[override]
        self,
        state: _BacktestState,
        execution: Any,
        strategy: Strategy,
        kill: KillSwitch,
        ts: pd.Timestamp,
        open_price: float,
        available_volume: float,
    ) -> None:
        if self.fixed_exposure_pct is None:
            return super()._open_pending(
                state, execution, strategy, kill, ts, open_price, available_volume
            )
        pending = state.pending_entry
        state.pending_entry = None
        if state.position is not None or pending is None or not kill.can_trade:
            return
        signal_row, direction = pending
        side = OrderSide.BUY if direction == 1 else OrderSide.SELL
        volatility = signal_row.get("_rvol")
        vol_annual = float(volatility) if volatility is not None and pd.notna(volatility) else None
        quoted = execution.quote_price(side, float(open_price), volatility_annual=vol_annual)
        qty = (
            (state.cash + (state.position.unrealized(open_price) if state.position else 0.0))
            * float(self.fixed_exposure_pct)
            / quoted
        )
        qty = min(qty, state.cash * self.risk.max_position_pct * self.risk.max_leverage / quoted)
        if direction == -1:
            qty *= self.short_size_mult
        if qty <= 0:
            return
        fill = execution.execute_market(
            MarketOrder(
                order_id=f"discovery:{strategy.name}:{ts.isoformat()}:fixed-entry:{direction}",
                side=side,
                qty=qty,
                reference_price=float(open_price),
                available_volume=float(available_volume),
                volatility_annual=vol_annual,
            )
        )
        if fill.qty <= 0:
            return
        stop = strategy.initial_stop(signal_row, fill.price, direction)
        state.entry_fee_pending = fill.fee
        state.cash -= fill.fee
        state.position = Position(
            entry_time=ts,
            entry_price=fill.price,
            qty=fill.qty,
            stop_price=stop,
            direction=Direction(direction),
            best_close=fill.price,
        )

    def _process_intrabar(  # type: ignore[override]
        self,
        state: _BacktestState,
        execution: Any,
        strategy_name: str,
        ts: pd.Timestamp,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        available_volume: float,
        funding_rate: float,
        volatility_annual: float | None,
    ) -> None:
        if not self.disable_stops:
            return super()._process_intrabar(
                state,
                execution,
                strategy_name,
                ts,
                open_price,
                high_price,
                low_price,
                close_price,
                available_volume,
                funding_rate,
                volatility_annual,
            )
        if state.position is not None:
            state.cash -= funding_amount(state.position, funding_rate, float(close_price))


def _load_frame() -> pd.DataFrame:
    base = reference.load_ohlcv(
        "binance", "BTC/USDT", "1h", "2019-01-01", data_dir=ROOT / "data", refresh=False
    )
    funding = pd.read_csv(reference.TREND_FUNDING, index_col=0)
    funding.index = pd.to_datetime(funding.index, utc=True, format="mixed")
    funding["rate"] = pd.to_numeric(funding["rate"], errors="raise")
    frame = reference.resample(base, "4h", source_frequency="1h")
    funding = funding.loc[funding.index <= frame.index[-1]]
    return reference.add_funding_columns(frame, funding["rate"], "4h")


def _run(
    frame: pd.DataFrame,
    strategy: Strategy,
    *,
    cost_multiplier: float = 1.0,
    stress: bool = False,
    disable_stops: bool = False,
    fixed_exposure_pct: float | None = None,
) -> Any:
    cfg = reference.deepcopy(reference.load_config(ROOT / "environments" / "paper" / "config.yaml"))
    cfg["costs"]["perp_fee_rate"] = float(cfg["costs"]["perp_fee_rate"]) * cost_multiplier
    cfg["costs"]["slippage_bps"] = float(cfg["costs"]["slippage_bps"]) * cost_multiplier
    cfg["execution"]["simulation"]["profile"] = "stress" if stress else "normal"
    risk = reference._trend_risk()
    return DiscoveryEngine(
        disable_stops=disable_stops,
        fixed_exposure_pct=fixed_exposure_pct,
        risk=risk,
        allow_short=True,
        funding_rate_8h=0.0,
        execution_simulator=reference.ExecutionSimulator(
            reference.execution_config_from_config(cfg, float(cfg["costs"]["perp_fee_rate"]))
        ),
    ).run(strategy, frame)


def _slice_metrics(result: Any, start: pd.Timestamp, end: pd.Timestamp | None) -> dict[str, Any]:
    equity = reference._slice_equity(result.equity, start, end)
    trades = [
        t for t in result.trades if t.exit_time >= start and (end is None or t.exit_time < end)
    ]
    return reference._metric_payload(equity, trades)


def _variant_params(name: str) -> dict[str, Any]:
    p = dict(DEPLOYED)
    if name == "no_adx":
        p["adx_min"] = None
    elif name == "no_pyramiding":
        p["pyramid_atr_step"] = None
        p["pyramid_max_adds"] = 0
    elif name == "no_ema_regime":
        p["regime_mode"] = "none"
    elif name == "no_funding_filter":
        p["funding_long_max"] = None
        p["funding_short_min"] = None
    elif name == "no_adaptive_sizing":
        p["adaptive_regime_enabled"] = False
        p["sizing_mode"] = "simple"
    elif name == "fixed_stop_8pct":
        p["stop_mode"] = "fixed"
    elif name == "donchian_only":
        p.update(
            {
                "regime_mode": "none",
                "adx_min": None,
                "funding_long_max": None,
                "funding_short_min": None,
                "adaptive_regime_enabled": False,
                "sizing_mode": "simple",
                "pyramid_atr_step": None,
                "pyramid_max_adds": 0,
                "exit_mode": "channel",
            }
        )
    elif name == "channel_exit":
        p["exit_mode"] = "channel"
    elif name == "ema_signal_btcquant_rm":
        p.update(
            {
                "signal_mode": "ema_cross",
                "adx_min": None,
                "funding_long_max": None,
                "funding_short_min": None,
                "adaptive_regime_enabled": False,
                "sizing_mode": "simple",
                "pyramid_atr_step": None,
                "pyramid_max_adds": 0,
            }
        )
    elif name == "simple_sizing":
        p["sizing_mode"] = "simple"
        p["adaptive_regime_enabled"] = False
    elif name == "no_atr_stop":
        p["stop_mode"] = "none"
    elif name == "full":
        pass
    else:
        raise ValueError(f"unknown ablation {name}")
    return p


ABLATIONS = (
    "full",
    "no_adx",
    "no_pyramiding",
    "no_atr_stop",
    "fixed_stop_8pct",
    "no_ema_regime",
    "no_funding_filter",
    "no_adaptive_sizing",
    "simple_sizing",
    "fixed_exposure_50pct",
    "donchian_only",
    "channel_exit",
    "ema_signal_btcquant_rm",
)


def _make_strategy(name: str, params: Mapping[str, Any] | None = None) -> DiscoveryTrend:
    p = (
        dict(params)
        if params is not None
        else _variant_params("simple_sizing" if name == "fixed_exposure_50pct" else name)
    )
    if name == "fixed_exposure_50pct":
        p = _variant_params("simple_sizing")
    strategy = DiscoveryTrend(**p)
    strategy.name = name
    return strategy


def _ablation_spec(manifest: dict[str, Any], seed: int, budget: int) -> ExperimentSpec:
    base = reference._trend_spec(manifest, seed, budget)
    return replace(
        base,
        experiment_id="trend-discovery-ablations-20260906",
        created_at=datetime.now(UTC).isoformat(),
        parameter_space={"ablations": list(ABLATIONS)},
        split_policy={
            "type": "expanding",
            "shuffle": False,
            "search_end": DISCOVERY_END.isoformat(),
        },
        holdout_policy={
            "interval": "2026-01-01..dataset_end",
            "sealed": True,
            "used_for_selection": False,
            "legacy_consumed": True,
        },
        code_provenance={
            "script": "scripts/run_strategy_discovery.py",
            "commit": reference._git("rev-parse", "HEAD"),
        },
        candidate_selection_rule="ablation attribution only; no legacy holdout selection",
        maximum_trial_budget=budget,
    )


def run_ablations(
    frame: pd.DataFrame, manifest: dict[str, Any], seed: int, output_dir: Path
) -> dict[str, Any]:
    development = frame[(frame.index >= DEVELOPMENT_START) & (frame.index < DISCOVERY_END)]
    split_defs = {
        "train": (DEVELOPMENT_START, pd.Timestamp("2023-01-01", tz="UTC")),
        "validation": (pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC")),
        "oos": (pd.Timestamp("2024-01-01", tz="UTC"), DISCOVERY_END),
    }
    store = GovernanceStore(output_dir / "ablation_governance.sqlite3")
    spec = _ablation_spec(manifest, seed, 256)
    dataset_fp = sha256_canonical(
        {"manifest": manifest["datasets"], "legacy_holdout_consumed": True}
    )
    split_fp = sha256_canonical(
        {"splits": {k: (a.isoformat(), b.isoformat()) for k, (a, b) in split_defs.items()}}
    )
    rows: list[dict[str, Any]] = []
    full_metrics: dict[str, Any] | None = None
    for name in ABLATIONS:
        strategy = _make_strategy(name)
        result = _run(
            development,
            strategy,
            disable_stops=name == "no_atr_stop",
            fixed_exposure_pct=0.50 if name == "fixed_exposure_50pct" else None,
        )
        metrics = {
            split: _slice_metrics(result, start, end) for split, (start, end) in split_defs.items()
        }
        metrics["legacy_consumed_holdout"] = _slice_metrics(result, LEGACY_HOLDOUT_START, None)
        if name == "full":
            full_metrics = metrics
        outcome = {
            "metrics": {"oos_sharpe": float(metrics["oos"].get("sharpe") or 0.0)},
            "metrics_by_split": metrics,
            "variant": name,
        }
        durable = store.execute_trial(
            spec,
            {"variant": name},
            dataset_fingerprint=dataset_fp,
            split_fingerprint=split_fp,
            evaluator=lambda _reservation, payload=outcome: payload,
        )
        rows.append(
            {"variant": name, "parameters": strategy.params, "metrics": durable["metrics_by_split"]}
        )
    assert full_metrics is not None
    full_oos = full_metrics["oos"]
    for row in rows:
        oos = row["metrics"]["oos"]
        row["marginal_effect_vs_full"] = {
            "return_delta": float(
                (oos.get("total_return") or 0.0) - (full_oos.get("total_return") or 0.0)
            ),
            "sharpe_delta": float((oos.get("sharpe") or 0.0) - (full_oos.get("sharpe") or 0.0)),
            "max_drawdown_delta": float(
                (oos.get("max_drawdown") or 0.0) - (full_oos.get("max_drawdown") or 0.0)
            ),
            "turnover_delta": float(
                (oos.get("turnover") or 0.0) - (full_oos.get("turnover") or 0.0)
            ),
        }
        d = row["marginal_effect_vs_full"]
        signs = [
            float(row["metrics"][s].get("sharpe") or 0.0)
            - float(full_metrics[s].get("sharpe") or 0.0)
            for s in ("train", "validation", "oos")
        ]
        if row["variant"] == "full":
            classification = "NEUTRAL"
        elif row["metrics"]["oos"].get("observations", 0) < 180:
            classification = "INSUFFICIENT_EVIDENCE"
        elif max(signs) > 0.05 and min(signs) < -0.05:
            classification = "REGIME_DEPENDENT"
        elif d["sharpe_delta"] > 0.05 and d["max_drawdown_delta"] >= -0.02:
            classification = "POSITIVE"
        elif d["sharpe_delta"] < -0.05 and d["max_drawdown_delta"] < 0.02:
            classification = "NEGATIVE"
        else:
            classification = "NEUTRAL"
        row["component_value"] = classification
    return {
        "legacy_holdout_consumed": True,
        "development_window": {
            "start": DEVELOPMENT_START.isoformat(),
            "end": DISCOVERY_END.isoformat(),
        },
        "store": str(output_dir / "ablation_governance.sqlite3"),
        "trials": len(rows),
        "rows": rows,
    }


def _candidate_space() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for donchian in (55, 100):
        for atr_mult in (2.5, 3.0):
            candidates.append(
                {"family": "donchian_simple", "donchian": donchian, "atr_mult": atr_mult}
            )
    for fast, slow in ((30, 150), (50, 200)):
        for atr_mult in (2.5, 3.0):
            candidates.append(
                {"family": "ema_vol", "fast": fast, "slow": slow, "atr_mult": atr_mult}
            )
    return candidates


def _candidate_strategy(parameters: Mapping[str, Any]) -> DiscoveryTrend:
    p = dict(DEPLOYED)
    p.update(
        {
            "adx_min": None,
            "funding_long_max": None,
            "funding_short_min": None,
            "adaptive_regime_enabled": False,
            "sizing_mode": "simple",
            "pyramid_atr_step": None,
            "pyramid_max_adds": 0,
            "exit_mode": "ema",
        }
    )
    if parameters["family"] == "donchian_simple":
        p.update(
            {
                "donchian": int(parameters["donchian"]),
                "atr_mult": float(parameters["atr_mult"]),
                "regime_mode": "none",
                "signal_mode": "channel",
            }
        )
    elif parameters["family"] == "ema_vol":
        p.update(
            {
                "ema_fast": int(parameters["fast"]),
                "ema_slow": int(parameters["slow"]),
                "atr_mult": float(parameters["atr_mult"]),
                "signal_mode": "ema_cross",
            }
        )
    else:
        raise ValueError("unknown candidate family")
    return DiscoveryTrend(**p)


def _nested_spec(
    manifest: dict[str, Any], seed: int, budget: int, outer: str, candidates: list[dict[str, Any]]
) -> ExperimentSpec:
    base = reference._trend_spec(manifest, seed, budget)
    return replace(
        base,
        experiment_id=f"trend-discovery-nested-{outer}",
        created_at=datetime.now(UTC).isoformat(),
        parameter_space={"candidates": candidates},
        split_policy={"type": "expanding", "shuffle": False, "search_end": "2026-01-01T00:00:00Z"},
        holdout_policy={
            "interval": "2026-01-01..dataset_end",
            "sealed": True,
            "used_for_selection": False,
            "legacy_consumed": True,
        },
        code_provenance={
            "script": "scripts/run_strategy_discovery.py",
            "commit": reference._git("rev-parse", "HEAD"),
        },
        candidate_selection_rule="inner train Sharpe only; outer test never enters selection",
        maximum_trial_budget=budget,
        multiple_testing_policy={
            "method": "DSR across every inner trial in this campaign",
            "return_sampling_frequency": "1D_UTC",
            "legacy_holdout_consumed": True,
        },
    )


def _outer_folds() -> list[tuple[str, pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    return [
        (
            "2023",
            pd.Timestamp("2023-01-01", tz="UTC"),
            pd.Timestamp("2023-01-01", tz="UTC"),
            pd.Timestamp("2024-01-01", tz="UTC"),
        ),
        (
            "2024",
            pd.Timestamp("2024-01-01", tz="UTC"),
            pd.Timestamp("2024-01-01", tz="UTC"),
            pd.Timestamp("2025-01-01", tz="UTC"),
        ),
        (
            "2025",
            pd.Timestamp("2025-01-01", tz="UTC"),
            pd.Timestamp("2025-01-01", tz="UTC"),
            pd.Timestamp("2026-01-01", tz="UTC"),
        ),
    ]


def run_nested(
    frame: pd.DataFrame, manifest: dict[str, Any], seed: int, output_dir: Path
) -> dict[str, Any]:
    candidates = _candidate_space()
    store = GovernanceStore(output_dir / "nested_governance.sqlite3")
    outer_results: list[dict[str, Any]] = []
    candidate_outer: dict[str, list[dict[str, Any]]] = {sha256_canonical(c): [] for c in candidates}
    total_trials = 0
    for label, train_end, test_start, test_end in _outer_folds():
        train = frame[(frame.index >= DEVELOPMENT_START) & (frame.index < train_end)]
        test = frame[(frame.index >= test_start) & (frame.index < test_end)]
        if len(train) < 1500 or len(test) < 1000:
            raise ValueError(f"outer fold {label} too short")
        spec = _nested_spec(manifest, seed, 512, label, candidates)

        def evaluate(
            parameters: Mapping[str, Any], inner_train: pd.DataFrame, evaluation: pd.DataFrame
        ) -> dict[str, Any]:
            strategy = _candidate_strategy(parameters)
            train_result = _run(inner_train, strategy)
            combined = pd.concat([inner_train.tail(strategy.warmup_bars() + 2), evaluation])
            evaluation_result = _run(combined, strategy)
            eval_eq = reference._slice_equity(
                evaluation_result.equity,
                evaluation.index[0],
                evaluation.index[-1] + pd.Timedelta("1ns"),
            )
            return {
                "sharpe": float(
                    reference._metric_payload(train_result.equity).get("sharpe") or 0.0
                ),
                "evaluation_metrics": reference._metric_payload(eval_eq, evaluation_result.trades),
            }

        wf = reference.governed_walk_forward(
            train,
            candidates,
            spec=spec,
            train_duration=timedelta(days=730),
            evaluation_duration=timedelta(days=365),
            warmup_duration=timedelta(days=180),
            purge_duration=timedelta(days=4),
            embargo_duration=timedelta(0),
            evaluator=evaluate,
            code_sha=reference._git("rev-parse", "HEAD"),
            governance_store=store,
        )
        total_trials += wf.trials_attempted
        selected = asdict(reference._latest_complete_fold(wf.folds, wf.split_definitions, 1800))
        selected_params = selected["selected_parameters"]
        selected_strategy = _candidate_strategy(selected_params)
        combined_outer = pd.concat([train.tail(selected_strategy.warmup_bars() + 2), test])
        selected_result = _run(combined_outer, selected_strategy)
        selected_metric = _slice_metrics(selected_result, test_start, test_end)
        outer_results.append(
            {
                "outer_fold": label,
                "inner_winner": selected_params,
                "inner_folds": [asdict(f) for f in wf.folds],
                "outer_metrics": selected_metric,
            }
        )
        for candidate in candidates:
            strategy = _candidate_strategy(candidate)
            combined = pd.concat([train.tail(strategy.warmup_bars() + 2), test])
            result = _run(combined, strategy)
            candidate_outer[sha256_canonical(candidate)].append(
                {"outer_fold": label, "metrics": _slice_metrics(result, test_start, test_end)}
            )
    candidate_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        key = sha256_canonical(candidate)
        records = candidate_outer[key]
        sharpes = [float(r["metrics"].get("sharpe") or 0.0) for r in records]
        returns = [float(r["metrics"].get("total_return") or 0.0) for r in records]
        selected_count = sum(1 for r in outer_results if r["inner_winner"] == candidate)
        full_result = _run(frame[frame.index < DISCOVERY_END], _candidate_strategy(candidate))
        candidate_rows.append(
            {
                "strategy": candidate,
                "complexity": {
                    "parameters": len(candidate),
                    "indicators": 4 if candidate["family"] == "donchian_simple" else 3,
                    "filters": 0,
                    "configs_tested": 1,
                },
                "inner_selected_outer_folds": selected_count,
                "outer_metrics": records,
                "outer_mean_sharpe": float(np.mean(sharpes)),
                "outer_positive_folds": int(sum(v > 0 for v in sharpes)),
                "outer_mean_return": float(np.mean(returns)),
                "bootstrap_development": reference._bootstrap(
                    reference._slice_equity(
                        full_result.equity, pd.Timestamp("2023-01-01", tz="UTC"), DISCOVERY_END
                    ),
                    seed + len(candidate_rows),
                ),
                "regimes_development": reference._regime_summary(
                    frame,
                    reference._slice_equity(
                        full_result.equity, pd.Timestamp("2023-01-01", tz="UTC"), DISCOVERY_END
                    ),
                ),
            }
        )
    current = _run(frame[frame.index < DISCOVERY_END], _make_strategy("full"))
    current_outer = []
    for label, _, test_start, test_end in _outer_folds():
        current_outer.append(
            {"outer_fold": label, "metrics": _slice_metrics(current, test_start, test_end)}
        )
    all_trial_sharpes: list[float] = []
    rows = store._connection.execute(
        "SELECT metrics_json FROM trials WHERE trial_kind='SEARCH'"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row[0]))
            value = payload.get("sharpe")
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                all_trial_sharpes.append(float(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    current_mean_sharpe = float(
        np.mean([float(row["metrics"].get("sharpe") or 0.0) for row in current_outer])
    )
    # Select the discovery leader by inner selection frequency only. Outer
    # results are a locked diagnostic, never an input to the winner rule.
    leader = max(
        candidates,
        key=lambda item: (
            sum(1 for row in outer_results if row["inner_winner"] == item),
            sha256_canonical(item),
        ),
    )
    stress_frame = frame[
        (frame.index >= pd.Timestamp("2023-01-01", tz="UTC")) & (frame.index < DISCOVERY_END)
    ]
    stress_targets = {
        "current_trend": _make_strategy("full"),
        "discovery_leader": _candidate_strategy(leader),
    }
    stress: dict[str, Any] = {}
    for label, strategy in stress_targets.items():
        stress[label] = {"cost": {}, "execution": None}
        for multiplier in (1.0, 1.25, 1.5, 2.0):
            stressed = _run(stress_frame, strategy, cost_multiplier=multiplier)
            stress[label]["cost"][f"{multiplier:g}x"] = reference._metric_payload(
                stressed.equity, stressed.trades
            )
        stressed = _run(stress_frame, strategy, cost_multiplier=1.5, stress=True)
        stress[label]["execution"] = reference._metric_payload(stressed.equity, stressed.trades)
    leader_result = _run(frame[frame.index < DISCOVERY_END], _candidate_strategy(leader))
    leader_metric = _slice_metrics(
        leader_result, pd.Timestamp("2023-01-01", tz="UTC"), DISCOVERY_END
    )
    skew, kurt, autocorr, nobs, dependence = reference._daily_stats(current.equity)
    dsr = reference.deflated_sharpe_ratio(
        observed_sharpe=float(leader_metric.get("sharpe") or float("nan")),
        trial_sharpes=all_trial_sharpes,
        raw_attempted_trials=max(2, total_trials),
        n_observations=int(nobs) if math.isfinite(nobs) else 0,
        skewness=skew,
        raw_kurtosis=kurt,
        return_sampling_frequency="1D_UTC",
        sharpe_scale=reference.SharpeScale.ANNUALIZED,
        periods_per_year=365.0,
        dependence_status=dependence,
    )
    for row in candidate_rows:
        selected = int(row["inner_selected_outer_folds"])
        robust = (
            selected >= 2
            and row["outer_positive_folds"] >= 2
            and row["outer_mean_sharpe"] > current_mean_sharpe + 0.10
            and float(stress["discovery_leader"]["cost"]["1.5x"].get("sharpe") or -1.0) > 0.0
            and float(row["bootstrap_development"].get("probability_positive") or 0.0) >= 0.60
        )
        row["classification"] = (
            "RESEARCH_CANDIDATE"
            if robust
            else ("BASELINE" if selected == 0 and row["outer_positive_folds"] >= 2 else "REJECTED")
        )
    return {
        "legacy_holdout_consumed": True,
        "outer_folds": outer_results,
        "candidate_leaderboard": candidate_rows,
        "current_trend_outer": current_outer,
        "current_mean_outer_sharpe": current_mean_sharpe,
        "discovery_leader_by_inner": leader,
        "stress": stress,
        "multiple_testing": {
            "method": "Deflated Sharpe Ratio",
            "raw_attempted_trials": total_trials,
            "valid_trial_sharpes": len(all_trial_sharpes),
            "dependence_status": dependence,
            "leader_development_metrics": leader_metric,
            "dsr": asdict(dsr),
        },
        "trials_attempted": total_trials,
        "governance_store": str(output_dir / "nested_governance.sqlite3"),
        "all_inner_trial_sharpes": all_trial_sharpes,
    }


def _carry_collector_plan() -> dict[str, Any]:
    return {
        "status": "QUALIFIED_FOR_FUTURE_ACCUMULATION",
        "script": "scripts/acquire_carry_v2_data.py",
        "endpoint": "https://api.hyperliquid.xyz/info",
        "authentication": "none",
        "observed": ["BTC perp candles", "native hourly funding", "UBTC/USDC spot candles"],
        "proxy": [
            "wrapped UBTC/USDC as spot reference",
            "previous completed perp close as funding reference",
        ],
        "assumed": ["borrow rate when replaying until public historical borrow exists"],
        "borrow_status": "BORROW_NOT_OBSERVED",
        "manifest_fields": [
            "source",
            "endpoint",
            "retrieval timestamp",
            "venue timestamp",
            "symbol",
            "rows",
            "gaps",
            "duplicates",
            "transforms",
            "sha256",
        ],
        "future_holdout_start": "2026-09-07T00:00:00Z",
        "collection_rule": "append immutable dated snapshots; never select parameters on the future interval",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260906)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = reference.build_manifest()
    frame = _load_frame()
    output = {
        "version": 1,
        "run_id": f"strategy-discovery-{args.seed}-{reference._git('rev-parse', 'HEAD')[:12]}",
        "source_sha": reference._git("rev-parse", "HEAD"),
        "source_tree": reference._git("rev-parse", "HEAD^{tree}"),
        "legacy_holdout_consumed": True,
        "manifest": manifest,
        "manifest_sha256": sha256_canonical(manifest),
        "ablations": run_ablations(frame, manifest, args.seed, args.output_dir),
        "nested": run_nested(frame, manifest, args.seed, args.output_dir),
        "carry_collector": _carry_collector_plan(),
        "paper_changed": False,
    }
    report = args.output_dir / "strategy_discovery_report.json"
    report.write_text(
        json.dumps(output, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(report),
                "run_id": output["run_id"],
                "ablation_trials": output["ablations"]["trials"],
                "nested_trials": output["nested"]["trials_attempted"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
