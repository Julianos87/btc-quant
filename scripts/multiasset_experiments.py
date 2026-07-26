"""Validation des 3 pistes issues de la recherche (arXiv 2602.11708 & al.) :
  #1 diversification multi-actifs (BTC + ETH + SOL en un portefeuille),
  #2 funding réel dans le backtest (au lieu de la constante plate),
  #3 tilt net-long (shorts sous-dimensionnés) vs symétrique.

Discipline inchangée : rien n'est adopté sans tenir hors-échantillon. Les params
structurels (Donchian 20/55/100, EMA, ADX, stop 3×ATR) restent GELÉS — on ne
teste QUE la structure de portefeuille, pas de nouveaux paramètres à optimiser.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest.engine import BacktestEngine
from btcquant.backtest.metrics import compute_metrics
from btcquant.carry import add_funding_columns, load_funding
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.indicators import bars_per_year
from btcquant.risk import RiskConfig
from btcquant.strategies.trend_ls import TrendLS

TIMEFRAME = "4h"
BPY = bars_per_year(TIMEFRAME)
HORIZONS = [20, 55, 100]
BASE_PARAMS = {"adx_min": 20}  # funding filter inactif en backtest (pas de colonne)
PERP_FEE, SLIPPAGE_BPS, FUNDING_8H, CAPITAL = 0.0005, 5.0, 0.0001, 10_000.0
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def _engine(sleeve_capital: float, short_mult: float = 1.0) -> BacktestEngine:
    risk = RiskConfig(
        initial_capital=sleeve_capital,
        risk_per_trade=0.0075,
        max_position_pct=0.95,
        vol_target_annual=0.40,
        max_drawdown_halt=0.30,
        daily_loss_limit=None,
        max_leverage=1.0,
    )
    return BacktestEngine(
        fee_rate=PERP_FEE,
        slippage_bps=SLIPPAGE_BPS,
        risk=risk,
        funding_rate_8h=FUNDING_8H,
        allow_short=True,
        short_size_mult=short_mult,
    )


def add_real_funding(df: pd.DataFrame, symbol_perp: str) -> pd.DataFrame:
    """Ajoute `funding_rate` (P&L) et `funding` (filtre d'entrée) à partir du
    funding réel 8 h. Convention : positif = les longs paient."""
    fund = load_funding(symbol_perp, data_dir=ROOT / "data", refresh=False)
    return add_funding_columns(df, fund, TIMEFRAME_TO_PANDAS[TIMEFRAME])


def symbol_ensemble(
    df: pd.DataFrame, sleeve: float, stop_cfg: dict | None = None, short_mult: float = 1.0
) -> pd.Series:
    """Équity de l'ensemble 3-horizons pour UN actif (chaque horizon = sleeve/3)."""
    stop_cfg = stop_cfg or {}
    curves = []
    for donchian in HORIZONS:
        strat = TrendLS(donchian=donchian, **BASE_PARAMS, **stop_cfg)
        res = _engine(sleeve / 3.0, short_mult).run(strat, df)
        curves.append(res.equity)
    return pd.concat(curves, axis=1, sort=True).ffill().dropna().sum(axis=1)


def portfolio(dfs: dict[str, pd.DataFrame], stop_cfg: dict | None = None) -> pd.Series:
    """Portefeuille équipondéré entre actifs ; chaque sleeve = CAPITAL/n_actifs."""
    n = len(dfs)
    sleeves = [symbol_ensemble(df, CAPITAL / n, stop_cfg) for df in dfs.values()]
    combined = pd.concat(sleeves, axis=1, sort=True).ffill().dropna().sum(axis=1)
    return combined


def block_bootstrap_sharpe_diff(ret_a, ret_b, block=30, n_boot=3000, seed=0):
    idx = ret_a.index.intersection(ret_b.index)
    a, b = ret_a.reindex(idx).to_numpy(), ret_b.reindex(idx).to_numpy()
    m = len(a)
    sq = np.sqrt(BPY)
    sharpe = lambda x: x.mean() / x.std() * sq if x.std() > 0 else 0.0
    obs = sharpe(a) - sharpe(b)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(m / block))
    wins = 0
    for _ in range(n_boot):
        starts = rng.integers(0, m - block + 1, size=nb)
        sel = np.concatenate([np.arange(s, s + block) for s in starts])[:m]
        if sharpe(a[sel]) - sharpe(b[sel]) > 0:
            wins += 1
    return obs, 1.0 - wins / n_boot


def load_all() -> dict[str, pd.DataFrame]:
    out = {}
    for sym in SYMBOLS:
        base = load_ohlcv("binance", sym, "1h", "2019-01-01", data_dir=ROOT / "data", refresh=False)
        out[sym] = resample(base, TIMEFRAME_TO_PANDAS[TIMEFRAME])
    return out


