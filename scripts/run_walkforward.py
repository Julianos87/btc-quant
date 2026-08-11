"""Validation walk-forward : optimisation glissante, mesure out-of-sample.

Un ratio d'efficacité (Sharpe OOS / Sharpe IS) > 0.5 indique des paramètres
robustes ; proche de 0 ou négatif = surapprentissage.

`trend_ls` — la stratégie RÉELLEMENT DÉPLOYÉE — n'avait aucune grille ici : seules
les deux stratégies archivées en avaient une. Elle n'avait donc jamais été
validée hors échantillon par cet outil. La défense « paramètres standards de la
littérature, non optimisés » reste valable contre le surapprentissage par
grid-search, mais elle ne dit rien de la stabilité des règles dans le temps :
c'est ce que mesure ce walk-forward.

Usage :
    python scripts/run_walkforward.py trend_ls --config environments/paper/config.yaml --no-refresh
    python scripts/run_walkforward.py trend_ls --symbol ETH/USDT --no-refresh
    python scripts/run_walkforward.py trend_ls --no-short --no-refresh
"""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output

enable_utf8_output()

from btcquant.backtest import BacktestEngine
from btcquant.backtest.metrics import format_metrics
from btcquant.config import execution_config_from_config, load_config, risk_from_config
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.indicators import bars_per_year
from btcquant.carry import add_funding_columns, resolve_funding
from btcquant.provenance import quantitative_source_sha256
from btcquant.research.strategies import RESEARCH_STRATEGY_REGISTRY
from btcquant.research.walkforward import walk_forward
from btcquant.strategies import STRATEGY_REGISTRY
from btcquant.data_integrity import GapPolicy, frame_provenance

log = logging.getLogger(__name__)

# Grilles volontairement restreintes : chaque paramètre supplémentaire
# augmente le risque de surapprentissage, pas la robustesse.
GRIDS = {
    "trend_ls": {
        # Les trois horizons de l'ensemble déployé, et l'ATR du stop. Les
        # filtres ADX et funding restent FIXES à leur valeur de production :
        # on mesure la stabilité des règles, pas la meilleure combinaison.
        "donchian": [20, 55, 100],
        "atr_mult": [2.5, 3.0, 3.5],
    },
    "trend_swing": {
        "ema_fast": [30, 50],
        "ema_slow": [150, 200],
        "donchian": [40, 55],
        "atr_mult": [2.5, 3.5],
    },
    "intraday_breakout": {
        "lookback_high": [24, 48],
        "atr_mult": [2.0, 3.0],
        "volume_mult": [1.0, 1.3],
    },
}
# train/test en barres, par timeframe de stratégie
WINDOWS = {
    "trend_ls": (4380, 1095),  # 4h : 2 ans / 6 mois
    "trend_swing": (4380, 1095),  # 4h : 2 ans / 6 mois
    "intraday_breakout": (17520, 4380),  # 1h : 2 ans / 6 mois
}
#: `trend_ls` appartient au registre runtime, les autres au registre recherche.
ALL_STRATEGIES = {**RESEARCH_STRATEGY_REGISTRY, **STRATEGY_REGISTRY}
#: Timeframe par défaut quand la config ne déclare pas la stratégie demandée.
DEFAULT_TIMEFRAMES = {"trend_ls": "4h", "trend_swing": "4h", "intraday_breakout": "1h"}


def _portable_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(_portable_bytes(path)).hexdigest()


