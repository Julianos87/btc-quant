"""Tests du journal des flux (apports/rééquilibrages) et de sa prise en
compte par le dashboard : un apport ne doit jamais ressembler à un gain."""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "dashboard"))

import app as dash  # dashboard/app.py
from btcquant.execution.state_store import StateStore
from btcquant.execution.readiness import ReadinessPolicy, start_campaign
from btcquant.entrypoints import digest, rebalance


def _load_rebalance():
    return rebalance


def _write_states(state: Path, trend_cash: float = 2000.0, carry_equity: float = 4000.0) -> None:
    state.mkdir(exist_ok=True)
    (state / "live_state_4x.json").write_text(
        json.dumps(
            {
                "slots": {
                    f"trend_ls_{n}": {"cash": trend_cash, "position": None} for n in (20, 55, 100)
                },
                "peak_equity": trend_cash * 3,
                "day_start_equity": trend_cash * 3,
                "halted": False,
            }
        )
    )
    (state / "carry_state.json").write_text(
        json.dumps({"equity": carry_equity, "in_position": False})
    )


def test_rebalance_deposit_logs_flow(tmp_path, monkeypatch, capsys):
    reb = _load_rebalance()
    monkeypatch.setattr(reb, "STATE", tmp_path)
    _write_states(tmp_path)  # 6000/4000 : déjà à la cible, pas de rééquilibrage

    monkeypatch.setattr(sys, "argv", ["rebalance.py", "--apply", "--deposit", "100"])
    reb.main()

    store = StateStore(tmp_path / "btcquant.db")
    trend = store.load_engine_state("trend")
    carry = store.load_engine_state("carry")
    assert trend is not None and carry is not None
    assert trend["slots"]["trend_ls_20"]["cash"] == pytest.approx(2020.0)
    assert trend["peak_equity"] == pytest.approx(6060.0)  # l'apport n'est pas un gain
    assert carry["equity"] == pytest.approx(4040.0)

    flows = pd.DataFrame(store.read_flows())
    assert len(flows) == 1  # apport seul : allocation à la cible, pas de transfert
    row = flows.iloc[0]
    assert row["kind"] == "deposit"
    assert row["trend_flow"] == pytest.approx(60.0)
    assert row["carry_flow"] == pytest.approx(40.0)


def test_rebalance_transfer_logs_zero_sum_flow(tmp_path, monkeypatch):
    reb = _load_rebalance()
    monkeypatch.setattr(reb, "STATE", tmp_path)
    _write_states(tmp_path, trend_cash=2400.0, carry_equity=2800.0)  # 72/28 : dérive > 3 %

    monkeypatch.setattr(sys, "argv", ["rebalance.py", "--apply"])
    reb.main()

    flows = pd.DataFrame(StateStore(tmp_path / "btcquant.db").read_flows())
    assert list(flows["kind"]) == ["rebalance"]
    assert flows["trend_flow"].iloc[0] + flows["carry_flow"].iloc[0] == pytest.approx(0.0)


# ── côté dashboard ───────────────────────────────────────────────────────────


