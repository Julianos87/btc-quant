"""Frozen-parameter BTC cross-venue validation.

This command is research-only.  It downloads public OHLCV through CCXT for
the venues frozen in ``cross_venue_preregistration.json``, validates and hashes
the raw/transformed data, then runs the existing BTCQuant causal 4h engine
without venue-specific tuning.  Observations at or after the future-holdout
cutoff are discarded before persistence and are never used in diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ccxt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_research_benchmark as reference  # noqa: E402
import run_strategy_discovery as discovery  # noqa: E402
from run_strategy_discovery import DEPLOYED, DiscoveryTrend  # noqa: E402
from btcquant.strategies.base import Strategy  # noqa: E402


PREREGISTRATION_DEFAULT = Path(
    "/home/ubuntu/btcquant-cross-venue-20260907/cross_venue_preregistration.json"
)
DEFAULT_OUTPUT = Path("/home/ubuntu/btcquant-cross-venue-20260907")
FUTURE_CUTOFF = pd.Timestamp("2026-09-07T00:00:00Z")
RAW_TIMEFRAME = "1h"
FINAL_TIMEFRAME = "4h"
OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class VenueExcluded(RuntimeError):
    """A pre-registered venue cannot produce a valid historical dataset."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_value(*args: str) -> str:
    import subprocess

    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def _exchange(exchange_id: str) -> ccxt.Exchange:
    klass = getattr(ccxt, exchange_id)
    return klass({"enableRateLimit": True, "timeout": 30_000})


def _select_symbol(ex: ccxt.Exchange, candidates: list[str]) -> str:
    ex.load_markets()
    for candidate in candidates:
        if candidate in ex.symbols:
            return candidate
    raise VenueExcluded(f"no pre-registered symbol available: {candidates}")


def _validate_ohlcv(frame: pd.DataFrame, *, venue: str, cutoff: pd.Timestamp) -> dict[str, Any]:
    missing = sorted(set(OHLCV_COLUMNS) - set(frame.columns))
    if missing:
        raise VenueExcluded(f"{venue}: missing columns {missing}")
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if timestamps.isna().any():
        raise VenueExcluded(f"{venue}: invalid timestamps")
    if ((timestamps.dt.minute != 0) | (timestamps.dt.second != 0)).any():
        raise VenueExcluded(f"{venue}: timestamps are not aligned to one-hour boundaries")
    if (timestamps >= cutoff).any():
        raise VenueExcluded(f"{venue}: future rows were not clipped before validation")
    if timestamps.duplicated().any():
        raise VenueExcluded(f"{venue}: duplicate timestamps")
    if not timestamps.is_monotonic_increasing:
        raise VenueExcluded(f"{venue}: out-of-order timestamps")
    numeric = frame[["open", "high", "low", "close", "volume"]].apply(
        pd.to_numeric, errors="coerce"
    )
    values = numeric.to_numpy(dtype=float)
    if numeric.isna().any().any() or not np.isfinite(values).all():
        raise VenueExcluded(f"{venue}: non-finite OHLCV")
    if (numeric[["open", "high", "low", "close"]] <= 0).any().any() or (
        numeric["volume"] < 0
    ).any():
        raise VenueExcluded(f"{venue}: impossible OHLCV")
    if (numeric["high"] < numeric[["open", "close"]].max(axis=1)).any():
        raise VenueExcluded(f"{venue}: high below open/close")
    if (numeric["low"] > numeric[["open", "close"]].min(axis=1)).any():
        raise VenueExcluded(f"{venue}: low above open/close")
    deltas = timestamps.diff().dropna()
    gaps = deltas[deltas != pd.Timedelta("1h")]
    return {
        "rows": int(len(frame)),
        "start": timestamps.iloc[0].isoformat(),
        "end": timestamps.iloc[-1].isoformat(),
        "timeframe": RAW_TIMEFRAME,
        "timezone": "UTC",
        "duplicates": 0,
        "out_of_order": 0,
        "gaps": int(len(gaps)),
        "gap_examples": [
            {
                "after": timestamps.iloc[int(index)].isoformat(),
                "delta_seconds": float(delta.total_seconds()),
            }
            for index, delta in gaps.head(10).items()
        ],
        "incomplete_last_candle": False,
        "future_rows_persisted": 0,
    }


