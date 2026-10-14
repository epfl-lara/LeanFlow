"""Keep new prepared translation allocations separate from immutable older generations."""

from pathlib import Path

import pytest

from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.lean_layout import generation_layout
from leanflow_cli.native.harness_formalization_stages import campaign_budget_limits


def test_preparation_generation_does_not_own_or_mutate_previous_allocation():
    layout = {
        "accepted": True,
        "src_dir": "src",
        "module_prefix": "Main.Formalization.Document_123",
        "owned_prefix": "src/Main/Formalization/Document_123",
        "aggregator": "src/Main/Formalization/Document_123.lean",
    }
    first = generation_layout(layout, {"workflow_protocol": 2, "preparation_id": "first"})
    second = generation_layout(layout, {"workflow_protocol": 2, "preparation_id": "second"})
    assert first["module_prefix"] == layout["module_prefix"] + ".Run_" + identity("first")[:12]
    assert first["owned_prefix"] != second["owned_prefix"]
    assert not second["owned_prefix"].startswith(first["owned_prefix"] + "/")
    assert first["aggregator"] == first["owned_prefix"] + ".lean"
    assert second["build_targets"] == ["+" + second["module_prefix"] + ":olean"]
    assert layout["module_prefix"] == "Main.Formalization.Document_123"
    assert generation_layout(layout, {}) == layout
    with pytest.raises(ValueError, match="preparation identity"):
        generation_layout(layout, {"workflow_protocol": 2})


def test_shared_campaign_preserves_default_limits_and_rejects_native_override(tmp_path: Path):
    path = tmp_path / "budget.json"
    budget = CampaignLimits(path, {"agent_calls": 250, "tokens": None, "wall_time_s": 86400})
    budget.persist()
    environment = {"LEANFLOW_FORMALIZATION_BUDGET_PATH": str(path)}
    resumed = CampaignLimits(path, campaign_budget_limits(path, environment))
    assert resumed.limits == budget.limits
    environment["LEANFLOW_PROVER_AGENT_CALLS"] = "20"
    with pytest.raises(ValueError, match="Start a new campaign"):
        CampaignLimits(path, campaign_budget_limits(path, environment))
