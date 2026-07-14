"""Rééquilibrage 60/40 entre les moteurs trend et carry (paper) + apports.

Ramène l'allocation à la cible en ajustant le cash des deux sous-comptes,
sans toucher aux positions ouvertes du trend (on ne déplace que du cash
disponible, prudent). Appelé par un timer systemd mensuel, ou à la main.

--deposit N : apport de N $ réparti 60/40 (trend/carry). L'apport s'applique
TOUJOURS, même avec des positions ouvertes ou sous le seuil de dérive — seul
le rééquilibrage entre poches est soumis à ces conditions. En paper, le
capital de départ affiché par le dashboard reste 10 000 $ + apports non
suivis : l'important est l'équity, la performance en % se lit sur le backtest.

En paper, "déplacer du cash" = réécrire les fichiers d'état. En live réel,
ce mouvement supposerait un transfert entre le compte futures (trend) et le
sous-compte carry — étape à recoder pour le live, volontairement non faite ici.

IMPORTANT : ne jamais lancer avec les runners actifs (ils écrasent l'état au
tick suivant) — toujours passer par scripts/rebalance_safe.sh.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.notify import notify

STATE = ROOT / "state"
TARGET_TREND = 0.60


def _write_atomic(path: Path, payload: dict) -> None:
    """Écriture atomique (tmp + os.replace) — même invariant que les runners."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="applique (sinon simulation)")
    parser.add_argument("--deposit", type=float, default=0.0,
                        help="apport en $ réparti 60/40 (toujours appliqué, même positions ouvertes)")
    args = parser.parse_args()
    if args.deposit < 0:
        parser.error("--deposit doit être positif (pas de retrait par ce script)")

    trend_path = STATE / "live_state_4x.json"
    carry_path = STATE / "carry_state.json"
    trend = json.loads(trend_path.read_text())
    carry = json.loads(carry_path.read_text())

    slots = trend.get("slots", {})
    open_positions = any(s.get("position") for s in slots.values())
    parts = []

    # ── 1. apport : réparti 60/40, toujours appliqué ─────────────────────
    if args.deposit > 0:
        dep_trend = args.deposit * TARGET_TREND
        per_slot = dep_trend / len(slots)
        for s in slots.values():
            s["cash"] = s.get("cash", 0.0) + per_slot
        carry["equity"] = carry.get("equity", 0.0) + args.deposit * (1 - TARGET_TREND)
        # le peak_equity doit suivre l'apport, sinon le kill-switch drawdown
        # se relâche artificiellement (l'apport n'est pas un gain)
        if "peak_equity" in trend:
            trend["peak_equity"] = trend["peak_equity"] + dep_trend
        if "day_start_equity" in trend:
            trend["day_start_equity"] = trend["day_start_equity"] + dep_trend
        parts.append(f"Apport {args.deposit:,.0f} $ → trend +{dep_trend:,.0f} $, "
                     f"carry +{args.deposit - dep_trend:,.0f} $.")

    # ── 2. rééquilibrage : uniquement à plat et au-delà du seuil ─────────
    trend_cash = sum(s.get("cash", 0.0) for s in slots.values())
    carry_cash = carry.get("equity", 0.0)
    total = trend_cash + carry_cash
    if total <= 0:
        print("Rien à rééquilibrer.")
        return

    cur_trend_frac = trend_cash / total
    target_trend_cash = total * TARGET_TREND
    drift = cur_trend_frac - TARGET_TREND
    parts.append(f"Allocation : trend {cur_trend_frac:.1%} ({trend_cash:,.0f} $) / "
                 f"carry {1-cur_trend_frac:.1%} ({carry_cash:,.0f} $), écart {drift:+.1%}.")

    do_rebalance = True
    if open_positions:
        parts.append("Positions trend ouvertes → rééquilibrage reporté (on ne déplace pas de position).")
        do_rebalance = False
    elif abs(drift) < 0.03:
        parts.append("Sous le seuil de 3 %, pas de mouvement entre poches.")
        do_rebalance = False

    if do_rebalance:
        per_slot = target_trend_cash / len(slots)
        for s in slots.values():
            s["cash"] = per_slot
        carry["equity"] = total - target_trend_cash
        parts.append(f"Rééquilibré : trend → {target_trend_cash:,.0f} $, "
                     f"carry → {total - target_trend_cash:,.0f} $.")

    if args.apply and (args.deposit > 0 or do_rebalance):
        _write_atomic(trend_path, trend)
        _write_atomic(carry_path, carry)
        parts.append("✅ Appliqué.")
    elif not args.apply:
        parts.append("(simulation — relancer avec --apply pour appliquer)")

    msg = " ".join(parts)
    print(msg)
    notify(msg)


if __name__ == "__main__":
    main()
