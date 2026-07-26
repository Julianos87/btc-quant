"""Backtest du module cash-and-carry (funding) + sensibilité aux paramètres.

Usage : python scripts/run_carry_backtest.py [--leverage 3] [--no-refresh]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import backtest_carry, load_funding


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leverage", type=float, default=3.0)
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()

    funding = load_funding(data_dir=ROOT / "data", refresh=not args.no_refresh)
    print(
        f"Funding : {len(funding)} paiements, {funding.index[0].date()} -> {funding.index[-1].date()}\n"
    )

    print(
        f"{'entrée>':>8} {'sortie<':>8} {'lissage':>8} | {'CAGR':>7} {'Sharpe':>7} {'max DD':>7} "
        f"{'exposition':>11} {'cycles':>7}"
    )
    for enter in (0.03, 0.05, 0.08):
        for exit_ in (0.0, 0.02):
            for smooth in (3, 7, 14):
                r = backtest_carry(
                    funding,
                    leverage=args.leverage,
                    enter_ann=enter,
                    exit_ann=exit_,
                    smooth_days=smooth,
                )
                print(
                    f"{enter:>7.0%} {exit_:>8.0%} {smooth:>6}j  | {r['cagr']:>+7.1%} "
                    f"{r['sharpe']:>7.2f} {r['max_drawdown']:>7.1%} {r['exposure']:>11.0%} "
                    f"{r['cycles']:>7}"
                )
    print("\nRéférence sans règles (toujours investi) :")
    r = backtest_carry(funding, leverage=args.leverage, enter_ann=-9e9, exit_ann=-9e9)
    print(f"  CAGR {r['cagr']:+.1%} | Sharpe {r['sharpe']:.2f} | max DD {r['max_drawdown']:.1%}")


if __name__ == "__main__":
    main()