def _write_equity(path: Path, values: dict[str, float]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("ts,equity\n")
        for ts, v in values.items():
            fh.write(f"{ts},{v:.2f}\n")


@pytest.fixture
def dash_state(tmp_path, monkeypatch):
    """État synthétique : 30 jours plats, apport de 40 $ (poche carry) au jour 15.
    Sans neutralisation, l'apport ressemblerait à +0,4 % de gain."""
    monkeypatch.setattr(dash, "STATE", tmp_path)
    days = pd.date_range("2026-06-01", periods=30, freq="1D", tz="UTC")
    deposit_ts = days[15] + pd.Timedelta(hours=4)
    _write_equity(tmp_path / "equity_trend.csv", {ts.isoformat(): 6000.0 for ts in days})
    _write_equity(
        tmp_path / "equity_carry.csv",
        {ts.isoformat(): (4000.0 if ts < deposit_ts else 4040.0) for ts in days},
    )
    (tmp_path / "flows.csv").write_text(
        f"ts,kind,trend_flow,carry_flow\n{deposit_ts.isoformat()},deposit,0.00,40.00\n"
    )
    (tmp_path / "carry_state.json").write_text(json.dumps({"equity": 4040.0, "in_position": False}))
    (tmp_path / "live_state_4x.json").write_text(
        json.dumps({"slots": {}, "peak_equity": 6000.0, "halted": False})
    )
    return tmp_path


def test_metrics_ignore_deposits(dash_state):
    m = dash.app.test_client().get("/api/metrics").get_json()
    # équity plate hors apport : aucun gain, aucun drawdown
    assert m["max_dd"] == pytest.approx(0.0)
    assert m["cur_dd"] == pytest.approx(0.0)
    assert m["cagr"] == pytest.approx(0.0, abs=1e-9)
    assert m["sharpe"] is None  # rendements constants : pas de ratio


def test_analytics_funding_excludes_deposits(dash_state):
    a = dash.app.test_client().get("/api/analytics").get_json()
    # carry 4000 → 4040 uniquement par apport : aucun funding gagné
    assert a["records"]["funding_total"] == pytest.approx(0.0)
    assert a["records"]["best_day"] == pytest.approx(0.0, abs=1e-9)


def test_readiness_drawdown_unaffected(dash_state):
    store = StateStore(dash_state / "btcquant.db")
    store.migrate_legacy_journals(dash_state)
    start_campaign(
        store,
        ReadinessPolicy(),
        started_at="2026-06-01T00:00:00+00:00",
    )
    r = dash.app.test_client().get("/api/readiness").get_json()
    dd = next(c for c in r["checks"] if c["key"] == "drawdown")
    assert dd["status"] == "ok"
    assert dd["value"] == "0.0%"


def test_summary_pnl_net_of_deposits(dash_state, monkeypatch):
    monkeypatch.setattr(dash, "_cached", lambda key, ttl, fn: None)  # pas de réseau
    s = dash.app.test_client().get("/api/summary").get_json()
    assert s["totals"]["deposits"] == pytest.approx(40.0)
    assert s["totals"]["pnl"] == pytest.approx(0.0)  # 10 040 d'équity − 10 000 − 40 d'apports
    assert s["totals"]["pnl_pct"] == pytest.approx(0.0)


def test_flows_same_timestamp_no_crash(dash_state):
    """Apport + rééquilibrage journalisés au même instant (même run de
    rebalance.py) : le reindex de la série nette ne doit pas lever sur des
    labels dupliqués — les flux d'un même timestamp sont agrégés."""
    ts = pd.Timestamp("2026-06-20T04:00:00", tz="UTC").isoformat()
    (dash_state / "flows.csv").write_text(
        f"ts,kind,trend_flow,carry_flow\n{ts},deposit,0.00,40.00\n{ts},rebalance,24.00,-24.00\n"
    )
    m = dash.app.test_client().get("/api/metrics")
    assert m.status_code == 200
    a = dash.app.test_client().get("/api/analytics")
    assert a.status_code == 200


def test_digest_tolerates_torn_last_line(tmp_path, monkeypatch):
    """Le digest lit les CSV pendant que les runners écrivent : une dernière
    ligne tronquée ne doit pas le faire planter (même garde que le dashboard)."""
    monkeypatch.setattr(digest, "STATE", tmp_path)
    (tmp_path / "equity_trend.csv").write_text(
        "ts,equity\n2026-06-01T00:00:00+00:00,6000.00\n2026-06-0"
    )  # ligne coupée
    (tmp_path / "flows.csv").write_text(
        "ts,kind,trend_flow,carry_flow\n2026-06-01T04:00"
    )  # ligne coupée
    s = digest._equity("trend", "equity_trend.csv")
    assert len(s) == 1 and float(s.iloc[0]) == 6000.0
    assert len(digest._flows()) == 0


def test_equity_endpoint_returns_combined(dash_state, monkeypatch):
    """/api/equity doit servir les trois séries sans réseau (le buy & hold
    vient du cache, vide en test) — régression : un refactor avait supprimé
    la variable `combined` et l'endpoint faisait 500."""
    monkeypatch.setattr(dash, "_cached", lambda key, ttl, fn: None)  # pas de réseau
    r = dash.app.test_client().get("/api/equity")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["combined"]) > 0
    assert data["buyhold"] == []


def test_combined_equity_raw_keeps_deposit(dash_state):
    """La courbe d'équity affichée (brute) garde l'apport ; la série nette non."""
    raw = dash._combined_equity()
    net = dash._combined_equity(net_of_flows=True)
    assert float(raw.iloc[-1]) == pytest.approx(10040.0)
    assert float(net.iloc[-1]) == pytest.approx(10000.0)
    assert float(net.max() - net.min()) == pytest.approx(0.0)
