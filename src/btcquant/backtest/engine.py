"""Moteur de backtest barre par barre, sans look-ahead, long-short.

Séquencement par barre t :
  À L'OUVERTURE de t   : exécution des ordres décidés à la clôture de t-1
                         (sortie puis entrée), au prix d'ouverture ± slippage.
  EN INTRABAR          : stop touché ? long : low <= stop → fill à
                         min(open, stop) ; short : high >= stop → fill à
                         max(open, stop). Hypothèse conservatrice : les gaps
                         remplissent au pire prix.
  À LA CLÔTURE de t    : mise à jour du stop suiveur (resserré uniquement),
                         funding éventuel (perpétuels), évaluation des signaux
                         pour t+1, marquage de l'équity, coupe-circuits.

Comptabilité sur marge (compatible spot 1x et perpétuels sans levier) :
  équity = cash + PnL latent ; les frais sont débités à chaque exécution ;
  le funding est échangé chaque barre au prorata (les longs paient un taux
  positif, les shorts le reçoivent).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from ..indicators import bars_per_year, realized_vol
from ..risk import KillSwitch, RiskConfig, position_size
from ..strategies.base import Position, Strategy
from .metrics import compute_metrics

log = logging.getLogger(__name__)

VOL_LOOKBACK = 30  # barres utilisées pour la volatilité réalisée (sizing)


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    direction: int
    pnl: float
    pnl_pct: float
    bars_held: int
    exit_reason: str


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade]
    metrics: dict
    strategy_name: str
    params: dict = field(default_factory=dict)

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.__dict__ for t in self.trades])


class BacktestEngine:
    def __init__(
        self,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        risk: RiskConfig | None = None,
        funding_rate_8h: float = 0.0,
        allow_short: bool = False,
    ) -> None:
        self.fee_rate = fee_rate
        self.slippage = slippage_bps / 10_000.0
        self.risk = risk or RiskConfig()
        self.funding_rate_8h = funding_rate_8h
        self.allow_short = allow_short

    def run(
        self,
        strategy: Strategy,
        df: pd.DataFrame,
        no_trade_before: pd.Timestamp | None = None,
    ) -> BacktestResult:
        bpy = bars_per_year(strategy.timeframe)
        funding_per_bar = self.funding_rate_8h * (8_760.0 / bpy) / 8.0
        data = strategy.prepare(df)
        data["_rvol"] = realized_vol(data["close"], VOL_LOOKBACK, bpy)

        start = strategy.warmup_bars()
        if len(data) <= start + 2:
            raise ValueError("Pas assez de données après la période de chauffe")

        cash = self.risk.initial_capital
        position: Position | None = None
        entry_fee_pending = 0.0  # frais d'entrée de la position ouverte (pour le PnL du trade)
        pending_entry: tuple[pd.Series, int] | None = None  # (ligne du signal, direction)
        pending_exit_reason: str | None = None
        kill = KillSwitch(self.risk)
        trades: list[Trade] = []
        equity_index: list[pd.Timestamp] = []
        equity_values: list[float] = []

        opens = data["open"].to_numpy()
        highs = data["high"].to_numpy()
        lows = data["low"].to_numpy()
        closes = data["close"].to_numpy()
        index = data.index

        def fill_exit(raw_price: float, ts: pd.Timestamp, reason: str) -> None:
            nonlocal cash, position, entry_fee_pending
            assert position is not None
            exec_price = raw_price * (1.0 - position.direction * self.slippage)
            exit_fee = position.qty * exec_price * self.fee_rate
            gross = position.direction * position.qty * (exec_price - position.entry_price)
            cash += gross - exit_fee
            pnl = gross - exit_fee - entry_fee_pending
            cost_basis = position.qty * position.entry_price
            trades.append(
                Trade(
                    entry_time=position.entry_time,
                    exit_time=ts,
                    entry_price=position.entry_price,
                    exit_price=exec_price,
                    qty=position.qty,
                    direction=position.direction,
                    pnl=pnl,
                    pnl_pct=pnl / cost_basis if cost_basis else 0.0,
                    bars_held=position.bars_held,
                    exit_reason=reason,
                )
            )
            position = None
            entry_fee_pending = 0.0

        for i in range(start, len(data)):
            ts = index[i]
            row = data.iloc[i]

            # ── ouverture : sortie décidée à la clôture précédente ──────────
            if position is not None and pending_exit_reason is not None:
                fill_exit(opens[i], ts, pending_exit_reason)
            pending_exit_reason = None

            # ── ouverture : entrée décidée à la clôture précédente ──────────
            if position is None and pending_entry is not None and kill.can_trade:
                signal_row, direction = pending_entry
                exec_price = opens[i] * (1.0 + direction * self.slippage)
                stop = strategy.initial_stop(signal_row, exec_price, direction)
                rvol = signal_row["_rvol"]
                qty = position_size(
                    cash, exec_price, stop,
                    float(rvol) if pd.notna(rvol) else None, self.risk,
                    direction=direction,
                )
                if qty > 0:
                    entry_fee_pending = qty * exec_price * self.fee_rate
                    cash -= entry_fee_pending
                    position = Position(
                        entry_time=ts,
                        entry_price=exec_price,
                        qty=qty,
                        stop_price=stop,
                        direction=direction,
                        best_close=exec_price,
                    )
            pending_entry = None

            # ── intrabar : stop touché ? ─────────────────────────────────────
            if position is not None:
                if position.direction == 1 and lows[i] <= position.stop_price:
                    fill_exit(min(opens[i], position.stop_price), ts, "stop")
                elif position.direction == -1 and highs[i] >= position.stop_price:
                    fill_exit(max(opens[i], position.stop_price), ts, "stop")

            # ── clôture : funding, gestion de la position, signaux pour t+1 ─
            if position is not None:
                if funding_per_bar:
                    cash -= position.direction * position.qty * closes[i] * funding_per_bar
                position.bars_held += 1
                if position.direction == 1:
                    position.best_close = max(position.best_close, closes[i])
                else:
                    position.best_close = min(position.best_close, closes[i])
                new_stop = strategy.trailing_stop(row, position)
                if new_stop is not None:
                    if position.direction == 1 and new_stop > position.stop_price:
                        position.stop_price = new_stop
                    elif position.direction == -1 and new_stop < position.stop_price:
                        position.stop_price = new_stop
                if kill.halted:
                    pending_exit_reason = "kill_switch"
                elif strategy.exit_signal(row, position):
                    pending_exit_reason = "signal"
            else:
                allowed = no_trade_before is None or ts >= no_trade_before
                if allowed and kill.can_trade:
                    direction = int(strategy.entry_signal(row))
                    if direction == -1 and not self.allow_short:
                        direction = 0
                    if direction != 0:
                        pending_entry = (row, direction)

            equity = cash + (position.unrealized(closes[i]) if position else 0.0)
            kill.update(equity, ts.date())
            equity_index.append(ts)
            equity_values.append(equity)

        # position encore ouverte en fin d'historique : clôture à la dernière barre
        if position is not None:
            fill_exit(closes[-1], index[-1], "end_of_data")
            equity_values[-1] = cash

        equity_series = pd.Series(equity_values, index=pd.DatetimeIndex(equity_index), name="equity")
        metrics = compute_metrics(
            equity_series, trades, bpy,
            buy_hold=data["close"].iloc[start:],
        )
        return BacktestResult(
            equity=equity_series,
            trades=trades,
            metrics=metrics,
            strategy_name=strategy.name,
            params=dict(strategy.params),
        )
