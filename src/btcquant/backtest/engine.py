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

from ..domain import (
    EntryRequested,
    ExitRequested,
    PyramidRequested,
    decide_bar_close,
    funding_amount,
)
from ..domain.execution import ExecutionConfig, ExecutionSimulator, MarketOrder, OrderSide
from ..indicators import bars_per_year, realized_vol
from ..risk import KillSwitch, RiskConfig, position_size
from ..strategies.base import Direction, Position, Strategy
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


@dataclass
class _BacktestState:
    cash: float
    position: Position | None = None
    entry_fee_pending: float = 0.0
    pending_entry: tuple[pd.Series, int] | None = None
    pending_pyramid: tuple[pd.Series, float] | None = None
    pending_exit_reason: str | None = None
    pending_exit_volatility: float | None = None
    trades: list[Trade] = field(default_factory=list)


class BacktestEngine:
    def __init__(
        self,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        risk: RiskConfig | None = None,
        funding_rate_8h: float = 0.0,
        allow_short: bool = False,
        short_size_mult: float = 1.0,
        execution_simulator: ExecutionSimulator | None = None,
    ) -> None:
        self.execution = execution_simulator or ExecutionSimulator(
            ExecutionConfig(fee_rate=fee_rate, slippage_bps=slippage_bps)
        )
        self.fee_rate = self.execution.config.fee_rate
        self.slippage = self.execution.config.slippage_bps / 10_000.0
        self.risk = risk or RiskConfig()
        self.funding_rate_8h = funding_rate_8h
        self.allow_short = allow_short
        #: multiplicateur de taille des shorts (1.0 = symétrique, <1 = tilt
        #: net-long, 0 = long-only). N'affecte pas le signal, seulement le sizing.
        self.short_size_mult = short_size_mult

    @staticmethod
    def _fill_exit(
        state: _BacktestState,
        execution: ExecutionSimulator,
        strategy_name: str,
        raw_price: float,
        ts: pd.Timestamp,
        reason: str,
        available_volume: float,
        volatility_annual: float | None = None,
    ) -> bool:
        position = state.position
        assert position is not None
        requested_qty = position.qty
        side = OrderSide.SELL if position.direction == 1 else OrderSide.BUY
        fill = execution.execute_market(
            MarketOrder(
                order_id=(
                    f"backtest:{strategy_name}:{ts.isoformat()}:exit:"
                    f"{reason}:{position.direction}:{requested_qty:.17g}"
                ),
                side=side,
                qty=requested_qty,
                reference_price=float(raw_price),
                available_volume=float(available_volume),
                volatility_annual=volatility_annual,
            )
        )
        if fill.qty <= 0:
            return False
        entry_fee_share = state.entry_fee_pending * (fill.qty / requested_qty)
        gross = position.direction * fill.qty * (fill.price - position.entry_price)
        exit_fee = fill.fee
        state.cash += gross - exit_fee
        pnl = gross - exit_fee - entry_fee_share
        cost_basis = fill.qty * position.entry_price
        state.trades.append(
            Trade(
                entry_time=position.entry_time,
                exit_time=ts,
                entry_price=position.entry_price,
                exit_price=fill.price,
                qty=fill.qty,
                direction=position.direction,
                pnl=pnl,
                pnl_pct=pnl / cost_basis if cost_basis else 0.0,
                bars_held=position.bars_held,
                exit_reason=reason,
            )
        )
        if fill.qty < requested_qty:
            position.qty -= fill.qty
            state.entry_fee_pending -= entry_fee_share
            return False
        state.position = None
        state.entry_fee_pending = 0.0
        return True

    def _open_pending(
        self,
        state: _BacktestState,
        execution: ExecutionSimulator,
        strategy: Strategy,
        kill: KillSwitch,
        ts: pd.Timestamp,
        open_price: float,
        available_volume: float,
    ) -> None:
        pending = state.pending_entry
        # Une demande d'entrée vaut exclusivement pour cette ouverture, comme
        # dans le runner live qui marque ensuite la barre comme traitée. Un
        # rejet est terminal : rejouer plus tard un signal devenu ancien
        # créerait une divergence temporelle plus grave qu'un trade manqué.
        state.pending_entry = None
        if state.position is not None or pending is None or not kill.can_trade:
            return
        signal_row, direction = pending
        side = OrderSide.BUY if direction == 1 else OrderSide.SELL
        realized_volatility = signal_row["_rvol"]
        volatility_annual = float(realized_volatility) if pd.notna(realized_volatility) else None
        quoted_price = execution.quote_price(
            side,
            float(open_price),
            volatility_annual=volatility_annual,
        )
        stop = strategy.initial_stop(signal_row, quoted_price, direction)
        qty = position_size(
            state.cash,
            quoted_price,
            stop,
            float(realized_volatility) if pd.notna(realized_volatility) else None,
            self.risk,
            direction=Direction(direction),
        )
        qty *= strategy.position_size_multiplier(signal_row, direction)
        if direction == -1:
            qty *= self.short_size_mult
        if qty <= 0:
            return
        fill = execution.execute_market(
            MarketOrder(
                order_id=f"backtest:{strategy.name}:{ts.isoformat()}:entry:{direction}",
                side=side,
                qty=qty,
                reference_price=float(open_price),
                available_volume=float(available_volume),
                volatility_annual=volatility_annual,
            )
        )
        if fill.qty <= 0:
            return
        stop = strategy.initial_stop(signal_row, fill.price, direction)
        state.entry_fee_pending = fill.fee
        state.cash -= state.entry_fee_pending
        state.position = Position(
            entry_time=ts,
            entry_price=fill.price,
            qty=fill.qty,
            stop_price=stop,
            direction=Direction(direction),
            best_close=fill.price,
        )

    def _add_pending_pyramid(
        self,
        state: _BacktestState,
        execution: ExecutionSimulator,
        strategy: Strategy,
        ts: pd.Timestamp,
        open_price: float,
        available_volume: float,
    ) -> None:
        pending = state.pending_pyramid
        state.pending_pyramid = None
        position = state.position
        if pending is None or position is None:
            return
        signal_row, fraction = pending
        requested_qty = position.initial_qty * fraction
        equity = state.cash + position.unrealized(open_price)
        max_total_qty = (
            equity * self.risk.max_position_pct * self.risk.max_leverage / open_price
        )
        requested_qty = min(requested_qty, max_total_qty - position.qty)
        if requested_qty <= 0:
            return
        side = OrderSide.BUY if position.direction == 1 else OrderSide.SELL
        realized_volatility = signal_row.get("_rvol")
        volatility_annual = (
            float(realized_volatility)
            if realized_volatility is not None and pd.notna(realized_volatility)
            else None
        )
        fill = execution.execute_market(
            MarketOrder(
                order_id=(
                    f"backtest:{strategy.name}:{ts.isoformat()}:pyramid:"
                    f"{position.direction}:{position.pyramid_adds + 1}"
                ),
                side=side,
                qty=requested_qty,
                reference_price=open_price,
                available_volume=available_volume,
                volatility_annual=volatility_annual,
            )
        )
        if fill.qty <= 0:
            return
        previous_qty = position.qty
        total_qty = previous_qty + fill.qty
        position.entry_price = (
            previous_qty * position.entry_price + fill.qty * fill.price
        ) / total_qty
        position.qty = total_qty
        position.last_add_price = fill.price
        position.pyramid_adds += 1
        state.cash -= fill.fee
        state.entry_fee_pending += fill.fee

    def _process_intrabar(
        self,
        state: _BacktestState,
        execution: ExecutionSimulator,
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
        position = state.position
        if position is None:
            return
        stop_reference = execution.stop_trigger_price(
            direction=position.direction,
            open_price=float(open_price),
            high_price=float(high_price),
            low_price=float(low_price),
            stop_price=position.stop_price,
        )
        intrabar_cost = funding_amount(position, funding_rate, float(close_price))
        # Convention déterministe : le paiement associé à cette barre est
        # appliqué avant une éventuelle sortie intrabar, quel que soit son
        # signe. Ne débiter que les coûts tout en ignorant les crédits sur les
        # barres stoppées introduisait un biais asymétrique.
        state.cash -= intrabar_cost
        if stop_reference is not None and not self._fill_exit(
            state,
            execution,
            strategy_name,
            stop_reference,
            ts,
            "stop",
            available_volume,
            volatility_annual,
        ):
            state.pending_exit_reason = "stop"
            state.pending_exit_volatility = volatility_annual

    def _decide_close(
        self,
        state: _BacktestState,
        strategy: Strategy,
        row: pd.Series,
        ts: pd.Timestamp,
        kill: KillSwitch,
        no_trade_before: pd.Timestamp | None,
    ) -> None:
        if state.position is not None:
            decision = decide_bar_close(strategy, row, state.position, halted=kill.halted)
            state.position = decision.position
            for event in decision.events:
                if isinstance(event, ExitRequested):
                    state.pending_exit_reason = event.reason
                    realized_volatility = row.get("_rvol")
                    state.pending_exit_volatility = (
                        float(realized_volatility)
                        if realized_volatility is not None and pd.notna(realized_volatility)
                        else None
                    )
                elif isinstance(event, PyramidRequested):
                    state.pending_pyramid = (row, event.fraction)
            return
        allowed = no_trade_before is None or ts >= no_trade_before
        decision = decide_bar_close(
            strategy,
            row,
            None,
            can_enter=allowed and kill.can_trade,
            allow_short=self.allow_short,
        )
        for event in decision.events:
            if isinstance(event, EntryRequested):
                state.pending_entry = (row, event.direction)

    def run(
        self,
        strategy: Strategy,
        df: pd.DataFrame,
        no_trade_before: pd.Timestamp | None = None,
    ) -> BacktestResult:
        # Une session par run évite que deux plis walk-forward partageant les
        # mêmes timestamps ne rejouent les fills idempotents l'un de l'autre.
        execution = self.execution.fresh()
        bpy = bars_per_year(strategy.timeframe)
        funding_per_bar = self.funding_rate_8h * (8_760.0 / bpy) / 8.0
        data = strategy.prepare(df)
        data["_rvol"] = realized_vol(data["close"], VOL_LOOKBACK, bpy)
        # Les paiements à l'ouverture concernent la position détenue AVANT les
        # ordres de cette ouverture. Les autres paiements de la barre concernent
        # la position détenue ensuite. Les anciennes données ne portant qu'une
        # somme par barre restent compatibles, classées après l'ouverture.
        if {"funding_at_open", "funding_after_open"} <= set(data.columns):
            funding_open_arr = data["funding_at_open"].fillna(0.0).to_numpy()
            funding_intrabar_arr = data["funding_after_open"].fillna(0.0).to_numpy()
        elif "funding_rate" in data.columns:
            funding_open_arr = None
            funding_intrabar_arr = data["funding_rate"].fillna(0.0).to_numpy()
        else:
            funding_open_arr = None
            funding_intrabar_arr = None

        start = strategy.warmup_bars()
        if len(data) <= start + 2:
            raise ValueError("Pas assez de données après la période de chauffe")

        state = _BacktestState(cash=self.risk.initial_capital)
        kill = KillSwitch(self.risk)
        equity_index: list[pd.Timestamp] = []
        equity_values: list[float] = []
        exposure_bars = 0

        opens = data["open"].to_numpy()
        highs = data["high"].to_numpy()
        lows = data["low"].to_numpy()
        closes = data["close"].to_numpy()
        volumes = data["volume"].to_numpy()
        realized_volatilities = data["_rvol"].to_numpy()
        index = data.index

        for i in range(start, len(data)):
            ts = index[i]
            row = data.iloc[i]
            exposed_during_bar = state.position is not None

            # Un snapshot de funding à l'instant t précède les ordres exécutés
            # à l'ouverture t : une nouvelle position ne doit jamais recevoir
            # rétroactivement ce paiement.
            if state.position is not None and funding_open_arr is not None:
                state.cash -= funding_amount(
                    state.position,
                    float(funding_open_arr[i]),
                    float(opens[i]),
                )

            # ── ouverture : sortie décidée à la clôture précédente ──────────
            if state.position is not None and state.pending_exit_reason is not None:
                exit_reason = state.pending_exit_reason
                exit_volatility = state.pending_exit_volatility
                state.pending_exit_reason = None
                state.pending_exit_volatility = None
                if not self._fill_exit(
                    state,
                    execution,
                    strategy.name,
                    opens[i],
                    ts,
                    exit_reason,
                    volumes[i],
                    exit_volatility,
                ):
                    state.pending_exit_reason = exit_reason
                    state.pending_exit_volatility = exit_volatility
            else:
                state.pending_exit_reason = None
                state.pending_exit_volatility = None

            # ── ouverture : entrée décidée à la clôture précédente ──────────
            self._open_pending(
                state,
                execution,
                strategy,
                kill,
                ts,
                float(opens[i]),
                float(volumes[i]),
            )
            self._add_pending_pyramid(
                state,
                execution,
                strategy,
                ts,
                float(opens[i]),
                float(volumes[i]),
            )
            exposed_during_bar = exposed_during_bar or state.position is not None

            # ── intrabar : stop touché ? ─────────────────────────────────────
            intrabar_rate = (
                float(funding_intrabar_arr[i])
                if funding_intrabar_arr is not None
                else funding_per_bar
            )
            self._process_intrabar(
                state,
                execution,
                strategy.name,
                ts,
                float(opens[i]),
                float(highs[i]),
                float(lows[i]),
                float(closes[i]),
                float(volumes[i]),
                intrabar_rate,
                (
                    float(realized_volatilities[i - 1])
                    if i > 0 and pd.notna(realized_volatilities[i - 1])
                    else None
                ),
            )

            # Le risque est mis à jour AVANT les décisions de clôture. Ainsi,
            # un kill switch déclenché à t programme la liquidation à
            # l'ouverture t+1, et non à t+2.
            equity = state.cash + (state.position.unrealized(closes[i]) if state.position else 0.0)
            kill.update(equity, ts.date())
            # ── clôture : gestion de la position, signaux pour t+1 ─────────
            self._decide_close(state, strategy, row, ts, kill, no_trade_before)
            exposure_bars += int(exposed_during_bar)
            equity_index.append(ts)
            equity_values.append(equity)

        # position encore ouverte en fin d'historique : clôture à la dernière barre
        if state.position is not None:
            self._fill_exit(
                state,
                execution,
                strategy.name,
                closes[-1],
                index[-1],
                "end_of_data",
                volumes[-1],
                (float(realized_volatilities[-1]) if pd.notna(realized_volatilities[-1]) else None),
            )
            equity_values[-1] = state.cash + (
                state.position.unrealized(closes[-1]) if state.position is not None else 0.0
            )

        equity_series = pd.Series(
            equity_values, index=pd.DatetimeIndex(equity_index), name="equity"
        )
        metrics = compute_metrics(
            equity_series,
            state.trades,
            bpy,
            buy_hold=data["close"].iloc[start:],
            exposure_bars=exposure_bars,
        )
        return BacktestResult(
            equity=equity_series,
            trades=state.trades,
            metrics=metrics,
            strategy_name=strategy.name,
            params=dict(strategy.params),
        )