def _jsonable(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _data_path(exchange: str, symbol: str, timeframe: str, data_dir: str) -> Path:
    safe_symbol = symbol.replace("/", "-").replace(":", "_")
    return ROOT / data_dir / f"{exchange}_{safe_symbol}_{timeframe}.csv"


def _write_reference(
    destination: Path,
    *,
    args,
    cfg: dict,
    symbol: str,
    timeframe: str,
    market: str,
    allow_short: bool,
    train_bars: int,
    test_bars: int,
    result,
    fixed_params: dict,
    base,
    funding_resolution,
) -> None:
    config_path = Path(args.config).resolve()
    data_files = [
        _data_path(
            cfg["exchange"],
            symbol,
            cfg["data"]["base_timeframe"],
            cfg["data"]["dir"],
        )
    ]
    if market == "perp" and symbol == cfg["symbol"]:
        funding_symbol = f"{symbol}:{cfg['quote_currency']}".replace("/", "").replace(":", "_")
        funding_path = ROOT / "data" / f"binanceusdm_{funding_symbol}_funding.csv"
        if (
            funding_resolution is not None
            and funding_resolution.series is not None
            and funding_path.exists()
        ):
            data_files.append(funding_path)

    oos_trades = sum(int(fold["oos_trades"] or 0) for fold in result.folds)
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Référence walk-forward de recherche; aucune promesse de performance",
        "provenance": {
            "base_git_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "source_tree_sha256": quantitative_source_sha256(Path(__file__)),
            "script": {
                "path": Path(__file__).resolve().relative_to(ROOT).as_posix(),
                "sha256": _sha256(Path(__file__)),
            },
            "config": {
                "path": config_path.relative_to(ROOT).as_posix(),
                "sha256": _sha256(config_path),
            },
            "data": [
                {
                    "path": path.relative_to(ROOT).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in data_files
            ],
            "ohlcv": frame_provenance(
                base,
                source="historical_cache",
                expected_frequency=cfg["data"]["base_timeframe"],
                path=data_files[0],
            ),
            "funding": (
                frame_provenance(
                    funding_resolution.series,
                    source=funding_resolution.source,
                    expected_frequency="8h",
                    path=data_files[-1] if len(data_files) > 1 else None,
                )
                if funding_resolution is not None and funding_resolution.series is not None
                else {
                    "source": funding_resolution.source
                    if funding_resolution is not None
                    else "not_applicable"
                }
            ),
            "config_hash": _sha256(config_path),
            "code_provenance": quantitative_source_sha256(Path(__file__)),
        },
        "experiment": {
            "strategy": args.strategy,
            "fixed_params": fixed_params,
            "symbol": symbol,
            "timeframe": timeframe,
            "market": market,
            "allow_short": allow_short,
            "grid": GRIDS[args.strategy],
            "train_bars": train_bars,
            "test_bars": test_bars,
            "objective": "sharpe",
        },
        "methodology": {
            "validates": (
                "stabilité hors échantillon d'une sélection glissante d'un horizon "
                "et d'un multiple ATR"
            ),
            "does_not_validate": (
                "parité ou performance hors échantillon de l'ensemble fixe déployé "
                "Donchian 20/55/100"
            ),
        },
        "results": {
            "folds": _jsonable(result.folds),
            "oos_metrics": _jsonable(result.oos_metrics),
            "oos_trades": oos_trades,
            "efficiency": _jsonable(result.efficiency),
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nRéférence walk-forward écrite : {destination}")


def _strategy_spec(cfg: dict, name: str) -> dict:
    """Retrouve la déclaration d'une CLASSE de stratégie dans la config.

    Les clés de `strategies:` sont des noms d'INSTANCE (`trend_ls_20`) ; la
    classe est portée par `type:`. Chercher la classe comme une clé — ce que
    faisait ce script — échouait donc sur toute stratégie de l'ensemble.
    """

    strategies = cfg.get("strategies", {})
    if name in strategies:
        return strategies[name]
    for spec in strategies.values():
        if isinstance(spec, dict) and spec.get("type") == name:
            return spec
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("strategy", choices=list(GRIDS))
    parser.add_argument(
        "--config",
        default=ROOT / "environments" / "paper" / "config.yaml",
    )
    parser.add_argument(
        "--symbol",
        help="valide les mêmes règles sur un autre actif (ex. ETH/USDT) — "
        "le cache local doit exister ; le funding réel n'est chargé que pour BTC",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="écrit un artefact JSON reproductible avec résultats et provenance",
    )
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument(
        "--funding-mode",
        choices=("real", "synthetic"),
        default="real",
        help="real exige le cache funding ; synthetic doit être demandé explicitement",
    )
    parser.add_argument(
        "--synthetic-funding-rate",
        type=float,
        help="taux constant 8 h utilisé uniquement en mode synthetic",
    )
    parser.add_argument(
        "--no-short",
        action="store_true",
        help="mesure les mêmes règles en long seulement — les signaux short sont "
        "ignorés au lieu d'ouvrir une position (comparaison long-only vs long-short)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    funding_mode = "SYNTHETIC_EXPLICIT" if args.funding_mode == "synthetic" else "REAL"
    synthetic_rate = (
        args.synthetic_funding_rate
        if args.synthetic_funding_rate is not None
        else cfg["costs"].get("funding_rate_8h", 0.0)
    )
    spec = _strategy_spec(cfg, args.strategy)
    timeframe = spec.get("timeframe", DEFAULT_TIMEFRAMES[args.strategy])
    market = spec.get("market", "spot")
    is_perp = market == "perp"
    symbol = args.symbol or cfg["symbol"]

    base = load_ohlcv(
        cfg["exchange"],
        symbol,
        cfg["data"]["base_timeframe"],
        cfg["data"]["since"],
        data_dir=ROOT / cfg["data"]["dir"],
        refresh=not args.no_refresh,
        gap_policy=GapPolicy.ALLOW_REPORTED,
    )
    df = (
        base
        if timeframe == cfg["data"]["base_timeframe"]
        else resample(
            base, TIMEFRAME_TO_PANDAS[timeframe], source_frequency=cfg["data"]["base_timeframe"]
        )
    )
    funding_resolution = None
    if is_perp:
        funding_resolution = resolve_funding(
            f"{symbol}:{cfg['quote_currency']}",
            data_dir=ROOT / "data",
            refresh=not args.no_refresh,
            mode=funding_mode,
            synthetic_rate=synthetic_rate,
        )
        if funding_resolution.series is not None:
            df = add_funding_columns(df, funding_resolution.series, TIMEFRAME_TO_PANDAS[timeframe])
        print(f"Funding {symbol}:{cfg['quote_currency']} : {funding_resolution.source}")

    fee_rate = cfg["costs"]["perp_fee_rate"] if is_perp else cfg["costs"]["fee_rate"]
    # Le spot ne shorte jamais ; `--no-short` retire en plus le côté vendeur du
    # perp, sans toucher aux règles d'entrée : c'est le même signal, non exécuté.
    allow_short = is_perp and not args.no_short
    engine = BacktestEngine(
        risk=risk_from_config(cfg),
        funding_rate_8h=funding_resolution.rate
        if is_perp and funding_resolution is not None and funding_resolution.rate is not None
        else 0.0,
        allow_short=allow_short,
        execution_simulator=ExecutionSimulator(execution_config_from_config(cfg, fee_rate)),
    )
    train_bars, test_bars = WINDOWS[args.strategy]
    grid = GRIDS[args.strategy]
    configured_params = dict(spec.get("params") or {})
    fixed_params = {key: value for key, value in configured_params.items() if key not in grid}
    print(
        f"Walk-forward {args.strategy} sur {symbol} ({timeframe}, {market}, "
        f"{'long-short' if allow_short else 'LONG SEULEMENT'}) : "
        f"train {train_bars} barres, test {test_bars} barres\n"
        f"Grille : {grid}\n"
        f"Paramètres fixes : {fixed_params}\n"
    )

    result = walk_forward(
        ALL_STRATEGIES[args.strategy],
        df,
        grid,
        engine,
        train_bars=train_bars,
        test_bars=test_bars,
        objective="sharpe",
        bars_per_year_value=bars_per_year(timeframe),
        fixed_params=fixed_params,
    )

    for fold in result.folds:
        print(
            f"Pli {fold['fold']}: test {fold['test'][0][:10]} → {fold['test'][1][:10]}  "
            f"params {fold['best_params']}  "
            f"OOS: ret {fold['oos_return']:+.1%}, sharpe {fold['oos_sharpe']:.2f}, "
            f"dd {fold['oos_max_dd']:.1%}, {fold['oos_trades']} trades"
        )

    print("\n═══ Out-of-sample agrégé ═══")
    # `compute_metrics` reçoit une liste de trades vide (la courbe OOS est
    # recousue, pas rejouée) : afficher « Trades : 0 » serait faux. On somme les
    # comptes réels des plis.
    oos_trades = sum(int(fold["oos_trades"] or 0) for fold in result.folds)
    print(format_metrics({**result.oos_metrics, "n_trades": oos_trades}))
    print(
        f"\nEfficacité walk-forward (Sharpe OOS / IS) : {result.efficiency:.2f}"
        f"  {'✔ robuste' if result.efficiency and result.efficiency > 0.5 else '⚠ prudence'}"
    )
    if args.output is not None:
        destination = args.output if args.output.is_absolute() else ROOT / args.output
        _write_reference(
            destination,
            args=args,
            cfg=cfg,
            symbol=symbol,
            timeframe=timeframe,
            market=market,
            allow_short=allow_short,
            train_bars=train_bars,
            test_bars=test_bars,
            result=result,
            fixed_params=fixed_params,
            base=base,
            funding_resolution=funding_resolution,
        )


if __name__ == "__main__":
    main()