def main() -> None:
    dfs = load_all()
    common_start = max(df.index[0] for df in dfs.values())
    print(
        f"Fenêtre commune : {common_start.date()} → {min(df.index[-1] for df in dfs.values()).date()}\n"
    )

    # Perf par actif (ensemble seul), sur son propre historique
    print("═══ Ensemble trend par actif (plein historique de l'actif) ═══")
    for sym, df in dfs.items():
        eq = symbol_ensemble(df, CAPITAL)
        m = compute_metrics(eq, [], BPY)
        print(
            f"  {sym:10} Sharpe {m['sharpe']:.2f}  CAGR {m['cagr']:+.1%}  maxDD {m['max_drawdown']:+.1%}"
        )

    # Comparaison honnête sur la FENÊTRE COMMUNE
    print("\n═══ #1 Diversification — comparaison sur fenêtre commune ═══")
    btc_only = symbol_ensemble(dfs["BTC/USDT"], CAPITAL)
    btc_only = btc_only[btc_only.index >= common_start]
    combined = portfolio(dfs)
    combined = combined[combined.index >= common_start]
    m_btc = compute_metrics(btc_only, [], BPY)
    m_comb = compute_metrics(combined, [], BPY)

    r_btc = btc_only.pct_change().dropna()
    r_comb = combined.pct_change().dropna()
    diff, pval = block_bootstrap_sharpe_diff(r_comb, r_btc)

    print(
        f"  BTC seul        : Sharpe {m_btc['sharpe']:.2f}  CAGR {m_btc['cagr']:+.1%}  "
        f"maxDD {m_btc['max_drawdown']:+.1%}"
    )
    print(
        f"  BTC+ETH+SOL     : Sharpe {m_comb['sharpe']:.2f}  CAGR {m_comb['cagr']:+.1%}  "
        f"maxDD {m_comb['max_drawdown']:+.1%}"
    )
    print(f"  → ΔSharpe {diff:+.2f}  |  p-value (diversif. meilleure) = {pval:.3f}")

    # corrélation des rendements entre sleeves (diversification réelle ?)
    print("\n  Corrélations des rendements (ensemble par actif, fenêtre commune) :")
    sleeves = {s: symbol_ensemble(d, CAPITAL) for s, d in dfs.items()}
    rets = pd.concat({s: e.pct_change() for s, e in sleeves.items()}, axis=1)
    rets = rets[rets.index >= common_start].dropna()
    print(rets.corr().round(2).to_string())

    # ── #2 Funding réel vs constante plate (BTC, plein historique) ──────────
    print("\n═══ #2 Funding réel dans le backtest (BTC) ═══")
    btc = dfs["BTC/USDT"]
    eq_flat = symbol_ensemble(btc, CAPITAL)
    eq_real = symbol_ensemble(add_real_funding(btc, "BTC/USDT:USDT"), CAPITAL)
    m_flat, m_real = compute_metrics(eq_flat, [], BPY), compute_metrics(eq_real, [], BPY)
    d2, p2 = block_bootstrap_sharpe_diff(
        eq_real.pct_change().dropna(), eq_flat.pct_change().dropna()
    )
    print(f"  funding plat (0.01%/8h) : Sharpe {m_flat['sharpe']:.2f}  CAGR {m_flat['cagr']:+.1%}")
    print(f"  funding RÉEL            : Sharpe {m_real['sharpe']:.2f}  CAGR {m_real['cagr']:+.1%}")
    print(
        f"  → ΔSharpe {d2:+.2f} (le backtest plat {'sur' if d2 < 0 else 'sous'}-estimait le coût réel)"
    )

    # ── #3 Tilt net-long : sous-dimensionner les shorts (BTC) ───────────────
    print("\n═══ #3 Tilt net-long (multiplicateur de taille des shorts, BTC) ═══")
    base_ret = symbol_ensemble(btc, CAPITAL, short_mult=1.0).pct_change().dropna()
    for sm in (1.0, 0.5, 0.0):
        eq = symbol_ensemble(btc, CAPITAL, short_mult=sm)
        m = compute_metrics(eq, [], BPY)
        label = {1.0: "symétrique", 0.5: "tilt 50% shorts", 0.0: "long-only"}[sm]
        if sm == 1.0:
            print(
                f"  {label:16}: Sharpe {m['sharpe']:.2f}  CAGR {m['cagr']:+.1%}  maxDD {m['max_drawdown']:+.1%}  (réf.)"
            )
        else:
            d3, p3 = block_bootstrap_sharpe_diff(eq.pct_change().dropna(), base_ret)
            print(
                f"  {label:16}: Sharpe {m['sharpe']:.2f}  CAGR {m['cagr']:+.1%}  maxDD {m['max_drawdown']:+.1%}  "
                f"ΔSharpe {d3:+.2f}  p {p3:.3f}"
            )


if __name__ == "__main__":
    main()
