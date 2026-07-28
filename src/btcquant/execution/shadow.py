"""Observation mainnet en lecture seule de cotations maker hypothétiques.

Ce module ne possède aucune primitive d'ordre. Il lit le meilleur bid/ask,
simule une cotation post-only à la touche puis mesure un *market-through*
conservateur, le fallback au marché et le markout. Un market-through reste un
proxy de fill : la position réelle dans la file est inconnue.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Literal, Protocol

from .execution_policy import (
    ExecutionEvidence,
    ExecutionPolicy,
    ExecutionQualificationPolicy,
    ExecutionSnapshot,
    evaluate_execution_evidence,
)
from .quality_metrics import percentile

Side = Literal["BUY", "SELL"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True)
class BookTop:
    observed_at: datetime
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    funding_rate_8h: float = 0.0
    seconds_to_funding: float | None = None

    def __post_init__(self) -> None:
        ExecutionSnapshot(
            bid=self.bid,
            ask=self.ask,
            funding_rate_8h=self.funding_rate_8h,
            seconds_to_funding=self.seconds_to_funding,
        )

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000.0


class PublicBookPort(Protocol):
    def top(self) -> BookTop: ...


class ShadowStore:
    """Base séparée du track record paper, reprise sûre des quotes en attente."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_quotes(
                    id INTEGER PRIMARY KEY,
                    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                    opened_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    reference_mid REAL NOT NULL CHECK(reference_mid > 0),
                    limit_price REAL NOT NULL CHECK(limit_price > 0),
                    initial_spread_bps REAL NOT NULL,
                    touched_at TEXT,
                    closed_at TEXT,
                    outcome TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK(outcome IN ('PENDING', 'TOUCHED', 'FALLBACK')),
                    execution_price REAL,
                    execution_fee_bps REAL,
                    all_in_cost_bps REAL,
                    markout_bps REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS shadow_quotes_outcome ON shadow_quotes(outcome)"
            )

    def begin_pair(self, book: BookTop, timeout_seconds: float) -> None:
        expires_at = book.observed_at.timestamp() + timeout_seconds
        expires = datetime.fromtimestamp(expires_at, tz=UTC)
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO shadow_quotes(
                    side, opened_at, expires_at, reference_mid, limit_price,
                    initial_spread_bps
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "BUY",
                        _iso(book.observed_at),
                        _iso(expires),
                        book.mid,
                        book.bid,
                        book.spread_bps,
                    ),
                    (
                        "SELL",
                        _iso(book.observed_at),
                        _iso(expires),
                        book.mid,
                        book.ask,
                        book.spread_bps,
                    ),
                ],
            )

    def pending(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_quotes WHERE outcome='PENDING' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_touched(self, quote_id: int, observed_at: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE shadow_quotes
                SET touched_at=COALESCE(touched_at, ?)
                WHERE id=? AND outcome='PENDING'
                """,
                (_iso(observed_at), quote_id),
            )

    def close(
        self,
        quote_id: int,
        *,
        outcome: Literal["TOUCHED", "FALLBACK"],
        book: BookTop,
        execution_price: float,
        fee_bps: float,
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM shadow_quotes WHERE id=? AND outcome='PENDING'",
                (quote_id,),
            ).fetchone()
            if row is None:
                return
            side = str(row["side"])
            reference_mid = float(row["reference_mid"])
            direction = 1.0 if side == "BUY" else -1.0
            slippage_bps = direction * (execution_price - reference_mid) / reference_mid * 10_000.0
            markout_bps = direction * (book.mid - execution_price) / execution_price * 10_000.0
            connection.execute(
                """
                UPDATE shadow_quotes
                SET closed_at=?, outcome=?, execution_price=?,
                    execution_fee_bps=?, all_in_cost_bps=?, markout_bps=?
                WHERE id=?
                """,
                (
                    _iso(book.observed_at),
                    outcome,
                    execution_price,
                    fee_bps,
                    slippage_bps + fee_bps,
                    markout_bps,
                    quote_id,
                ),
            )

    def rows(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM shadow_quotes ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict:
        rows = self.rows()
        terminal = [row for row in rows if row["outcome"] != "PENDING"]
        opened = [_parse(str(row["opened_at"])) for row in rows]
        closed = [_parse(str(row["closed_at"])) for row in terminal if row["closed_at"]]
        start = min(opened) if opened else None
        end = max(closed, default=_utc_now()) if start else None
        observation_days = (end - start).total_seconds() / 86_400.0 if start and end else 0.0
        touches = [row for row in terminal if row["outcome"] == "TOUCHED"]
        fallbacks = [row for row in terminal if row["outcome"] == "FALLBACK"]
        touch_seconds = [
            (_parse(str(row["touched_at"])) - _parse(str(row["opened_at"]))).total_seconds()
            for row in touches
            if row["touched_at"]
        ]
        costs = [float(row["all_in_cost_bps"]) for row in terminal]
        markouts = [float(row["markout_bps"]) for row in terminal]
        evidence = ExecutionEvidence(
            observation_days=observation_days,
            eligible_intents=len(terminal),
            post_only_fills=len(touches),
            fallback_orders=len(fallbacks),
            p95_fill_seconds=percentile(touch_seconds, 0.95),
            mean_all_in_cost_bps=mean(costs) if costs else None,
            p95_slippage_bps=percentile(costs, 0.95),
        )
        return {
            "schema_version": 1,
            "status": "SHADOW_PROXY_ONLY",
            "warning": "market-through ne prouve ni fill ni priorité dans la file",
            "started_at": _iso(start) if start else None,
            "ended_at": _iso(end) if end else None,
            "pending_quotes": len(rows) - len(terminal),
            "touch_proxy_rate": len(touches) / len(terminal) if terminal else None,
            "fallback_rate": len(fallbacks) / len(terminal) if terminal else None,
            "mean_markout_bps": mean(markouts) if markouts else None,
            "evidence": asdict(evidence),
            "proxy_qualification": evaluate_execution_evidence(
                evidence,
                ExecutionQualificationPolicy(),
            ),
        }

    def summary_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class ShadowConfig:
    quote_interval_seconds: float = 60.0
    poll_interval_seconds: float = 2.0
    maker_timeout_seconds: float = 30.0
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.0
    notional: float = 1_000.0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value <= 0:
                raise ValueError(f"{name} doit être strictement positif")
        if self.quote_interval_seconds < self.maker_timeout_seconds:
            raise ValueError("quote_interval_seconds doit être >= maker_timeout_seconds")


class ShadowCollector:
    def __init__(
        self,
        market: PublicBookPort,
        store: ShadowStore,
        config: ShadowConfig | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self.market = market
        self.store = store
        self.config = config or ShadowConfig()
        self.policy = policy or ExecutionPolicy(
            maker_timeout_seconds=self.config.maker_timeout_seconds
        )
        self._last_pair_at: datetime | None = None

    @staticmethod
    def _market_through(quote: dict, book: BookTop) -> bool:
        if quote["side"] == "BUY":
            return book.ask <= float(quote["limit_price"])
        return book.bid >= float(quote["limit_price"])

    def observe(self, book: BookTop) -> None:
        for quote in self.store.pending():
            if quote["touched_at"] is None and self._market_through(quote, book):
                self.store.mark_touched(int(quote["id"]), book.observed_at)
                quote["touched_at"] = _iso(book.observed_at)
            if book.observed_at < _parse(str(quote["expires_at"])):
                continue
            touched = quote["touched_at"] is not None
            outcome: Literal["TOUCHED", "FALLBACK"] = "TOUCHED" if touched else "FALLBACK"
            if touched:
                execution_price = float(quote["limit_price"])
                fee_bps = self.config.maker_fee_bps
            else:
                execution_price = book.ask if quote["side"] == "BUY" else book.bid
                fee_bps = self.config.taker_fee_bps
            self.store.close(
                int(quote["id"]),
                outcome=outcome,
                book=book,
                execution_price=execution_price,
                fee_bps=fee_bps,
            )

        if self.store.pending():
            return

        if self._last_pair_at is not None:
            elapsed = (book.observed_at - self._last_pair_at).total_seconds()
            if elapsed < self.config.quote_interval_seconds:
                return
        snapshot = ExecutionSnapshot(
            bid=book.bid,
            ask=book.ask,
            funding_rate_8h=book.funding_rate_8h,
            seconds_to_funding=book.seconds_to_funding,
        )
        decisions = [
            self.policy.decide(
                side=side,
                notional=self.config.notional,
                snapshot=snapshot,
            )
            for side in ("BUY", "SELL")
        ]
        if all(decision.action == "POST_ONLY" for decision in decisions):
            self.store.begin_pair(book, self.config.maker_timeout_seconds)
            self._last_pair_at = book.observed_at

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stopped = stop_event or threading.Event()
        while not stopped.is_set():
            self.observe(self.market.top())
            stopped.wait(self.config.poll_interval_seconds)
