"""Rééquilibrage 60/40 entre les moteurs trend et carry (paper) + apports.

Ramène l'allocation à la cible en ajustant le cash des deux sous-comptes,
uniquement quand les deux moteurs sont à plat (on ne redimensionne jamais une
position existante). Appelé par un timer systemd mensuel, ou à la main.

--deposit N : apport de N $ réparti 60/40 (trend/carry). Si une position est
ouverte, l'apport est conservé en attente dans SQLite. Il est appliqué
automatiquement au prochain passage où les deux moteurs sont à plat.

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
from btcquant.backup import assert_writer_recovery_clear
from btcquant.execution.state_contract import validate_carry_state, validate_trend_state
from btcquant.execution.state_store import StateStore
from btcquant.notify import notify


def _runtime_root() -> Path:
    raw = os.environ.get("BTCQUANT_ROOT")
    if raw:
        return Path(raw).resolve()
    return Path.cwd().resolve()


def _paper_config_path() -> Path:
    """Paper YAML lives in the release tree, not under the runtime root."""

    candidates = (
        Path.cwd(),
        Path(sys.prefix).resolve().parent,
        _runtime_root(),
    )
    for root in candidates:
        config = root / "environments" / "paper" / "config.yaml"
        if config.is_file():
            return config
    return Path.cwd() / "environments" / "paper" / "config.yaml"


ROOT = _runtime_root()
STATE = ROOT / "state"
PORTFOLIO = portfolio_from_config(load_config(_paper_config_path()))
TARGET_TREND = PORTFOLIO.trend_fraction


def _positive_equity(label: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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
        if value is None:
            # Compatibilité avec les checkpoints antérieurs à l'introduction
            # des coupe-circuits. Une valeur présente mais invalide est refusée
            # par state_contract avant d'arriver ici.
            state[key] = after
        else:
            state[key] = float(value) * ratio


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="applique (sinon simulation)")
    parser.add_argument(
        "--deposit",
        type=float,
        default=0.0,
        help="apport en $ réparti 60/40 (mis en attente si une position est ouverte)",
    )
    parser.add_argument(
        "--deposit-id",
        help="identifiant unique et stable de l'apport, par exemple monthly:2026-08",
    )
    parser.add_argument(
        "--check-pending",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if not math.isfinite(args.deposit) or args.deposit < 0:
        parser.error("--deposit doit être un nombre fini positif (pas de retrait)")
    if args.deposit > 0 and not args.deposit_id:
        parser.error("--deposit-id est obligatoire pour empêcher les doubles apports")
    if args.check_pending and (args.apply or args.deposit > 0):
        parser.error("--check-pending ne peut pas appliquer de modification")

    trend_path = STATE / "live_state_4x.json"
    carry_path = STATE / "carry_state.json"
    assert_writer_recovery_clear(STATE)
    store = StateStore(STATE / "btcquant.db")
    if args.check_pending:
        raise SystemExit(0 if store.read_deposits(status="PENDING") else 3)
    store.migrate_legacy_journals(STATE)
    store.migrate_legacy_json("trend", trend_path)
    store.migrate_legacy_json("carry", carry_path)
    trend_raw = store.load_engine_state("trend")
    carry_raw = store.load_engine_state("carry")
    if trend_raw is None or carry_raw is None:
        sys.exit("États trend/carry absents de SQLite — abandon.")
    try:
        validate_trend_state(trend_raw)
        validate_carry_state(carry_raw)
    except ValueError as error:
        raise SystemExit(f"{error} — abandon.") from error
    trend = trend_raw
    carry = carry_raw

    slots = trend.get("slots", {})
    if not slots:
        sys.exit(f"État trend sans slot ({trend_path}) : rien à répartir — abandon.")
    trend_position_open = any(s.get("position") for s in slots.values())
    carry_position_open = bool(carry.get("in_position"))
    open_engines = [
        name
        for name, is_open in (
            ("trend", trend_position_open),
            ("carry", carry_position_open),
        )
        if is_open
    ]
    parts = []

    requested_deposit = None
    requested_created = False
    if args.deposit > 0:
        try:
            if args.apply:
                requested_deposit, requested_created = store.register_deposit(
                    args.deposit_id,
                    args.deposit,
                )
            else:
                existing = next(
                    (
                        item
                        for item in store.read_deposits()
                        if item["deposit_id"] == args.deposit_id
                    ),
                    None,
                )
                if existing is not None and not math.isclose(
                    float(existing["amount"]),
                    args.deposit,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                ):
                    parser.error(f"l'apport {args.deposit_id!r} existe déjà avec un autre montant")
                requested_deposit = existing or {
                    "deposit_id": args.deposit_id,
                    "amount": args.deposit,
                    "status": "PENDING",
                }
                requested_created = existing is None
        except ValueError as error:
            parser.error(str(error))

        if requested_created:
            action = "enregistré" if args.apply else "serait enregistré"
            parts.append(
                f"Apport {args.deposit:,.0f} $ ({args.deposit_id}) {action} une seule fois."
            )
        elif requested_deposit["status"] == "APPLIED":
            parts.append(f"Apport {args.deposit_id} déjà appliqué → doublon ignoré.")
        else:
            parts.append(f"Apport {args.deposit_id} déjà en attente → doublon ignoré.")

    pending_deposits = store.read_deposits(status="PENDING")
    if not args.apply and requested_created and requested_deposit is not None:
        pending_deposits = [*pending_deposits, requested_deposit]
    pending_total = sum(float(item["amount"]) for item in pending_deposits)
    deposit_to_apply = 0.0 if open_engines else pending_total

    # ── 1. apports : appliqués uniquement lorsque les deux moteurs sont à plat ──
    trend_equity_before = _positive_equity(
        "trend",
        sum(s.get("cash", 0.0) for s in slots.values()),
    )
    carry_equity_before = _positive_equity("carry", carry.get("equity"))
    if open_engines and pending_total > 0:
        parts.append(
            f"Total des apports en attente : {pending_total:,.0f} $ "
            f"({len(pending_deposits)} demande(s))."
        )
    elif deposit_to_apply > 0:
        dep_trend = deposit_to_apply * TARGET_TREND
        dep_carry = deposit_to_apply - dep_trend
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
            f"Apport {deposit_to_apply:,.0f} $ → trend +{dep_trend:,.0f} $, "
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
            f"Rééquilibré : trend → {target_trend_cash:,.0f} $, carry → {target_carry_cash:,.0f} $."
        )

    if args.apply and (deposit_to_apply > 0 or do_rebalance):
        flows = []
        if deposit_to_apply > 0:
            dep_trend = deposit_to_apply * TARGET_TREND
            flows.append(
                {
                    "kind": "deposit",
                    "trend_flow": dep_trend,
                    "carry_flow": deposit_to_apply - dep_trend,
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
        store.save_states_and_flows(
            {"trend": trend, "carry": carry},
            flows,
            applied_deposit_ids=[item["deposit_id"] for item in pending_deposits],
        )
        parts.append("✅ Appliqué.")
    elif not args.apply:
        parts.append("(simulation — relancer avec --apply pour appliquer)")

    msg = " ".join(parts)
    print(msg)
    notify(msg)


if __name__ == "__main__":
    main()
