"""Rééquilibrage 60/40 entre les moteurs trend et carry (paper).

Ramène l'allocation à la cible en ajustant le cash des deux sous-comptes,
sans toucher aux positions ouvertes du trend (on ne déplace que du cash
disponible, prudent). Appelé par un timer systemd mensuel, ou à la main.

En paper, "déplacer du cash" = réécrire les fichiers d'état. En live réel,
ce mouvement supposerait un transfert entre le compte futures (trend) et le
sous-compte carry — étape à recoder pour le live, volontairement non faite ici.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.notify import notify

STATE = ROOT / "state"
TARGET_TREND = 0.60


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="applique (sinon simulation)")
    args = parser.parse_args()

    trend_path = STATE / "live_state_4x.json"
    carry_path = STATE / "carry_state.json"
    trend = json.loads(trend_path.read_text())
    carry = json.loads(carry_path.read_text())

    slots = trend.get("slots", {})
    open_positions = any(s.get("position") for s in slots.values())
    trend_cash = sum(s.get("cash", 0.0) for s in slots.values())
    # valeur des positions ouvertes non déplaçable ; on rééquilibre sur le cash total
    carry_cash = carry.get("equity", 0.0)
    total = trend_cash + carry_cash
    if total <= 0:
        print("Rien à rééquilibrer.")
        return

    cur_trend_frac = trend_cash / total
    target_trend_cash = total * TARGET_TREND
    drift = cur_trend_frac - TARGET_TREND

    msg = (f"Rééquilibrage 60/40 : trend {cur_trend_frac:.1%} "
           f"({trend_cash:,.0f} $) / carry {1-cur_trend_frac:.1%} ({carry_cash:,.0f} $). "
           f"Écart à la cible : {drift:+.1%}.")
    if open_positions:
        msg += " Positions trend ouvertes → rééquilibrage reporté (on ne déplace pas de position)."
        print(msg); notify(msg)
        return
    if abs(drift) < 0.03:
        msg += " Sous le seuil de 3 %, aucun mouvement."
        print(msg); notify(msg)
        return

    if args.apply:
        # répartit le cash cible équitablement sur les 3 sous-systèmes du trend
        per_slot = target_trend_cash / len(slots)
        for s in slots.values():
            s["cash"] = per_slot
        carry["equity"] = total - target_trend_cash
        trend_path.write_text(json.dumps(trend, indent=2))
        carry_path.write_text(json.dumps(carry, indent=2))
        msg += f" ✅ Appliqué : trend → {target_trend_cash:,.0f} $, carry → {total-target_trend_cash:,.0f} $."
    else:
        msg += " (simulation — relancer avec --apply pour appliquer)"
    print(msg); notify(msg)


if __name__ == "__main__":
    main()
