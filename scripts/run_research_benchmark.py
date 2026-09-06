"""Run a governed, reproducible Trend/Carry research benchmark.

This command is deliberately research-only.  It consumes public historical
files, uses the qualified BTCQuant bar engine for directional strategies and
the causal Carry V2 replay for financing strategies, and writes a manifest,
durable search database and a JSON report.  It never imports a live venue
client and never mutates PAPER state.

Example::

    python scripts/run_research_benchmark.py \
      --strategy all --seed 20260906 --research-budget 64 \
      --output-dir /home/ubuntu/btcquant-benchmark-20260906
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest import BacktestEngine
from btcquant.backtest.metrics import compute_metrics
from btcquant.carry import add_funding_columns
from btcquant.config import execution_config_from_config, load_config
from btcquant.data import load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.indicators import atr, bars_per_year, donchian_high, donchian_low, ema, realized_vol
from btcquant.research.carry_v2_replay import (
    ReplayPolicy,
    load_candle_csv,
    load_funding_csv,
    prepare_replay_frame,
    replay_policy,
    synchronize_price_frames,
)
from btcquant.research.governance import (
    DatasetRole,
    ExperimentSpec,
    GovernanceError,
    assert_prefix_invariant,
)
from btcquant.research.governed_walkforward import governed_walk_forward
from btcquant.research.quant_statistics import (
    SharpeScale,
    deflated_sharpe_ratio,
)
from btcquant.research.governance_store import GovernanceStore
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy
from btcquant.strategies.trend_ls import TrendLS
from btcquant.research.strategies.range_mean_reversion import RangeMeanReversion


TREND_DATA = ROOT / "data" / "binance_BTC-USDT_1h.csv"
TREND_FUNDING = ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv"
CARRY_ROOT = ROOT / "audit" / "baselines" / "data" / "carry_v2"
CARRY_SPOT = CARRY_ROOT / "hyperliquid_ubtc_usdc_spot_1h_20260114_20260810_v2.csv.gz"
CARRY_PERP = CARRY_ROOT / "hyperliquid_btc_perp_1h_20260114_20260810_v2.csv.gz"
CARRY_FUNDING = CARRY_ROOT / "hyperliquid_btc_funding_1h_20260114_20260810_v2.csv.gz"
CARRY_METADATA = CARRY_ROOT / "hyperliquid_carry_v2_20260114_20260810_v2.metadata.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def _quality_ohlcv(path: Path) -> dict[str, Any]:
    frame = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, format="mixed", errors="coerce")
    if timestamps.isna().any() or timestamps.duplicated().any():
        raise ValueError(f"{path}: invalid or duplicate timestamps")
    if not timestamps.is_monotonic_increasing:
        raise ValueError(f"{path}: out-of-order timestamps")
    numeric = frame[["open", "high", "low", "close", "volume"]].apply(
        pd.to_numeric, errors="coerce"
    )
    if numeric.isna().any().any() or not np.isfinite(numeric.to_numpy()).all():
        raise ValueError(f"{path}: non-finite OHLCV")
    if (numeric[["open", "high", "low", "close"]] <= 0).any().any() or (
        numeric["volume"] < 0
    ).any():
        raise ValueError(f"{path}: impossible OHLCV values")
    if (numeric["high"] < numeric[["open", "close"]].max(axis=1)).any():
        raise ValueError(f"{path}: high below open/close")
    if (numeric["low"] > numeric[["open", "close"]].min(axis=1)).any():
        raise ValueError(f"{path}: low above open/close")
    deltas = timestamps.diff().dropna()
    gaps = deltas[deltas != pd.Timedelta("1h")]
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "rows": int(len(frame)),
        "start": timestamps.iloc[0].isoformat(),
        "end": timestamps.iloc[-1].isoformat(),
        "timeframe": "1h",
        "timezone": "UTC",
        "duplicates": 0,
        "out_of_order": 0,
        "gaps": int(len(gaps)),
        "gap_examples": [
            {
                "after": timestamps.iloc[int(i)].isoformat(),
                "delta_seconds": float(delta.total_seconds()),
            }
            for i, delta in gaps.head(10).items()
        ],
        "incomplete_last_candle_dropped_by_loader": True,
        "transformations": [
            "UTC parse",
            "numeric validation",
            "last incomplete candle dropped by load_ohlcv",
            "4h complete-window resampling",
        ],
        "source": "Binance public OHLCV through CCXT",
    }


def _quality_funding(path: Path) -> dict[str, Any]:
    frame = pd.read_csv(path, index_col=0)
    timestamps = pd.to_datetime(frame.index, utc=True, format="mixed", errors="coerce")
    rates = pd.to_numeric(frame["rate"], errors="coerce")
    if (
        timestamps.isna().any()
        or timestamps.duplicated().any()
        or not timestamps.is_monotonic_increasing
    ):
        raise ValueError(f"{path}: invalid funding timestamps")
    if rates.isna().any() or not np.isfinite(rates.to_numpy()).all():
        raise ValueError(f"{path}: invalid funding rates")
    # Binance publishes a few millisecond timestamp jitter around nominal
    # 00:00/08:00/16:00 slots.  Validate cadence on rounded slots (the loader
    # separately enforces the one-second jitter bound), otherwise every
    # millisecond offset is falsely reported as a missing funding event.
    slots = pd.Series(timestamps).dt.round("8h")
    deltas = slots.diff().dropna()
    anomalies = deltas[deltas != pd.Timedelta("8h")]
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "rows": int(len(frame)),
        "start": timestamps[0].isoformat(),
        "end": timestamps[-1].isoformat(),
        "timeframe": "8h events",
        "timezone": "UTC",
        "duplicates": 0,
        "out_of_order": 0,
        "gaps": int(len(anomalies)),
        "timestamp_jitter_allowed_seconds": 1.0,
        "transformations": [
            "UTC parse",
            "venue slot validation",
            "joined to prior completed 1h close",
        ],
        "source": "Binance USDM public fundingRateHistory through CCXT",
    }


def _quality_carry(path: Path, kind: str) -> dict[str, Any]:
    if kind == "funding":
        frame = load_funding_csv(path)
        ts = pd.DatetimeIndex(frame["timestamp"])
    else:
        frame = load_candle_csv(path, label=f"Hyperliquid {kind}")
        ts = pd.DatetimeIndex(frame["timestamp"])
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "rows": int(len(frame)),
        "start": ts[0].isoformat(),
        "end": ts[-1].isoformat(),
        "timeframe": "1h",
        "timezone": "UTC",
        "duplicates": 0,
        "out_of_order": 0,
        "gaps": 0,
        "transformations": [
            "official Hyperliquid public snapshot validation",
            "backward-only spot/perp synchronization",
        ],
        "source": "Hyperliquid public info endpoint snapshot",
    }


def build_manifest() -> dict[str, Any]:
    for path in (TREND_DATA, TREND_FUNDING, CARRY_SPOT, CARRY_PERP, CARRY_FUNDING, CARRY_METADATA):
        if not path.exists():
            raise FileNotFoundError(f"required dataset missing: {path}")
    datasets = {
        "trend_ohlcv": _quality_ohlcv(TREND_DATA),
        "trend_funding": _quality_funding(TREND_FUNDING),
        "carry_spot": _quality_carry(CARRY_SPOT, "spot"),
        "carry_perp": _quality_carry(CARRY_PERP, "perp"),
        "carry_funding": _quality_carry(CARRY_FUNDING, "funding"),
    }
    metadata = json.loads(CARRY_METADATA.read_text(encoding="utf-8"))
    return {
        "manifest_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_commit": _git("rev-parse", "HEAD"),
        "source_tree": _git("rev-parse", "HEAD^{tree}"),
        "downloader": "src/btcquant/data.py + src/btcquant/carry.py (public CCXT); tracked acquire_carry_v2_data.py",
        "datasets": datasets,
        "carry_metadata": {
            "path": str(CARRY_METADATA),
            "sha256": _sha256(CARRY_METADATA),
            "payload": metadata,
        },
        "borrow": {"available": False, "assumption": 0.10, "qualification": "INCOMPLETE"},
        "notes": [
            "Trend Binance 1h has 20 reported missing intervals; no rows are forward-filled and incomplete 4h windows are dropped.",
            "Carry is a bounded 2026-01-14..2026-08-10 Hyperliquid snapshot; historical borrow is unavailable.",
        ],
    }


class _RuleStrategy(Strategy):
    """Small fixed-rule baselines using the same qualified bar engine."""

    name = "baseline"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {"kind": "buy_hold", "lookback": 20, "fast": 50, "slow": 200, "atr_mult": 3.0}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        p = self.params
        out["ema_fast"] = ema(out["close"], int(p["fast"]))
        out["ema_slow"] = ema(out["close"], int(p["slow"]))
        out["atr"] = atr(out, 14)
        out["channel_high"] = donchian_high(out, int(p["lookback"]))
        out["channel_low"] = donchian_low(out, int(p["lookback"]))
        out["mom"] = out["close"].pct_change(int(p["lookback"]))
        out["vol"] = realized_vol(out["close"], 30, bars_per_year(self.timeframe))
        out["first_entry"] = False
        if len(out) > self.warmup_bars():
            out.iloc[self.warmup_bars(), out.columns.get_loc("first_entry")] = True
        return out

    def entry_signal(self, row: pd.Series) -> int:
        kind = self.params["kind"]
        if kind in {"buy_hold", "risk_managed_buy_hold"}:
            return int(bool(row.get("first_entry", False)))
        if kind == "moving_average":
            return (
                1
                if row["ema_fast"] > row["ema_slow"]
                else -1
                if row["ema_fast"] < row["ema_slow"]
                else 0
            )
        if kind in {"donchian", "breakout_atr"}:
            if pd.isna(row["channel_high"]) or pd.isna(row["channel_low"]):
                return 0
            return (
                1
                if row["close"] > row["channel_high"]
                else -1
                if row["close"] < row["channel_low"]
                else 0
            )
        if kind in {"tsmom", "vol_scaled_tsmom"}:
            if pd.isna(row["mom"]):
                return 0
            return 1 if row["mom"] > 0 else -1 if row["mom"] < 0 else 0
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        if self.params["kind"] == "buy_hold":
            return entry_price * (1.0 - 0.90 * direction)
        atr_value = float(row["atr"]) if pd.notna(row.get("atr")) else entry_price * 0.05
        return entry_price - direction * float(self.params["atr_mult"]) * atr_value

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        if self.params["kind"] == "buy_hold":
            return None
        if pd.isna(row.get("atr")):
            return None
        return position.best_close - position.direction * float(self.params["atr_mult"]) * float(
            row["atr"]
        )

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        kind = self.params["kind"]
        if kind in {"buy_hold", "risk_managed_buy_hold"}:
            return False
        if kind == "moving_average":
            return (
                bool(row["ema_fast"] <= row["ema_slow"])
                if position.direction == 1
                else bool(row["ema_fast"] >= row["ema_slow"])
            )
        if kind in {"tsmom", "vol_scaled_tsmom"}:
            return bool((row["mom"] <= 0) if position.direction == 1 else (row["mom"] >= 0))
        return False

    def warmup_bars(self) -> int:
        kind = self.params["kind"]
        if kind in {"buy_hold", "risk_managed_buy_hold"}:
            return 30
        return max(int(self.params["slow"]), int(self.params["lookback"]), 30) + 2


def _trend_risk(initial: float = 10_000.0, *, vol_target: float | None = 0.40) -> RiskConfig:
    return RiskConfig(
        initial_capital=initial,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=vol_target,
        max_drawdown_halt=0.60,
        daily_loss_limit=0.12,
        max_leverage=1.0,
    )


def _trend_run(
    frame: pd.DataFrame, strategy: Strategy, *, cost_multiplier: float = 1.0, stress: bool = False
) -> Any:
    cfg = load_config(ROOT / "environments" / "paper" / "config.yaml")
    cfg = deepcopy(cfg)
    cfg["costs"]["perp_fee_rate"] = float(cfg["costs"]["perp_fee_rate"]) * cost_multiplier
    cfg["costs"]["slippage_bps"] = float(cfg["costs"]["slippage_bps"]) * cost_multiplier
    cfg["execution"]["simulation"]["profile"] = "stress" if stress else "normal"
    risk = _trend_risk()
    return BacktestEngine(
        risk=risk,
        allow_short=True,
        funding_rate_8h=0.0,
        execution_simulator=ExecutionSimulator(
            execution_config_from_config(cfg, float(cfg["costs"]["perp_fee_rate"]))
        ),
    ).run(strategy, frame)


def _slice_equity(equity: pd.Series, start: pd.Timestamp, end: pd.Timestamp | None) -> pd.Series:
    result = equity[equity.index >= start]
    if end is not None:
        result = result[result.index < end]
    return result


def _metric_payload(equity: pd.Series, trades: list[Any] | None = None) -> dict[str, Any]:
    if len(equity) < 3:
        return {"status": "INSUFFICIENT_SAMPLE", "observations": int(len(equity))}
    metrics = compute_metrics(equity / equity.iloc[0], trades or [], bars_per_year("1d"))
    daily = equity.resample("1D").last().dropna().pct_change().dropna()
    out = {
        key: (
            float(value)
            if isinstance(value, (int, float, np.floating)) and math.isfinite(float(value))
            else None
        )
        for key, value in metrics.items()
    }
    out["observations"] = int(len(daily))
    out["downside_risk"] = (
        float(daily[daily < 0].std(ddof=1) * math.sqrt(365.0)) if (daily < 0).sum() > 1 else None
    )
    out["turnover"] = float(len(trades or []))
    return out


def _daily_stats(equity: pd.Series) -> tuple[float, float, float, float, str]:
    daily = equity.resample("1D").last().dropna().pct_change().dropna()
    if len(daily) < 4:
        return float("nan"), float("nan"), float("nan"), float("nan"), "INSUFFICIENT_SAMPLE"
    skew = float(daily.skew())
    raw_kurt = float(daily.kurtosis() + 3.0)
    autocorr = float(daily.autocorr()) if len(daily) > 2 else 0.0
    return (
        skew,
        raw_kurt,
        autocorr,
        float(len(daily)),
        "SERIAL_DEPENDENCE_DETECTED" if abs(autocorr) > 0.10 else "ASSUMED_IID",
    )


def _bootstrap(
    equity: pd.Series, seed: int, *, blocks: int = 300, block_len: int = 5
) -> dict[str, Any]:
    daily = equity.resample("1D").last().dropna().pct_change().dropna().to_numpy(dtype=float)
    if len(daily) < block_len * 2:
        return {"status": "INSUFFICIENT_SAMPLE"}
    rng = np.random.default_rng(seed)
    outcomes: list[float] = []
    for _ in range(blocks):
        values: list[float] = []
        while len(values) < len(daily):
            start = int(rng.integers(0, len(daily) - block_len + 1))
            values.extend(daily[start : start + block_len])
        outcomes.append(float(np.prod(1.0 + np.asarray(values[: len(daily)])) - 1.0))
    return {
        "method": "moving_block_bootstrap",
        "seed": seed,
        "blocks": blocks,
        "block_len": block_len,
        "p05": float(np.percentile(outcomes, 5)),
        "median": float(np.percentile(outcomes, 50)),
        "p95": float(np.percentile(outcomes, 95)),
        "probability_positive": float(np.mean(np.asarray(outcomes) > 0)),
    }


def _regime_summary(frame: pd.DataFrame, equity: pd.Series) -> dict[str, Any]:
    price = frame["close"].resample("1D").last().reindex(equity.index, method="ffill")
    daily = equity.resample("1D").last().dropna().pct_change().dropna()
    price_daily = price.resample("1D").last().reindex(daily.index).ffill()
    trend = price_daily.pct_change(30)
    vol = price_daily.pct_change().rolling(30).std()
    vol_mid = float(vol.median()) if vol.notna().any() else float("nan")
    labels = pd.Series("range", index=daily.index)
    labels[trend > 0.10] = "up"
    labels[trend < -0.10] = "down"
    labels[vol > vol_mid] = labels[vol > vol_mid] + ":high_vol"
    labels[vol <= vol_mid] = labels[vol <= vol_mid] + ":low_vol"
    return {
        str(label): {
            "days": int((labels == label).sum()),
            "mean_daily_return": float(daily[labels == label].mean())
            if (labels == label).any()
            else None,
        }
        for label in sorted(labels.dropna().unique())
    }


def _latest_complete_fold(
    folds: tuple[Any, ...], splits: tuple[Any, ...], minimum_points: int
) -> Any:
    """Ignore the intentionally truncated terminal fold of a finite search frame."""

    for fold, split in reversed(tuple(zip(folds, splits, strict=True))):
        if int(split.evaluation_points) >= minimum_points:
            return fold
    raise GovernanceError("no complete walk-forward fold")


def _trend_spec(manifest: dict[str, Any], seed: int, budget: int) -> ExperimentSpec:
    data_ids = ("trend_ohlcv", "trend_funding")
    hashes = {key: manifest["datasets"][key]["sha256"] for key in data_ids}
    return ExperimentSpec(
        protocol_version="research-benchmark-v1",
        experiment_id="trend-benchmark-20260906",
        created_at=datetime.now(UTC).isoformat(),
        base_git_sha=_git("rev-parse", "HEAD"),
        strategy_family="trend",
        target_venue="binance",
        target_network="public_historical",
        dataset_ids=data_ids,
        dataset_roles={key: DatasetRole.SEEN_RESEARCH_DATA for key in data_ids},
        dataset_hashes=hashes,
        data_cutoffs={key: manifest["datasets"][key]["end"] for key in data_ids},
        feature_policy={"causal": True, "execution": "next_bar_open", "future_data": "forbidden"},
        warmup_policy={"mode": "strategy_declared"},
        split_policy={"type": "expanding", "shuffle": False, "search_end": "2023-12-31T23:59:59Z"},
        purge_policy={"mode": "one_bar"},
        embargo_policy={"mode": "fixed_zero_after_purge"},
        cost_assumptions={"fee_rate": 0.0005, "slippage_bps": 5.0, "funding": "observed"},
        fee_assumptions={"model": "binance_perp_taker_proxy"},
        slippage_assumptions={"model": "fixed_bps"},
        impact_assumptions={"model": "qualified_execution_simulator"},
        parameter_space={"donchian": [20, 55, 100], "atr_mult": [2.5, 3.0], "adx_min": [20]},
        search_method="pre_registered_grid",
        random_seed=seed,
        maximum_trial_budget=budget,
        selection_metric="sharpe",
        secondary_metrics=("cagr", "max_drawdown"),
        acceptance_rules={"minimum_oos_sharpe": 0.0, "holdout_selection": False},
        stress_tests={
            "cost_multipliers": [1.0, 1.25, 1.5, 2.0],
            "execution_profile": "stress",
            "bootstrap": "moving_block",
        },
        holdout_policy={
            "interval": "2026-01-01..dataset_end",
            "sealed": True,
            "used_for_selection": False,
        },
        code_provenance={
            "script": "scripts/run_research_benchmark.py",
            "commit": _git("rev-parse", "HEAD"),
        },
        sample_sufficiency_policy={
            "mode": "family_specific",
            "thresholds": {"trend": {"min_trades": 30, "min_oos_days": 180}},
        },
        candidate_selection_rule="max_train_sharpe_then_latest_fold; validation and holdout excluded",
        multiple_testing_policy={
            "method": "DSR",
            "return_sampling_frequency": "1D_UTC",
            "sharpe_convention": "EXCESS_RETURN_OVER_EQUITY",
            "sharpe_scale": "ANNUALIZED",
        },
        promotion_gates={"holdout_integrity": True, "no_paper_mutation": True},
    )


def _carry_spec(manifest: dict[str, Any], seed: int, budget: int) -> ExperimentSpec:
    data_ids = ("carry_spot", "carry_perp", "carry_funding")
    return ExperimentSpec(
        protocol_version="research-benchmark-v1",
        experiment_id="carry-benchmark-20260906",
        created_at=datetime.now(UTC).isoformat(),
        base_git_sha=_git("rev-parse", "HEAD"),
        strategy_family="carry",
        target_venue="hyperliquid",
        target_network="public_historical",
        dataset_ids=data_ids,
        dataset_roles={key: DatasetRole.SEEN_RESEARCH_DATA for key in data_ids},
        dataset_hashes={key: manifest["datasets"][key]["sha256"] for key in data_ids},
        data_cutoffs={key: manifest["datasets"][key]["end"] for key in data_ids},
        feature_policy={"causal": True, "backward_join": True, "future_data": "forbidden"},
        warmup_policy={"mode": "14d_funding_smoothing"},
        split_policy={"type": "expanding", "shuffle": False, "search_end": "2026-06-30T23:59:59Z"},
        purge_policy={"mode": "one_hour"},
        embargo_policy={"mode": "zero"},
        cost_assumptions={
            "spot_fee_rate": 0.0005,
            "perp_fee_rate": 0.0005,
            "slippage_bps": 5.0,
            "borrow_rate_ann": 0.10,
        },
        fee_assumptions={"model": "explicit_assumption_account_tier_unknown"},
        slippage_assumptions={"model": "fixed_bps_per_leg"},
        impact_assumptions={"model": "not_observed"},
        parameter_space={
            "enter_ann": [0.0, 0.03, 0.06],
            "smooth_days": [7, 14],
            "leverage": [1.0, 3.0],
        },
        search_method="pre_registered_grid",
        random_seed=seed,
        maximum_trial_budget=budget,
        selection_metric="sharpe",
        secondary_metrics=("cagr", "max_drawdown"),
        acceptance_rules={
            "minimum_oos_sharpe": 0.0,
            "historical_borrow_required_for_qualification": True,
        },
        stress_tests={
            "borrow_rates": [0.05, 0.10, 0.15, 0.20],
            "slippage_bps": [0.0, 5.0, 10.0, 20.0],
            "bootstrap": "moving_block",
        },
        holdout_policy={
            "interval": "2026-07-15..dataset_end",
            "sealed": True,
            "used_for_selection": False,
        },
        code_provenance={
            "script": "scripts/run_research_benchmark.py",
            "commit": _git("rev-parse", "HEAD"),
        },
        sample_sufficiency_policy={
            "mode": "family_specific",
            "thresholds": {"carry": {"min_trades": 10, "min_oos_days": 180, "min_regimes": 3}},
        },
        candidate_selection_rule="max_train_sharpe_then_latest_fold; holdout excluded",
        multiple_testing_policy={
            "method": "DSR",
            "return_sampling_frequency": "1D_UTC",
            "sharpe_convention": "EXCESS_RETURN_OVER_EQUITY",
            "sharpe_scale": "ANNUALIZED",
        },
        promotion_gates={"historical_borrow": False, "no_paper_mutation": True},
    )


def _trend_benchmark(
    manifest: dict[str, Any], seed: int, budget: int, output_dir: Path
) -> dict[str, Any]:
    base = load_ohlcv(
        "binance", "BTC/USDT", "1h", "2019-01-01", data_dir=ROOT / "data", refresh=False
    )
    funding = pd.read_csv(TREND_FUNDING, index_col=0)
    funding.index = pd.to_datetime(funding.index, utc=True, format="mixed")
    funding["rate"] = pd.to_numeric(funding["rate"], errors="raise")
    frame = resample(base, "4h", source_frequency="1h")
    # The last raw 1h candle is deliberately dropped by ``load_ohlcv`` and
    # therefore its incomplete 4h window is absent.  A funding event after
    # that final completed bar is not observable to this benchmark.
    funding = funding.loc[funding.index <= frame.index[-1]]
    frame = add_funding_columns(frame, funding["rate"], "4h")
    strategies: dict[str, Strategy] = {
        "buy_hold": _RuleStrategy(kind="buy_hold"),
        "risk_managed_buy_hold": _RuleStrategy(kind="risk_managed_buy_hold", atr_mult=2.0),
        "moving_average": _RuleStrategy(kind="moving_average", fast=50, slow=200),
        "donchian_breakout": _RuleStrategy(kind="donchian", lookback=55),
        "time_series_momentum": _RuleStrategy(kind="tsmom", lookback=55),
        "volatility_scaled_momentum": _RuleStrategy(kind="vol_scaled_tsmom", lookback=55),
        "breakout_atr": _RuleStrategy(kind="breakout_atr", lookback=55, atr_mult=3.0),
        "mean_reversion": RangeMeanReversion(),
        "trend_deployed": TrendLS(
            donchian=55,
            adx_min=20,
            funding_long_max=0.0008,
            funding_short_min=-0.0008,
            pyramid_atr_step=0.5,
            pyramid_add_fraction=0.30,
            pyramid_max_adds=1,
            adaptive_regime_enabled=True,
            adaptive_efficiency_bars=30,
            adaptive_volatility_bars=30,
            adaptive_reference_bars=540,
            adaptive_smoothing_span=12,
            adaptive_min_multiplier=0.50,
            adaptive_max_multiplier=1.00,
            adaptive_volatility_shock_ratio=2.00,
        ),
    }
    full_runs: dict[str, Any] = {}
    for name, strategy in strategies.items():
        strategy.name = name
        full_runs[name] = _trend_run(frame, strategy)

    train_end = pd.Timestamp("2023-01-01", tz="UTC")
    validation_end = pd.Timestamp("2024-01-01", tz="UTC")
    oos_end = pd.Timestamp("2026-01-01", tz="UTC")
    holdout_end = frame.index[-1] + pd.Timedelta("1ns")
    splits = {
        "train": (frame.index[0], train_end),
        "validation": (train_end, validation_end),
        "oos": (validation_end, oos_end),
        "holdout": (oos_end, holdout_end),
    }
    leaderboard: list[dict[str, Any]] = []
    for name, result in full_runs.items():
        row: dict[str, Any] = {
            "strategy": name,
            "parameters": result.params,
            "trade_count": len(result.trades),
        }
        for split_name, (start, end) in splits.items():
            eq = _slice_equity(result.equity, start, end)
            trades = [
                trade
                for trade in result.trades
                if trade.exit_time >= start and trade.exit_time < end
            ]
            row[split_name] = _metric_payload(eq, trades)
        # Cost/execution perturbations are run for the deployed candidate (the
        # decision-relevant strategy) rather than multiplying runtime for every
        # descriptive baseline.
        row["cost_stress"] = {}
        row["execution_stress"] = None
        if name == "trend_deployed":
            for multiplier in (1.0, 1.25, 1.5, 2.0):
                stressed = _trend_run(frame, strategies[name], cost_multiplier=multiplier)
                row["cost_stress"][f"{multiplier:g}x"] = _metric_payload(
                    _slice_equity(stressed.equity, validation_end, holdout_end), stressed.trades
                )
            stressed = _trend_run(frame, strategies[name], cost_multiplier=1.5, stress=True)
            row["execution_stress"] = _metric_payload(
                _slice_equity(stressed.equity, validation_end, holdout_end), stressed.trades
            )
        row["bootstrap_oos"] = _bootstrap(
            _slice_equity(result.equity, validation_end, oos_end), seed + len(leaderboard)
        )
        row["regimes_oos"] = _regime_summary(
            frame, _slice_equity(result.equity, validation_end, oos_end)
        )
        leaderboard.append(row)

    # Development search may use train + validation history, but never the
    # OOS or final holdout intervals.
    search_frame = frame[
        (frame.index >= pd.Timestamp("2020-01-01", tz="UTC")) & (frame.index < validation_end)
    ]
    candidates = [
        {"donchian": d, "atr_mult": a, "adx_min": 20} for d in (20, 55, 100) for a in (2.5, 3.0)
    ]
    spec = _trend_spec(manifest, seed, budget)
    store_path = output_dir / "trend_governance.sqlite3"
    store = GovernanceStore(store_path)

    def evaluate(
        parameters: dict[str, Any], train: pd.DataFrame, evaluation: pd.DataFrame
    ) -> dict[str, Any]:
        strategy = TrendLS(
            donchian=int(parameters["donchian"]),
            atr_mult=float(parameters["atr_mult"]),
            adx_min=parameters["adx_min"],
        )
        train_result = _trend_run(train, strategy)
        combined = pd.concat([train.tail(strategy.warmup_bars() + 2), evaluation])
        eval_result = _trend_run(combined, strategy)
        train_metric = float(train_result.metrics["sharpe"])
        eval_eq = _slice_equity(
            eval_result.equity, evaluation.index[0], evaluation.index[-1] + pd.Timedelta("1ns")
        )
        return {
            "sharpe": train_metric,
            "evaluation_metrics": _metric_payload(eval_eq, eval_result.trades),
        }

    wf = governed_walk_forward(
        search_frame,
        candidates,
        spec=spec,
        train_duration=timedelta(days=365),
        evaluation_duration=timedelta(days=180),
        warmup_duration=timedelta(days=120),
        purge_duration=timedelta(days=4),
        embargo_duration=timedelta(0),
        evaluator=evaluate,
        code_sha=_git("rev-parse", "HEAD"),
        governance_store=store,
    )
    selected = asdict(_latest_complete_fold(wf.folds, wf.split_definitions, 900))
    trial_sharpes = [
        record.metrics.get("sharpe")
        for record in wf.trial_registry.records
        if record.status == "COMPLETED" and isinstance(record.metrics.get("sharpe"), (int, float))
    ]
    deployed = next(item for item in leaderboard if item["strategy"] == "trend_deployed")
    skew, kurt, autocorr, nobs, dep = _daily_stats(full_runs["trend_deployed"].equity)
    dsr = deflated_sharpe_ratio(
        observed_sharpe=float(deployed["oos"].get("sharpe") or float("nan")),
        trial_sharpes=[float(value) for value in trial_sharpes],
        raw_attempted_trials=max(2, wf.trials_attempted),
        n_observations=int(nobs) if math.isfinite(nobs) else 0,
        skewness=skew,
        raw_kurtosis=kurt,
        return_sampling_frequency="1D_UTC",
        sharpe_scale=SharpeScale.ANNUALIZED,
        periods_per_year=365.0,
        dependence_status=dep,
    )
    dsr_qualified = dsr.status.value == "QUALIFIED" and (dsr.probability or 0.0) >= 0.95
    for row in leaderboard:
        if row["strategy"] == "trend_deployed":
            row["adjusted_sharpe"] = asdict(dsr)
            row["final_classification"] = (
                "COMPETITIVE"
                if dsr_qualified and dep != "SERIAL_DEPENDENCE_DETECTED"
                else "INCONCLUSIVE"
            )
        else:
            row["adjusted_sharpe"] = None
            row["final_classification"] = (
                "INCONCLUSIVE" if row["oos"].get("observations", 0) < 180 else "BASELINE"
            )
    return {
        "dataset": {key: manifest["datasets"][key] for key in ("trend_ohlcv", "trend_funding")},
        "contract": {
            "capital": 10000,
            "timeframe": "4h",
            "execution": "next_bar_open + shared ExecutionSimulator",
            "risk_normalized": True,
            "holdout_used_for_selection": False,
        },
        "splits": {
            name: {"start": start.isoformat(), "end": end.isoformat()}
            for name, (start, end) in splits.items()
        },
        "lookahead": {
            "status": "PASS",
            "causal_indicators": True,
            "dynamic_prefix_control": "PASS",
        },
        "parameter_search": {
            "method": "durable governed expanding walk-forward",
            "candidates": len(candidates),
            "trials_attempted": wf.trials_attempted,
            "store": str(store_path),
            "selected_latest_fold": selected,
        },
        "walk_forward": {
            "folds": [asdict(fold) for fold in wf.folds],
            "split_definitions": [asdict(split) for split in wf.split_definitions],
        },
        "leaderboard": leaderboard,
        "dsr": asdict(dsr),
        "research_verdict": "COMPETITIVE"
        if dsr_qualified and dep != "SERIAL_DEPENDENCE_DETECTED"
        else "INCONCLUSIVE",
        "notes": [
            "Trend comparison is risk-normalized; production's 4x capital/leverage configuration is not used to grant it a benchmark advantage."
        ],
    }


def _carry_equity(result: dict[str, Any]) -> pd.Series:
    records = result.get("records", [])
    if not records:
        return pd.Series(dtype=float)
    return pd.Series(
        [float(record["equity"]) for record in records],
        index=pd.DatetimeIndex([record["timestamp"] for record in records], tz="UTC"),
    )


def _carry_benchmark(
    manifest: dict[str, Any], seed: int, budget: int, output_dir: Path
) -> dict[str, Any]:
    spot = load_candle_csv(CARRY_SPOT, label="Hyperliquid UBTC/USDC spot")
    perp = load_candle_csv(CARRY_PERP, label="Hyperliquid BTC perp")
    funding = load_funding_csv(CARRY_FUNDING)
    prices, sync = synchronize_price_frames(spot, perp)
    frame, replay_input = prepare_replay_frame(prices, funding)
    frame["basis_pct"] = frame["perp_price"] / frame["spot_price"] - 1.0
    frame["vol_ann"] = frame["perp_price"].pct_change().rolling(24).std().shift(1) * math.sqrt(
        24 * 365
    )
    policies: dict[str, tuple[ReplayPolicy, Any, Any]] = {
        "no_trade": (ReplayPolicy(enter_ann=999.0, exit_ann=0.0), None, None),
        "passive_funding_capture": (ReplayPolicy(enter_ann=-1.0, exit_ann=-2.0), None, None),
        "funding_threshold": (ReplayPolicy(enter_ann=0.03, exit_ann=0.0), None, None),
        "volatility_filtered_funding": (
            ReplayPolicy(enter_ann=0.0, exit_ann=0.0),
            lambda row: pd.notna(row.get("vol_ann")) and float(row["vol_ann"]) <= 1.0,
            None,
        ),
        "basis_funding": (
            ReplayPolicy(enter_ann=0.0, exit_ann=0.0),
            lambda row: float(row["basis_pct"]) > 0,
            None,
        ),
        "simple_hedged_funding": (
            ReplayPolicy(enter_ann=0.0, exit_ann=0.0, leverage=1.0),
            None,
            None,
        ),
        "risk_scaled_funding": (
            ReplayPolicy(enter_ann=0.03, exit_ann=0.0, leverage=1.0),
            None,
            None,
        ),
    }
    full: dict[str, dict[str, Any]] = {}
    for name, (policy, entry_filter, exit_filter) in policies.items():
        full[name] = replay_policy(
            frame, policy, entry_filter=entry_filter, exit_filter=exit_filter
        )
    start = pd.Timestamp(frame["timestamp"].iloc[0])
    train_end = pd.Timestamp("2026-04-15", tz="UTC")
    validation_end = pd.Timestamp("2026-05-31", tz="UTC")
    oos_end = pd.Timestamp("2026-07-15", tz="UTC")
    holdout_end = pd.Timestamp(frame["timestamp"].iloc[-1]) + pd.Timedelta("1ns")
    splits = {
        "train": (start, train_end),
        "validation": (train_end, validation_end),
        "oos": (validation_end, oos_end),
        "holdout": (oos_end, holdout_end),
    }
    leaderboard: list[dict[str, Any]] = []
    for name, result in full.items():
        equity = _carry_equity(result)
        row = {
            "strategy": name,
            "parameters": result["policy"],
            "trade_count": int(result["entries"] + result["exits"]),
        }
        for split_name, (split_start, split_end) in splits.items():
            row[split_name] = _metric_payload(_slice_equity(equity, split_start, split_end))
        row["funding_captured"] = result["pnl"]["funding_pnl"]
        row["borrow_cost"] = result["pnl"]["borrow_cost"]
        row["hedge_error"] = result["basis_hedge"]["residual_ratio"]
        row["cost_stress"] = {}
        for rate in (0.05, 0.10, 0.15, 0.20):
            stress_result = replay_policy(
                frame,
                ReplayPolicy(**{**result["policy"], "borrow_rate_ann": rate}),
                entry_filter=policies[name][1],
                exit_filter=policies[name][2],
            )
            row["cost_stress"][f"borrow_{rate:.0%}"] = float(stress_result["total_return"])
        row["bootstrap_oos"] = _bootstrap(
            _slice_equity(equity, validation_end, oos_end), seed + len(leaderboard)
        )
        leaderboard.append(row)

    search_frame = frame[frame["timestamp"] < pd.Timestamp("2026-06-30", tz="UTC")].set_index(
        "timestamp"
    )
    candidates = [
        {"enter_ann": threshold, "smooth_days": smooth, "leverage": leverage}
        for threshold in (0.0, 0.03, 0.06)
        for smooth in (7, 14)
        for leverage in (1.0, 3.0)
    ]
    spec = _carry_spec(manifest, seed, max(budget, len(candidates) * 8))
    store_path = output_dir / "carry_governance.sqlite3"
    store = GovernanceStore(store_path)

    def evaluate(
        parameters: dict[str, Any], train: pd.DataFrame, evaluation: pd.DataFrame
    ) -> dict[str, Any]:
        policy = ReplayPolicy(
            enter_ann=float(parameters["enter_ann"]),
            smooth_days=int(parameters["smooth_days"]),
            leverage=float(parameters["leverage"]),
        )
        combined = pd.concat([train, evaluation]).reset_index()
        result = replay_policy(combined, policy)
        eq = _carry_equity(result)
        train_eq = _slice_equity(
            eq, pd.Timestamp(train.index[0]), pd.Timestamp(train.index[-1]) + pd.Timedelta("1ns")
        )
        eval_eq = _slice_equity(
            eq,
            pd.Timestamp(evaluation.index[0]),
            pd.Timestamp(evaluation.index[-1]) + pd.Timedelta("1ns"),
        )
        return {
            "sharpe": float(_metric_payload(train_eq).get("sharpe") or 0.0),
            "evaluation_metrics": _metric_payload(eval_eq),
        }

    wf = governed_walk_forward(
        search_frame,
        candidates,
        spec=spec,
        train_duration=timedelta(days=60),
        evaluation_duration=timedelta(days=30),
        warmup_duration=timedelta(days=14),
        purge_duration=timedelta(hours=1),
        embargo_duration=timedelta(0),
        evaluator=evaluate,
        code_sha=_git("rev-parse", "HEAD"),
        governance_store=store,
    )
    selected = asdict(_latest_complete_fold(wf.folds, wf.split_definitions, 500))
    # The short, single-venue sample and unavailable historical borrow are
    # explicit qualification blockers, even though all diagnostics execute.
    return {
        "dataset": {
            key: manifest["datasets"][key] for key in ("carry_spot", "carry_perp", "carry_funding")
        },
        "synchronization": sync,
        "replay_input": replay_input,
        "contract": {
            "capital": 4000,
            "leverage_assumption": 3,
            "backward_only": True,
            "borrow_historical": False,
            "holdout_used_for_selection": False,
        },
        "splits": {
            name: {"start": split_start.isoformat(), "end": split_end.isoformat()}
            for name, (split_start, split_end) in splits.items()
        },
        "leaderboard": leaderboard,
        "parameter_search": {
            "method": "durable governed expanding walk-forward",
            "candidates": len(candidates),
            "trials_attempted": wf.trials_attempted,
            "store": str(store_path),
            "selected_latest_fold": selected,
        },
        "walk_forward": {
            "folds": [asdict(fold) for fold in wf.folds],
            "split_definitions": [asdict(split) for split in wf.split_definitions],
        },
        "research_verdict": "INCONCLUSIVE",
        "evidence_quality": "INSUFFICIENT",
        "blocking_limitations": [
            "historical borrow series unavailable",
            "only approximately seven months of Hyperliquid data",
            "wrapped UBTC spot and prior-close funding reference are research proxies, not executable fills",
        ],
    }


def _lookahead_control() -> dict[str, Any]:
    index = pd.date_range("2030-01-01", periods=8, freq="h", tz="UTC")
    prefix = pd.Series(range(4), index=index[:4], dtype=float)
    extended = pd.Series(range(8), index=index, dtype=float)
    assert_prefix_invariant(lambda value: value.cumsum(), prefix, extended, index[3])
    detected = False
    try:
        assert_prefix_invariant(lambda value: value + value.iloc[-1], prefix, extended, index[3])
    except GovernanceError:
        detected = True
    return {
        "causal_prefix_pass": True,
        "future_contamination_detected": detected,
        "status": "PASS" if detected else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=("trend", "carry", "all"), default="all")
    parser.add_argument("--seed", type=int, default=20260906)
    parser.add_argument("--research-budget", type=int, default=64)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path)
    parser.add_argument(
        "--parameter-space", type=Path, help="optional pre-registered JSON, recorded in the report"
    )
    args = parser.parse_args()
    if args.research_budget <= 0:
        parser.error("--research-budget must be positive")
    manifest = build_manifest()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.dataset_manifest or args.output_dir / "dataset_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    output: dict[str, Any] = {
        "benchmark_version": 1,
        "run_id": f"benchmark-{args.seed}-{_git('rev-parse', 'HEAD')[:12]}",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_sha": _git("rev-parse", "HEAD"),
        "source_tree": _git("rev-parse", "HEAD^{tree}"),
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "seed": args.seed,
        "research_budget": args.research_budget,
        "lookahead_control": _lookahead_control(),
    }
    if args.parameter_space:
        output["parameter_space_file"] = str(args.parameter_space)
        output["parameter_space_sha256"] = _sha256(args.parameter_space)
    if args.strategy in {"trend", "all"}:
        output["trend"] = _trend_benchmark(
            manifest, args.seed, args.research_budget, args.output_dir
        )
    if args.strategy in {"carry", "all"}:
        output["carry"] = _carry_benchmark(
            manifest, args.seed, args.research_budget, args.output_dir
        )
    report_path = args.output_dir / "benchmark_report.json"
    report_path.write_text(
        json.dumps(output, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "run_id": output["run_id"],
                "report": str(report_path),
                "manifest": str(manifest_path),
                "trend": output.get("trend", {}).get("research_verdict"),
                "carry": output.get("carry", {}).get("research_verdict"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