def acquire_venue(
    venue_spec: dict[str, Any],
    *,
    output_dir: Path,
    start: pd.Timestamp,
    cutoff: pd.Timestamp,
    refresh: bool,
) -> dict[str, Any]:
    venue = str(venue_spec["venue"])
    exchange_id = venue.lower()
    data_dir = output_dir / "data"
    raw_dir = output_dir / "raw"
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe = venue.lower().replace(" ", "_")
    transformed_path = data_dir / f"{safe}_btc_1h.csv"
    raw_path = raw_dir / f"{safe}_btc_1h.json"
    metadata_path = output_dir / f"{safe}_metadata.json"

    if transformed_path.exists() and raw_path.exists() and metadata_path.exists() and not refresh:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        cached_frame = pd.read_csv(transformed_path)
        cached_quality = _validate_ohlcv(cached_frame, venue=venue, cutoff=cutoff)
        if metadata.get("transformed_sha256") != sha256_file(transformed_path):
            raise VenueExcluded(f"{venue}: cached transformed SHA-256 mismatch")
        if metadata.get("raw_sha256") != sha256_file(raw_path):
            raise VenueExcluded(f"{venue}: cached raw SHA-256 mismatch")
        if metadata.get("quality", {}).get("rows") != cached_quality["rows"]:
            raise VenueExcluded(f"{venue}: cached quality row count mismatch")
        metadata["cached"] = True
        return metadata

    ex = _exchange(exchange_id)
    symbol = _select_symbol(ex, list(venue_spec["symbol_candidates"]))
    since_ms = int(start.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cursor = since_ms
    tf_ms = 3_600_000
    limit = 1_000
    all_rows: list[list[Any]] = []
    request_count = 0
    max_requests = 1_000
    while cursor < cutoff_ms:
        if request_count >= max_requests:
            raise VenueExcluded(f"{venue}: pagination exceeded {max_requests} requests")
        request_count += 1
        batch: list[list[Any]] | None = None
        error: Exception | None = None
        for attempt in range(5):
            try:
                batch = ex.fetch_ohlcv(symbol, RAW_TIMEFRAME, since=cursor, limit=limit)
                break
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as exc:
                error = exc
                time.sleep(min(2**attempt, 16))
        if batch is None:
            raise VenueExcluded(f"{venue}: public OHLCV failed after retries: {error}")
        if not batch:
            break
        # Kraken's public OHLC endpoint intentionally returns only its recent
        # bounded window and ignores old ``since`` values.  Detect that
        # contract before considering the venue history valid.
        first_ts = int(batch[0][0])
        if first_ts > cursor + tf_ms:
            raise VenueExcluded(
                f"{venue}: API returned {pd.Timestamp(first_ts, unit='ms', tz='UTC')} "
                f"for requested {pd.Timestamp(cursor, unit='ms', tz='UTC')}; no bounded history"
            )
        usable = [row for row in batch if int(row[0]) < cutoff_ms]
        all_rows.extend(usable)
        last_ts = max(int(row[0]) for row in batch)
        if last_ts < cursor:
            raise VenueExcluded(f"{venue}: cursor moved backwards")
        if last_ts == cursor:
            if len(batch) == 1:
                break
            last_ts = int(batch[-1][0])
        cursor = last_ts + tf_ms
        if last_ts >= cutoff_ms - tf_ms:
            break

    if not all_rows:
        raise VenueExcluded(f"{venue}: no rows before future holdout cutoff")
    raw_payload = {
        "venue": venue,
        "exchange_id": exchange_id,
        "symbol": symbol,
        "timeframe": RAW_TIMEFRAME,
        "start_requested": start.isoformat(),
        "end_exclusive": cutoff.isoformat(),
        "rows": all_rows,
    }
    raw_path.write_text(json.dumps(raw_payload, separators=(",", ":")), encoding="utf-8")
    frame = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    # Keep only completed candles strictly before the frozen holdout cutoff.
    frame = frame[frame["timestamp"] + pd.Timedelta("1h") <= cutoff].copy()
    frame["timestamp"] = frame["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Do not repair duplicates silently: validation must fail closed.
    frame = frame.sort_values("timestamp")
    quality = _validate_ohlcv(frame, venue=venue, cutoff=cutoff)
    frame.to_csv(transformed_path, index=False, columns=OHLCV_COLUMNS)
    metadata = {
        "venue": venue,
        "exchange_id": exchange_id,
        "symbol": symbol,
        "instrument": "spot",
        "quote_currency": venue_spec["quote_currency"],
        "source": "official public OHLCV endpoint through CCXT",
        "endpoint": str(getattr(ex, "urls", {}).get("api", "public CCXT API")),
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "start_requested": start.isoformat(),
        "end_exclusive": cutoff.isoformat(),
        "raw_path": str(raw_path),
        "transformed_path": str(transformed_path),
        "raw_sha256": sha256_file(raw_path),
        "transformed_sha256": sha256_file(transformed_path),
        "quality": quality,
        "transformations": [
            "CCXT public fetch_ohlcv pagination",
            "UTC conversion",
            "strict OHLCV validation",
            "drop candles ending at/after the future-holdout cutoff",
            "causal complete 4h resampling performed at evaluation time",
        ],
        "cached": False,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def _load_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("timestamp")
    return frame[["open", "high", "low", "close", "volume"]].astype(float)


def _make_strategy(name: str) -> Strategy:
    if name == "CURRENT_TREND":
        strategy: Strategy = DiscoveryTrend(**dict(DEPLOYED))
    elif name == "EMA_50_200_ATR_3":
        params = DiscoveryTrend.default_params()
        params.update(
            {
                "ema_fast": 50,
                "ema_slow": 200,
                "atr_mult": 3.0,
                "signal_mode": "ema_cross",
                "regime_mode": "none",
                "adx_min": None,
                "funding_long_max": None,
                "funding_short_min": None,
                "exit_mode": "ema",
                "stop_mode": "atr",
                "sizing_mode": "adaptive",
                "adaptive_regime_enabled": True,
            }
        )
        strategy = DiscoveryTrend(**params)
    elif name == "EMA_30_150_ATR_3":
        params = DiscoveryTrend.default_params()
        params.update(
            {
                "ema_fast": 30,
                "ema_slow": 150,
                "atr_mult": 3.0,
                "signal_mode": "ema_cross",
                "regime_mode": "none",
                "adx_min": None,
                "funding_long_max": None,
                "funding_short_min": None,
                "exit_mode": "ema",
                "stop_mode": "atr",
                "sizing_mode": "adaptive",
                "adaptive_regime_enabled": True,
            }
        )
        strategy = DiscoveryTrend(**params)
    elif name == "DONCHIAN_100_ATR_3":
        params = DiscoveryTrend.default_params()
        params.update(
            {
                "donchian": 100,
                "atr_mult": 3.0,
                "signal_mode": "channel",
                "regime_mode": "none",
                "adx_min": None,
                "funding_long_max": None,
                "funding_short_min": None,
                "exit_mode": "ema",
                "stop_mode": "atr",
                "sizing_mode": "adaptive",
                "adaptive_regime_enabled": True,
            }
        )
        strategy = DiscoveryTrend(**params)
    elif name == "BUY_AND_HOLD":
        strategy = reference._RuleStrategy(kind="buy_hold")
    elif name == "RISK_MANAGED_BUY_AND_HOLD":
        strategy = reference._RuleStrategy(kind="risk_managed_buy_hold", atr_mult=2.0)
    else:
        raise ValueError(f"unknown frozen strategy {name}")
    strategy.name = name
    return strategy


STRATEGIES = (
    "CURRENT_TREND",
    "EMA_50_200_ATR_3",
    "EMA_30_150_ATR_3",
    "DONCHIAN_100_ATR_3",
    "BUY_AND_HOLD",
    "RISK_MANAGED_BUY_AND_HOLD",
)


def _frame_4h(frame: pd.DataFrame) -> pd.DataFrame:
    return reference.resample(frame, FINAL_TIMEFRAME, source_frequency=RAW_TIMEFRAME)


def _run(
    frame: pd.DataFrame, strategy_name: str, *, cost: float = 1.0, stress: bool = False
) -> Any:
    strategy = _make_strategy(strategy_name)
    # New spot venues have no venue-native funding series in this campaign.
    # An all-NaN column makes the frozen funding filters explicit and prevents
    # synthetic funding charges or venue-specific assumptions.
    data = frame.copy()
    data["funding"] = np.nan
    return discovery._run(data, strategy, cost_multiplier=cost, stress=stress)


def _slice_metrics(result: Any, start: pd.Timestamp, end: pd.Timestamp | None) -> dict[str, Any]:
    return reference._metric_payload(
        reference._slice_equity(result.equity, start, end),
        [t for t in result.trades if t.exit_time >= start and (end is None or t.exit_time < end)],
    )


def _signal_trace(frame: pd.DataFrame, strategy_name: str) -> pd.Series:
    strategy = _make_strategy(strategy_name)
    data = frame.copy()
    data["funding"] = np.nan
    prepared = strategy.prepare(data)
    values = pd.Series(0, index=prepared.index, dtype=int)
    for timestamp, row in prepared.iloc[strategy.warmup_bars() :].iterrows():
        try:
            values.loc[timestamp] = int(strategy.entry_signal(row))
        except (KeyError, TypeError, ValueError):
            values.loc[timestamp] = 0
    return values


def _signal_agreement(frames: dict[str, pd.DataFrame], valid_venues: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for strategy_name in STRATEGIES:
        pair_rows: list[dict[str, Any]] = []
        traces = {venue: _signal_trace(frames[venue], strategy_name) for venue in valid_venues}
        for left_index, left in enumerate(valid_venues):
            for right in valid_venues[left_index + 1 :]:
                joined = pd.concat([traces[left], traces[right]], axis=1, join="inner").dropna()
                joined.columns = ["left", "right"]
                active = (joined["left"] != 0) | (joined["right"] != 0)
                active_joined = joined[active]
                pair_rows.append(
                    {
                        "venues": [left, right],
                        "bars": int(len(joined)),
                        "direction_agreement_all_bars": float(
                            (joined["left"] == joined["right"]).mean()
                        )
                        if len(joined)
                        else None,
                        "direction_agreement_when_active": float(
                            (active_joined["left"] == active_joined["right"]).mean()
                        )
                        if len(active_joined)
                        else None,
                        "active_bars": int(len(active_joined)),
                    }
                )
        result[strategy_name] = pair_rows
    return result


def _classify(strategy_name: str, summary: dict[str, Any], current: dict[str, Any]) -> str:
    if strategy_name == "BUY_AND_HOLD":
        return "BASELINE"
    if summary["venues_tested"] < 2:
        return "INCONCLUSIVE"
    if (
        summary["positive_venues"] == summary["venues_tested"]
        and summary["cost_1_5x_positive_venues"] >= 2
    ):
        if strategy_name == "CURRENT_TREND":
            return "ROBUST_CROSS_VENUE"
        if summary["median_sharpe"] >= float(current.get("median_sharpe") or -999):
            return "RESEARCH_CANDIDATE"
        return "PROMISING_HYPOTHESIS"
    if summary["positive_venues"] == 0:
        return "REJECTED"
    return "BASELINE" if strategy_name == "DONCHIAN_100_ATR_3" else "INCONCLUSIVE"


def _summarize(
    strategy_name: str, rows: list[dict[str, Any]], current: dict[str, Any]
) -> dict[str, Any]:
    sharpes = [float(row["common_metrics"].get("sharpe") or 0.0) for row in rows]
    returns = [float(row["common_metrics"].get("total_return") or 0.0) for row in rows]
    drawdowns = [float(row["common_metrics"].get("max_drawdown") or 0.0) for row in rows]
    cost_rows = [row["cost_stress"] for row in rows]
    summary = {
        "strategy": strategy_name,
        "venues_tested": len(rows),
        "positive_venues": int(sum(value > 0 for value in returns)),
        "median_return": float(np.median(returns)) if returns else None,
        "worst_return": float(min(returns)) if returns else None,
        "median_sharpe": float(np.median(sharpes)) if sharpes else None,
        "worst_sharpe": float(min(sharpes)) if sharpes else None,
        "median_max_drawdown": float(np.median(drawdowns)) if drawdowns else None,
        "worst_max_drawdown": float(min(drawdowns)) if drawdowns else None,
        "cost_1_5x_positive_venues": int(
            sum(float(item.get("1.5", {}).get("total_return") or 0.0) > 0 for item in cost_rows)
        ),
        "cost_2x_positive_venues": int(
            sum(float(item.get("2.0", {}).get("total_return") or 0.0) > 0 for item in cost_rows)
        ),
    }
    summary["classification"] = _classify(strategy_name, summary, current)
    return summary


def evaluate(
    datasets: dict[str, dict[str, Any]],
    *,
    output_dir: Path,
    common_start: pd.Timestamp,
    common_end: pd.Timestamp,
) -> dict[str, Any]:
    frames = {
        venue: _frame_4h(_load_csv(Path(meta["transformed_path"])))
        for venue, meta in datasets.items()
    }
    periods: dict[str, dict[str, str]] = {
        "COMMON_OVERLAP": {"start": common_start.isoformat(), "end": common_end.isoformat()},
    }
    results: dict[str, Any] = {}
    summaries: dict[str, Any] = {}
    normal_results: dict[tuple[str, str], Any] = {}
    for venue, frame in frames.items():
        venue_end = min(pd.Timestamp(frame.index[-1]), common_end)
        venue_start = pd.Timestamp(frame.index[0])
        periods["MAX_AVAILABLE_HISTORY"] = periods.get("MAX_AVAILABLE_HISTORY", {})
        periods["MAX_AVAILABLE_HISTORY"][venue] = {
            "start": venue_start.isoformat(),
            "end": venue_end.isoformat(),
        }
        results[venue] = {}
        for strategy_name in STRATEGIES:
            normal = _run(frame, strategy_name)
            normal_results[(venue, strategy_name)] = normal
            max_metrics = _slice_metrics(normal, venue_start, venue_end + pd.Timedelta("4h"))
            common_metrics = _slice_metrics(normal, common_start, common_end)
            annual: dict[str, Any] = {}
            for year in range(common_start.year, common_end.year):
                start = pd.Timestamp(f"{year}-01-01", tz="UTC")
                end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
                annual[str(year)] = _slice_metrics(normal, start, end)
            cost_stress: dict[str, Any] = {}
            for multiplier in (1.0, 1.25, 1.5, 2.0):
                stressed = _run(frame, strategy_name, cost=multiplier)
                cost_stress[f"{multiplier:.1f}"] = _slice_metrics(
                    stressed, common_start, common_end
                )
            execution_stress = _run(frame, strategy_name, cost=1.5, stress=True)
            bootstrap = reference._bootstrap(
                reference._slice_equity(normal.equity, common_start, common_end),
                20260907 + len(results[venue]),
            )
            regime = reference._regime_summary(
                frame,
                reference._slice_equity(normal.equity, common_start, common_end),
            )
            results[venue][strategy_name] = {
                "max_history": max_metrics,
                "common_overlap": common_metrics,
                "annual": annual,
                "cost_stress": cost_stress,
                "execution_stress": _slice_metrics(execution_stress, common_start, common_end),
                "bootstrap": bootstrap,
                "regime": regime,
                "complexity": {
                    "parameters": len(_make_strategy(strategy_name).params),
                    "indicators": "shared causal indicators; exact count is strategy-family dependent",
                },
            }
    valid_venues = list(frames)
    for strategy_name in STRATEGIES:
        rows = [
            {
                "venue": venue,
                "common_metrics": results[venue][strategy_name]["common_overlap"],
                "cost_stress": results[venue][strategy_name]["cost_stress"],
                "execution_stress": results[venue][strategy_name]["execution_stress"],
            }
            for venue in valid_venues
        ]
        summaries[strategy_name] = _summarize(
            strategy_name, rows, summaries.get("CURRENT_TREND", {})
        )
    return {
        "periods": periods,
        "venues": results,
        "summary": summaries,
        "signal_agreement": _signal_agreement(frames, valid_venues),
        "leave_one_venue_out": {
            "status": "NOT_APPLICABLE",
            "reason": "only two new venues were valid; leave-one-venue-out was pre-registered only for three",
        },
        "normal_results_available": len(normal_results),
    }


def _binance_reproduction() -> dict[str, Any]:
    frame = discovery._load_frame()
    strategy = reference.TrendLS(
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
    )
    strategy.name = "trend_deployed"
    result = reference._trend_run(frame, strategy)
    oos = reference._metric_payload(
        reference._slice_equity(
            result.equity,
            pd.Timestamp("2024-01-01", tz="UTC"),
            pd.Timestamp("2026-01-01", tz="UTC"),
        ),
        [
            t
            for t in result.trades
            if pd.Timestamp("2024-01-01", tz="UTC")
            <= t.exit_time
            < pd.Timestamp("2026-01-01", tz="UTC")
        ],
    )
    return {
        "source": "existing Binance manifest and qualified benchmark engine",
        "oos_2024_2026": oos,
        "expected_reference_return": 0.020211594868589478,
        "expected_reference_sharpe": 0.20437141056797334,
        "within_reference_tolerance": abs(
            float(oos.get("total_return") or 0.0) - 0.020211594868589478
        )
        < 1e-6
        and abs(float(oos.get("sharpe") or 0.0) - 0.20437141056797334) < 1e-6,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preregistration", type=Path, default=PREREGISTRATION_DEFAULT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2019-01-01T00:00:00Z")
    parser.add_argument("--end-exclusive", default=FUTURE_CUTOFF.isoformat())
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    prereg_path: Path = args.preregistration
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    prereg_sha = sha256_file(prereg_path)
    start = pd.Timestamp(args.start)
    cutoff = pd.Timestamp(args.end_exclusive)
    if start.tzinfo is None or cutoff.tzinfo is None:
        raise SystemExit("start/end-exclusive must be timezone-aware UTC timestamps")
    start = start.tz_convert("UTC")
    cutoff = cutoff.tz_convert("UTC")
    if cutoff != FUTURE_CUTOFF:
        raise SystemExit("the frozen future-holdout cutoff cannot be changed")
    datasets: dict[str, dict[str, Any]] = {}
    exclusions: dict[str, str] = {}
    for venue_spec in prereg["venues"]:
        venue = str(venue_spec["venue"])
        try:
            datasets[venue] = acquire_venue(
                venue_spec,
                output_dir=output_dir,
                start=start,
                cutoff=cutoff,
                refresh=bool(args.refresh),
            )
        except VenueExcluded as exc:
            exclusions[venue] = str(exc)
    if len(datasets) < 2:
        raise SystemExit(f"fewer than two valid new venues: {exclusions}")
    starts = [pd.Timestamp(meta["quality"]["start"]) for meta in datasets.values()]
    ends = [pd.Timestamp(meta["quality"]["end"]) for meta in datasets.values()]
    common_start = max(starts)
    common_end = min(ends) + pd.Timedelta("1h")
    evaluation = evaluate(
        datasets, output_dir=output_dir, common_start=common_start, common_end=common_end
    )
    campaign_manifest = {
        "schema_version": 1,
        "campaign": "btcquant-cross-venue-validation",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_sha": git_value("rev-parse", "HEAD"),
        "source_tree": git_value("rev-parse", "HEAD^{tree}"),
        "preregistration_path": str(prereg_path),
        "preregistration_sha256": prereg_sha,
        "future_holdout_cutoff": cutoff.isoformat(),
        "future_holdout_touched": False,
        "reference_venue": "Binance (existing tracked manifest; not a new venue)",
        "datasets": datasets,
        "excluded_venues": exclusions,
        "common_overlap": {"start": common_start.isoformat(), "end": common_end.isoformat()},
        "quality_policy": "fail closed for invalid OHLCV; real gaps retained and never forward-filled",
    }
    manifest_path = output_dir / "cross_venue_dataset_manifest.json"
    manifest_path.write_text(json.dumps(campaign_manifest, indent=2), encoding="utf-8")
    report = {
        "schema_version": 1,
        "campaign": "btcquant-cross-venue-validation",
        "run_id": f"cross-venue-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{git_value('rev-parse', 'HEAD')[:12]}",
        "source_sha": campaign_manifest["source_sha"],
        "source_tree": campaign_manifest["source_tree"],
        "preregistration": {
            "path": str(prereg_path),
            "sha256": prereg_sha,
            "frozen": True,
            "strategies": prereg["frozen_strategies"],
        },
        "future_holdout_touched": False,
        "binance_reproduction": _binance_reproduction(),
        "dataset_manifest": {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
        },
        "new_venues_valid": len(datasets),
        "excluded_venues": exclusions,
        "evaluation": evaluation,
        "paper_changed": False,
        "testnet": "NOT_AUTHORIZED",
        "mainnet": "NOT_AUTHORIZED",
    }
    report_path = output_dir / "cross_venue_validation_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"report": str(report_path), "manifest": str(manifest_path), "excluded": exclusions},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
