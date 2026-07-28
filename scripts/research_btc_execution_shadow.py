"""Collecte publique du carnet BTC et test shadow de la politique d'exécution."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.execution.execution_policy import (
    ExecutionEvidence,
    ExecutionPolicy,
    ExecutionQualificationPolicy,
    ExecutionSnapshot,
    evaluate_execution_evidence,
)
from btcquant.execution.state_store import StateStore
from btcquant.execution.venue import Venue

OUTPUT = ROOT / "audit" / "btc_execution_shadow.json"
STATE_DB = ROOT / "state" / "btcquant.db"


def _seconds_to_next_hour(now: pd.Timestamp) -> float:
    next_hour = now.floor("h") + pd.Timedelta(hours=1)
    return float((next_hour - now).total_seconds())


def _collect_snapshot(venue: Venue, funding_rate_8h: float) -> dict:
    book = venue.exchange.fetch_order_book(venue.symbol, limit=20)
    if not book["bids"] or not book["asks"]:
        raise RuntimeError("Carnet Hyperliquid vide")
    bid_price, bid_qty = map(float, book["bids"][0][:2])
    ask_price, ask_qty = map(float, book["asks"][0][:2])
    now = pd.Timestamp.now(tz="UTC")
    snapshot = ExecutionSnapshot(
        bid=bid_price,
        ask=ask_price,
        funding_rate_8h=funding_rate_8h,
        seconds_to_funding=_seconds_to_next_hour(now),
    )
    return {
        "observed_at": now.isoformat(),
        "bid": bid_price,
        "ask": ask_price,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "spread_bps": snapshot.spread_bps,
        "funding_rate_8h": funding_rate_8h,
        "seconds_to_funding": snapshot.seconds_to_funding,
    }


def _touch_summary(observations: list[dict]) -> dict:
    buy_quotes = 0
    sell_quotes = 0
    buy_market_through = 0
    sell_market_through = 0
    for index, row in enumerate(observations[:-1]):
        future = observations[index + 1 :]
        buy_quotes += 1
        sell_quotes += 1
        buy_market_through += any(item["ask"] <= row["bid"] for item in future)
        sell_market_through += any(item["bid"] >= row["ask"] for item in future)
    return {
        "buy_quotes": buy_quotes,
        "sell_quotes": sell_quotes,
        "buy_market_through": buy_market_through,
        "sell_market_through": sell_market_through,
        "interpretation": (
            "market-through est une borne conservatrice de touche, jamais une preuve "
            "de fill ni de priorité dans la file"
        ),
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--notional", type=float, default=1_000.0)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    if args.samples < 1 or args.interval_seconds < 0:
        raise ValueError("samples doit être >= 1 et interval-seconds >= 0")
    venue = Venue("hyperliquid", "BTC/USDC:USDC")
    funding_rate_8h = venue.funding_rate_8h()
    observations = []
    for index in range(args.samples):
        observations.append(_collect_snapshot(venue, funding_rate_8h))
        if index + 1 < args.samples:
            time.sleep(args.interval_seconds)

    policy = ExecutionPolicy()
    last = observations[-1]
    snapshot = ExecutionSnapshot(
        bid=last["bid"],
        ask=last["ask"],
        funding_rate_8h=last["funding_rate_8h"],
        seconds_to_funding=last["seconds_to_funding"],
    )
    store = StateStore(STATE_DB)
    existing_orders = [
        order for order in store.read_orders() if order["order_type"] != "STOP"
    ]
    qualification_policy = ExecutionQualificationPolicy()
    evidence = ExecutionEvidence(
        observation_days=0.0,
        eligible_intents=0,
        post_only_fills=0,
        fallback_orders=0,
        p95_fill_seconds=None,
        mean_all_in_cost_bps=None,
        p95_slippage_bps=None,
    )
    spreads = [row["spread_bps"] for row in observations]
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "venue": "hyperliquid",
        "symbol": "BTC/USDC:USDC",
        "provenance": {
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "policy_sha256": hashlib.sha256(
                (ROOT / "src" / "btcquant" / "execution" / "execution_policy.py").read_bytes()
            ).hexdigest(),
        },
        "policy": asdict(policy),
        "sample": {
            "count": len(observations),
            "interval_seconds": args.interval_seconds,
            "spread_bps_min": min(spreads),
            "spread_bps_mean": sum(spreads) / len(spreads),
            "spread_bps_max": max(spreads),
            "observations": observations,
        },
        "decisions_at_last_snapshot": {
            side: asdict(
                policy.decide(
                    side=side,
                    notional=args.notional,
                    snapshot=snapshot,
                )
            )
            for side in ("BUY", "SELL")
        },
        "maker_touch_proxy": _touch_summary(observations),
        "real_order_evidence": {
            "non_stop_orders_in_state_db": len(existing_orders),
            "can_estimate_real_fill_rate": bool(existing_orders),
        },
        "qualification": {
            "policy": asdict(qualification_policy),
            "evidence": asdict(evidence),
            "result": evaluate_execution_evidence(evidence, qualification_policy),
        },
        "activation_status": "SHADOW_ONLY",
        "activation_blocker": (
            "Un taux de fill maker exige des intentions shadow sur plusieurs signaux "
            "et, ensuite, des ordres testnet; le carnet public seul ne prouve pas un fill."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(observations)} snapshots | spread "
        f"{payload['sample']['spread_bps_mean']:.3f} bps moyen | "
        f"BUY={payload['decisions_at_last_snapshot']['BUY']['action']} | "
        f"SELL={payload['decisions_at_last_snapshot']['SELL']['action']}"
    )
    print(f"Ordres réels mesurables : {len(existing_orders)}")
    print(f"Artefact : {args.output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
