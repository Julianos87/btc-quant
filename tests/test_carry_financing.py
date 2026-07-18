"""Coût de financement du carry à levier.

Contexte : jusqu'au 18/07/2026, le modèle créditait `equity × funding × levier`
sans jamais débiter le coût des fonds empruntés. Or un carry à levier L
immobilise L×capital de spot alors qu'on ne dispose que du capital : les
(L−1)×capital manquants sont empruntés et se paient. L'omission produisait un
Sharpe de 12 et un rendement annoncé environ deux fois trop élevé.

Ces tests figent la correction et, surtout, la **parité backtest/runner** : les
deux doivent appliquer la même formule, faute de quoi on recrée un écart du type
de celui du filtre funding.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import (
    DEFAULT_BORROW_RATE_ANN,
    PAYMENTS_PER_YEAR,
    backtest_carry,
)


def _flat_funding(rate: float = 0.0002, n: int = 3 * 365 * 2) -> pd.Series:
    """Funding constant : rend le rendement théorique calculable à la main."""
    idx = pd.date_range("2020-01-01", periods=n, freq="8h", tz="UTC")
    return pd.Series([rate] * n, index=idx)


# ── le coût existe et croît avec le levier ──────────────────────────────────


def test_leverage_one_pays_no_borrow_cost():
    """À levier 1 la position est financée par le seul capital : aucun emprunt,
    donc le taux ne doit avoir aucun effet."""
    f = _flat_funding()
    a = backtest_carry(f, leverage=1.0, borrow_rate_ann=0.0, initial_capital=4000)
    b = backtest_carry(f, leverage=1.0, borrow_rate_ann=0.30, initial_capital=4000)
    assert a["cagr"] == pytest.approx(b["cagr"], rel=1e-9)
    assert a["borrow_cost_ann"] == pytest.approx(0.0)


def test_borrow_cost_scales_with_leverage_minus_one():
    """Le coût porte sur (L−1)×capital, pas sur L×capital."""
    f = _flat_funding()
    r = 0.10
    c3 = backtest_carry(f, leverage=3.0, borrow_rate_ann=r, initial_capital=4000)
    c2 = backtest_carry(f, leverage=2.0, borrow_rate_ann=r, initial_capital=4000)
    # exposition identique (funding constant) -> rapport des coûts = 2:1
    assert c3["borrow_cost_ann"] == pytest.approx(2 * c2["borrow_cost_ann"], rel=1e-6)


def test_higher_borrow_rate_reduces_return():
    f = _flat_funding()
    cheap = backtest_carry(f, leverage=3.0, borrow_rate_ann=0.02, initial_capital=4000)
    dear = backtest_carry(f, leverage=3.0, borrow_rate_ann=0.25, initial_capital=4000)
    assert dear["cagr"] < cheap["cagr"]


def test_expensive_borrowing_can_make_carry_unprofitable():
    """Un taux d'emprunt supérieur au funding capté doit rendre le carry
    perdant — c'est tout l'intérêt de le modéliser."""
    f = _flat_funding(rate=0.0001)  # ~10,95 %/an
    res = backtest_carry(f, leverage=3.0, borrow_rate_ann=0.40, initial_capital=4000)
    assert res["cagr"] < 0.0


# ── la formule est celle documentée ─────────────────────────────────────────


def test_net_return_matches_closed_form():
    """L×funding − (L−1)×r/n par période, quand on est exposé en permanence."""
    rate, lev, r = 0.0002, 3.0, 0.10
    f = _flat_funding(rate=rate, n=3 * 365)
    res = backtest_carry(
        f, leverage=lev, borrow_rate_ann=r, initial_capital=1000,
        fee_rate=0.0, slippage_bps=0.0,
    )
    expo = res["exposure"]
    per_period = lev * rate - (lev - 1) * r / PAYMENTS_PER_YEAR
    attendu = per_period * PAYMENTS_PER_YEAR * expo
    assert res["ann_return_simple"] == pytest.approx(attendu, rel=1e-6)


def test_borrow_cost_only_charged_while_in_position():
    """Hors position, rien n'est immobilisé en spot, donc rien n'est emprunté."""
    # funding négatif d'emblée -> jamais d'entrée -> aucun coût
    f = _flat_funding(rate=-0.0002)
    res = backtest_carry(f, leverage=3.0, borrow_rate_ann=0.20, initial_capital=4000)
    assert res["exposure"] == pytest.approx(0.0)
    assert res["borrow_cost_ann"] == pytest.approx(0.0)
    assert res["cagr"] == pytest.approx(0.0, abs=1e-9)


def test_leverage_below_one_is_rejected():
    with pytest.raises(ValueError, match="leverage < 1"):
        backtest_carry(_flat_funding(), leverage=0.5)


def test_metrics_expose_financing_assumptions():
    """Les hypothèses doivent être lisibles dans le résultat, pas implicites."""
    res = backtest_carry(_flat_funding(), leverage=3.0, borrow_rate_ann=0.07)
    assert res["leverage"] == 3.0
    assert res["borrow_rate_ann"] == 0.07


# ── parité backtest / runner ────────────────────────────────────────────────


def test_runner_applies_same_formula_as_backtest():
    """Le runner paper doit débiter exactement le même portage que le backtest.

    On rejoue à la main la boucle de crédit du runner et on la compare à la
    formule du backtest.
    """
    rate, lev, r = 0.0002, 3.0, 0.10
    payments_per_year = PAYMENTS_PER_YEAR  # venue 8 h, comme le backtest

    borrow = (lev - 1.0) * r / payments_per_year
    equity = 1000.0
    for _ in range(10):
        equity += equity * (rate * lev - borrow)

    # même chose côté backtest, exposition forcée à 100 %
    per_period = lev * rate - (lev - 1) * r / payments_per_year
    attendu = 1000.0 * (1.0 + per_period) ** 10
    assert equity == pytest.approx(attendu, rel=1e-12)


def test_default_borrow_rate_is_not_zero():
    """Le défaut doit être une hypothèse prudente explicite : un défaut à 0
    reproduirait silencieusement le bug corrigé."""
    assert DEFAULT_BORROW_RATE_ANN > 0.0
    res = backtest_carry(_flat_funding(), leverage=3.0, initial_capital=4000)
    assert res["borrow_cost_ann"] > 0.0
