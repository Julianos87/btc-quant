"""Rééquilibrage 60/40 entre les moteurs trend et carry (paper) + apports.

Ramène l'allocation à la cible en ajustant le cash des deux sous-comptes,
uniquement quand les deux moteurs sont à plat (on ne redimensionne jamais une
position existante). Appelé par un timer systemd mensuel, ou à la main.

--deposit N : apport de N $ réparti 60/40 (trend/carry). L'apport s'applique
TOUJOURS, même avec des positions ouvertes ou sous le seuil de dérive — seul
le rééquilibrage entre poches est soumis à ces conditions.

Tout flux de capital recale proportionnellement ``peak_equity`` et
``day_start_equity``. Le drawdown et la performance du jour restent ainsi
inchangés par un apport ou un transfert purement comptable.

Chaque mouvement appliqué (apport ou transfert entre poches) est journalisé
dans SQLite : le dashboard s'en sert pour calculer des métriques
hors apports (Sharpe, drawdown, PnL, funding cumulé) — sans ce journal, un
apport ressemblerait à un gain de trading.

En paper, "déplacer du cash" = valider une transaction SQLite. En live réel,
ce mouvement supposerait un transfert entre le compte futures (trend) et le
sous-compte carry — étape à recoder pour le live, volontairement non faite ici.

IMPORTANT : ne jamais lancer avec les runners actifs (ils écrasent l'état au
tick suivant) — toujours passer par le timer systemd et son wrapper privilégié.
"""

import argparse
import math
import os
import sys
from pathlib import Path
from typing import Any

from btcquant.config import load_config, portfolio_from_config
from btcquant.execution.state_store import StateStore
from btcquant.notify import notify

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()
STATE = ROOT / "state"
PORTFOLIO = portfolio_from_config(
    load_config(ROOT / "environments" / "paper" / "config.yaml")
)
TARGET_TREND = PORTFOLIO.trend_fraction


def _positive_equity(label: str, value: Any) -> float:
    if not isinstance(value, (int, float)):
        sys.exit(f"Équity {label} absente ou non numérique — abandon.")
    equity = float(value)
    if not math.isfinite(equity) or equity <= 0:
        sys.exit(f"Équity {label} invalide ({equity!r}) — abandon.")
    return equity


def _rescale_risk_baselines(state: dict[str, Any], before: float, after: float) -> None:
    """Neutralise un flux dans les ratios de drawdown et de perte du jour."""

    ratio = after / before
    for key in ("peak_equity", "day_start_equity"):
        value = state.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            state[key] = float(value) * ratio
        else:
            # État legacy incomplet : repartir d'une référence sûre plutôt que
            # laisser le runner utiliser une ancienne allocation.
            state[key] = after


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="applique (sinon simulation)")
    parser.add_argument(
        "--deposit",
        type=float,
        default=0.0,
        help="apport en $ réparti 60/40 (toujours appliqué, même positions ouvertes)",
    )
    args = parser.parse_args()
    if not math.isfinite(args.deposit) or args.deposit < 0:
        parser.error("--deposit doit être un nombre fini positif (pas de retrait)")

    trend_path = STATE / "live_state_4x.json"
    carry_path = STATE / "carry_state.json"
    store = StateStore(STATE / "btcquant.db")
    store.migrate_legacy_journals(STATE)
    store.migrate_legacy_json("trend", trend_path)
    store.migrate_legacy_json("carry", carry_path)
    trend = store.load_engine_state("trend")
    carry = store.load_engine_state("carry")
    if trend is None or carry is None:
        sys.exit("États trend/carry absents de SQLite — abandon.")

    slots = trend.get("slots", {})
    if not slots:
        sys.exit(f"État trend sans slot ({trend_path}) : rien à répartir — abandon.")
    trend_position_open = any(s.get("position") for s in slots.values())
    carry_position_open = bool(carry.get("in_position"))
    parts = []

    # ── 1. apport : réparti 60/40, toujours appliqué ─────────────────────
    trend_equity_before = _positive_equity(
        "trend",
        sum(s.get("cash", 0.0) for s in slots.values()),
    )
    carry_equity_before = _positive_equity("carry", carry.get("equity"))
    if args.deposit > 0:
        dep_trend = args.deposit * TARGET_TREND
        dep_carry = args.deposit - dep_trend
        per_slot = dep_trend / len(slots)
        for s in slots.values():
            s["cash"] = s.get("cash", 0.0) + per_slot
        carry["equity"] = carry_equity_before + dep_carry
        _rescale_risk_baselines(
            trend,
            trend_equity_before,
            trend_equity_before + dep_trend,
        )
        _rescale_risk_baselines(
            carry,
            carry_equity_before,
            carry_equity_before + dep_carry,
        )
        parts.append(
            f"Apport {args.deposit:,.0f} $ → trend +{dep_trend:,.0f} $, "
            f"carry +{dep_carry:,.0f} $."
        )

    # ── 2. rééquilibrage : uniquement à plat et au-delà du seuil ─────────
    trend_cash = _positive_equity(
        "trend",
        sum(s.get("cash", 0.0) for s in slots.values()),
    )
    carry_cash = _positive_equity("carry", carry.get("equity"))
    total = trend_cash + carry_cash

    cur_trend_frac = trend_cash / total
    target_trend_cash = total * TARGET_TREND
    drift = cur_trend_frac - TARGET_TREND
    parts.append(
        f"Allocation : trend {cur_trend_frac:.1%} ({trend_cash:,.0f} $) / "
        f"carry {1 - cur_trend_frac:.1%} ({carry_cash:,.0f} $), écart {drift:+.1%}."
    )

    do_rebalance = True
    open_engines = [
        name
        for name, is_open in (
            ("trend", trend_position_open),
            ("carry", carry_position_open),
        )
        if is_open
    ]
    if open_engines:
        parts.append(
            f"Position ouverte ({', '.join(open_engines)}) → rééquilibrage reporté "
            "(aucune exposition n'est redimensionnée)."
        )
        do_rebalance = False
    elif abs(drift) < 0.03:
        parts.append("Sous le seuil de 3 %, pas de mouvement entre poches.")
        do_rebalance = False

    if do_rebalance:
        per_slot = target_trend_cash / len(slots)
        for s in slots.values():
            s["cash"] = per_slot
        target_carry_cash = total - target_trend_cash
        carry["equity"] = target_carry_cash
        _rescale_risk_baselines(trend, trend_cash, target_trend_cash)
        _rescale_risk_baselines(carry, carry_cash, target_carry_cash)
        parts.append(
            f"Rééquilibré : trend → {target_trend_cash:,.0f} $, "
            f"carry → {target_carry_cash:,.0f} $."
        )

    if args.apply and (args.deposit > 0 or do_rebalance):
        flows = []
        if args.deposit > 0:
            dep_trend = args.deposit * TARGET_TREND
            flows.append(
                {
                    "kind": "deposit",
                    "trend_flow": dep_trend,
                    "carry_flow": args.deposit - dep_trend,
                }
            )
        if do_rebalance:
            transfer = target_trend_cash - trend_cash  # >0 : cash carry → trend
            flows.append(
                {
                    "kind": "rebalance",
                    "trend_flow": transfer,
                    "carry_flow": -transfer,
                }
            )
        store.save_states_and_flows({"trend": trend, "carry": carry}, flows)
        parts.append("✅ Appliqué.")
    elif not args.apply:
        parts.append("(simulation — relancer avec --apply pour appliquer)")

    msg = " ".join(parts)
    print(msg)
    notify(msg)


if __name__ == "__main__":
    main()
