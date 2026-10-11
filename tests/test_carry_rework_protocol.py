from __future__ import annotations

import json
from pathlib import Path

from btcquant.research.governance import ExperimentSpec
from btcquant.research.search_gates import validate_search_ready
from scripts.register_carry_rework_protocol import register


ROOT = Path(__file__).resolve().parents[1]


def test_preregistered_protocol_passes_search_gate_and_is_durable(tmp_path) -> None:
    spec_path = ROOT / "audit" / "carry_rework_protocol.json"
    spec = ExperimentSpec.from_dict(json.loads(spec_path.read_text(encoding="utf-8")))
    validate_search_ready(spec)
    candidates = spec.parameter_space["candidates"]
    assert len(candidates) == spec.maximum_trial_budget == 7
    assert spec.dataset_roles[spec.dataset_ids[0]].value == "SEEN_RESEARCH_DATA"

    result = register(spec_path, tmp_path / "research" / "governance.sqlite3")
    assert result["status"] == "REGISTERED_NO_TRIALS"
    assert result["selection_started"] is False
    assert Path(str(result["database"])).exists()
