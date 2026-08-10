"""Refuse les références dashboard dont les hashes ne correspondent plus."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.provenance import quantitative_source_sha256


def _portable_bytes(path: Path) -> bytes:
    """Normalise les fichiers texte suivis entre Windows et Linux."""

    return path.read_bytes().replace(b"\r\n", b"\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(_portable_bytes(path)).hexdigest()


def _check_file(path: Path, expected: str, label: str) -> None:
    if not path.exists() or _sha256(path) != expected:
        raise SystemExit(f"Référence périmée : hash {label} incorrect pour {path}")


#: Chiffres publiés en clair, et l'endroit exact où ils sont affirmés.
#
# Motif : le dépôt a longtemps annoncé TROIS Sharpe différents pour le même
# profil et deux drawdowns différents. Le Sharpe canonique est désormais
# journalier ; README, configuration et référence doivent rester alignés.
# (-49 % contre -52,9 %). Le contrat de risque que l'opérateur relit avant de
# tenir un creux était donc faux. Toute prose citant un résultat doit désormais
# être vérifiable ici, sous peine d'échec CI.
#
# (fichier, motif de capture, chemin dans la baseline, mise en forme)
PUBLISHED_FIGURES = [
    (
        "README.md",
        r"Référence reproductible du profil x4(?: adaptatif)? : \*\*Sharpe (\d+,\d+)\*\*",
        ("combined", "sharpe"),
        lambda value: f"{value:.2f}".replace(".", ","),
    ),
    (
        "README.md",
        r"\| Trades \| (\d+) \|",
        ("conformity", "n_trades"),
        lambda value: str(int(value)),
    ),
    (
        "README.md",
        r"\| Drawdown maximal historique \| (-[\d,]+) %",
        ("combined", "max_drawdown"),
        lambda value: f"{value * 100:.1f}".replace(".", ","),
    ),
    (
        "README.md",
        r"\| Plus longue série de pertes \| (\d+) trades",
        ("conformity", "worst_loss_streak"),
        lambda value: str(int(value)),
    ),
    (
        "environments/paper/config.yaml",
        r"Sharpe ([\d.]+) \|",
        ("combined", "sharpe"),
        lambda value: f"{value:.2f}",
    ),
    (
        "environments/paper/config.yaml",
        r"MAX DRAWDOWN (-[\d.]+) %",
        ("combined", "max_drawdown"),
        lambda value: f"{value * 100:.1f}",
    ),
    (
        "environments/paper/config.yaml",
        r"\+([\d.]+) %/an",
        ("combined", "cagr"),
        lambda value: f"{value * 100:.1f}",
    ),
]


def _check_published_figures(baseline: dict) -> None:
    results = baseline["results"]
    for filename, pattern, (section, key), render in PUBLISHED_FIGURES:
        text = (ROOT / filename).read_text(encoding="utf-8")
        found = re.search(pattern, text)
        if found is None:
            raise SystemExit(
                f"{filename} : la phrase citant {section}.{key} a changé de forme. "
                "Vérifier le chiffre, puis mettre à jour PUBLISHED_FIGURES."
            )
        expected = render(results[section][key])
        if found.group(1) != expected:
            raise SystemExit(
                f"{filename} : {section}.{key} publié à {found.group(1)!r}, "
                f"la référence dit {expected!r}."
            )


def _check_walkforward_reference(source_hash: str, *, verify_local_data: bool) -> None:
    path = ROOT / "audit" / "walkforward_trend_ls_reference.json"
    if not path.exists():
        raise SystemExit(f"Référence walk-forward absente : {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    provenance = payload["provenance"]
    if provenance["source_tree_sha256"] != source_hash:
        raise SystemExit(
            "Référence walk-forward périmée : relancer scripts/run_walkforward.py avec --output"
        )
    script = provenance["script"]
    _check_file(ROOT / script["path"], script["sha256"], "script walk-forward")
    config = provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config walk-forward")
    if verify_local_data:
        for item in provenance["data"]:
            _check_file(ROOT / item["path"], item["sha256"], "données walk-forward")
    methodology = payload.get("methodology", {})
    if "ensemble fixe déployé" not in methodology.get("does_not_validate", ""):
        raise SystemExit(
            "Référence walk-forward ambiguë : documenter explicitement "
            "qu'elle ne valide pas l'ensemble fixe déployé"
        )


def _check_multiasset_reference(*, verify_local_data: bool) -> None:
    path = ROOT / "audit" / "multiasset_reference.json"
    if not path.exists():
        raise SystemExit(f"Référence multi-actifs absente : {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    symbols = payload.get("method", {}).get("symbols", [])
    if symbols != ["BTC/USDT", "ETH/USDT", "SOL/USDT"]:
        raise SystemExit("Référence multi-actifs incomplète : BTC, ETH et SOL sont requis")
    provenance = payload["provenance"]
    script = ROOT / "scripts" / "multiasset_experiments.py"
    _check_file(script, provenance["script_sha256"], "script multi-actifs")
    if verify_local_data:
        for item in provenance["data"]:
            _check_file(ROOT / item["path"], item["sha256"], "données multi-actifs")


def _check_btc_return_research(*, verify_local_data: bool) -> None:
    path = ROOT / "audit" / "btc_return_research.json"
    if not path.exists():
        raise SystemExit(f"Recherche BTC orientée rendement absente : {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["protocol"].get("sealed_test_used_for_selection") is not False:
        raise SystemExit(
            "Recherche BTC invalide : le test scellé ne doit pas servir à la sélection"
        )
    provenance = payload["provenance"]
    script = ROOT / "scripts" / "research_btc_return.py"
    _check_file(script, provenance["script_sha256"], "script recherche BTC")
    config = provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config recherche BTC")
    if verify_local_data:
        for item in provenance["data"]:
            _check_file(ROOT / item["path"], item["sha256"], "données recherche BTC")


def _check_btc_cost_filter_research(*, verify_local_data: bool) -> None:
    path = ROOT / "audit" / "btc_cost_filter_research.json"
    if not path.exists():
        raise SystemExit(f"Recherche filtre de coûts absente : {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["protocol"].get("sealed_test_used_for_selection") is not False:
        raise SystemExit("Filtre de coûts invalide : le test scellé a servi à la sélection")
    provenance = payload["provenance"]
    script = ROOT / "scripts" / "research_btc_cost_filter.py"
    _check_file(script, provenance["script_sha256"], "script filtre de coûts")
    config = provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config filtre de coûts")
    if verify_local_data:
        for item in provenance["data"]:
            _check_file(ROOT / item["path"], item["sha256"], "données filtre de coûts")


def _check_carry_net_edge_research(*, verify_local_data: bool) -> None:
    path = ROOT / "audit" / "carry_net_edge_research.json"
    if not path.exists():
        raise SystemExit(f"Recherche carry nette absente : {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["protocol"].get("sealed_test_used_for_selection") is not False:
        raise SystemExit("Recherche carry nette invalide : test scelle utilise pour selection")
    provenance = payload["provenance"]
    _check_file(
        ROOT / "scripts" / "research_carry_net_edge.py",
        provenance["script_sha256"],
        "script carry nette",
    )
    if verify_local_data:
        data = provenance["data"]
        _check_file(ROOT / data["path"], data["sha256"], "donnees carry nette")
    real_inputs_complete = payload.get("adoption_checks", {}).get(
        "real_market_inputs_complete", False
    )
    if payload.get("adopted") and not real_inputs_complete:
        raise SystemExit("Recherche carry nette invalide : adoption sans emprunt et basis complets")


def _check_adaptive_regime_research(
    source_hash: str,
    *,
    verify_local_data: bool,
) -> None:
    path = ROOT / "audit" / "btc_adaptive_regime_research.json"
    if not path.exists():
        raise SystemExit(f"Recherche de regime adaptatif absente : {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["protocol"].get("sealed_test_used_for_selection") is not False:
        raise SystemExit("Recherche adaptative invalide : test scelle utilise pour selection")
    provenance = payload["provenance"]
    _check_file(
        ROOT / "scripts" / "research_btc_adaptive_regime.py",
        provenance["script_sha256"],
        "script regime adaptatif",
    )
    if provenance.get("source_tree_sha256") != source_hash:
        raise SystemExit("Recherche adaptative perimee : relancer le script de recherche")
    config = provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config regime adaptatif")
    if verify_local_data:
        for item in provenance["data"]:
            _check_file(ROOT / item["path"], item["sha256"], "donnees regime adaptatif")


def _check_horizon_contribution_research(
    source_hash: str,
    *,
    verify_local_data: bool,
) -> None:
    path = ROOT / "audit" / "btc_horizon_contribution_research.json"
    if not path.exists():
        raise SystemExit(f"Recherche de contribution Donchian absente : {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["protocol"].get("sealed_test_used_for_selection") is not False:
        raise SystemExit("Contribution Donchian invalide : test scellé utilisé pour sélection")
    provenance = payload["provenance"]
    _check_file(
        ROOT / "scripts" / "research_btc_horizon_contribution.py",
        provenance["script_sha256"],
        "script contribution Donchian",
    )
    if provenance.get("source_tree_sha256") != source_hash:
        raise SystemExit("Recherche Donchian périmée : relancer le script de recherche")
    config = provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config contribution Donchian")
    if verify_local_data:
        for item in provenance["data"]:
            _check_file(ROOT / item["path"], item["sha256"], "données contribution Donchian")


def _check_btc_improvement_research(
    source_hash: str,
    *,
    verify_local_data: bool,
) -> None:
    artifacts = {
        "btc_volatility_research.json": "research_btc_volatility.py",
        "btc_short_sizing_research.json": "research_btc_short_sizing.py",
        "btc_funding_sizing_research.json": "research_btc_funding_sizing.py",
        "btc_mean_reversion_research.json": "research_btc_mean_reversion.py",
        "btc_execution_costs_research.json": "research_btc_execution_costs.py",
        "btc_combined_research.json": "research_btc_combined.py",
    }
    for artifact_name, script_name in artifacts.items():
        path = ROOT / "audit" / artifact_name
        if not path.exists():
            raise SystemExit(f"Recherche BTC absente : {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        provenance = payload["provenance"]
        _check_file(
            ROOT / "scripts" / script_name,
            provenance["script_sha256"],
            f"script {artifact_name}",
        )
        config = provenance["config"]
        _check_file(ROOT / config["path"], config["sha256"], f"config {artifact_name}")
        if verify_local_data:
            for item in provenance["data"]:
                _check_file(ROOT / item["path"], item["sha256"], f"données {artifact_name}")
    combined = json.loads(
        (ROOT / "audit" / "btc_combined_research.json").read_text(encoding="utf-8")
    )
    if combined["provenance"].get("source_tree_sha256") != source_hash:
        raise SystemExit("Recherche BTC combinée périmée : relancer research_btc_combined.py")


def main() -> None:
    source_hashes = {
        "baseline": quantitative_source_sha256(ROOT / "scripts/make_baseline_snapshot.py"),
        "walkforward": quantitative_source_sha256(ROOT / "scripts/run_walkforward.py"),
        "adaptive": quantitative_source_sha256(ROOT / "scripts/research_btc_adaptive_regime.py"),
        "combined": quantitative_source_sha256(ROOT / "scripts/research_btc_combined.py"),
        "horizon": quantitative_source_sha256(
            ROOT / "scripts/research_btc_horizon_contribution.py"
        ),
        "yearly": quantitative_source_sha256(ROOT / "scripts/make_yearly_reference.py"),
    }
    verify_local_data = os.environ.get("BTCQUANT_VERIFY_REFERENCE_DATA", "1") != "0"
    baseline = json.loads((ROOT / "audit" / "baseline_reference.json").read_text(encoding="utf-8"))
    provenance = baseline["provenance"]
    if provenance["source_tree_sha256"] != source_hashes["baseline"]:
        raise SystemExit("Baseline périmée : relancer scripts/make_baseline_snapshot.py")
    config = provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config baseline")
    if verify_local_data:
        for item in provenance["data"]:
            _check_file(ROOT / item["path"], item["sha256"], "données baseline")
    if "conformity" not in baseline["results"]:
        raise SystemExit("Baseline incomplète : référence de conformité absente")
    _check_published_figures(baseline)
    _check_walkforward_reference(source_hashes["walkforward"], verify_local_data=verify_local_data)
    _check_multiasset_reference(verify_local_data=verify_local_data)
    _check_btc_return_research(verify_local_data=verify_local_data)
    _check_btc_cost_filter_research(verify_local_data=verify_local_data)
    _check_carry_net_edge_research(verify_local_data=verify_local_data)
    _check_adaptive_regime_research(
        source_hashes["adaptive"],
        verify_local_data=verify_local_data,
    )
    _check_horizon_contribution_research(
        source_hashes["horizon"],
        verify_local_data=verify_local_data,
    )
    _check_btc_improvement_research(
        source_hashes["combined"],
        verify_local_data=verify_local_data,
    )

    yearly = json.loads((ROOT / "dashboard" / "yearly_reference.json").read_text(encoding="utf-8"))
    yearly_provenance = yearly["provenance"]
    if yearly_provenance["source_tree_sha256"] != source_hashes["yearly"]:
        raise SystemExit("Référence annuelle périmée : relancer make_yearly_reference.py")
    config = yearly_provenance["config"]
    _check_file(ROOT / config["path"], config["sha256"], "config annuelle")
    if verify_local_data:
        base_data = ROOT / "data" / "binance_BTC-USDT_1h.csv"
        _check_file(base_data, yearly_provenance["base_data_sha256"], "données annuelles")


if __name__ == "__main__":
    main()
