"""Phase 5 (3/6) tests: planner fan-out, synthesis, merge, stub seeding.

Everything model-shaped is faked: delegate_task returns canned lane
results, run_model_verification_review returns a canned synthesis, and
place_helpers is stubbed where file mechanics are not the point. The
assertions pin the N1 contract (no lane ever lost), the kernel-truth
merge path (apply_delta only), and the guarded stub door.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state, planner_phase
from leanflow_cli.workflows.orchestrator import OrchestratorRoute


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLANNER_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def _delegate_payload(*summaries: str, statuses: tuple[str, ...] = ()) -> str:
    results = []
    for index, summary in enumerate(summaries):
        status = statuses[index] if index < len(statuses) else "completed"
        results.append({"task_index": index, "status": status, "summary": summary, "api_calls": 1})
    return json.dumps({"results": results, "total_duration_seconds": 1.0})


_SYNTHESIS = json.dumps(
    {
        "grounding": ["the bound follows from AM-GM"],
        "strategy": ["state the helper", "close the goal"],
        "nodes": [
            {
                "name": "demo_helper",
                "file": "Demo.lean",
                "statement": "lemma demo_helper : True := by sorry",
                "split_of": "demo",
            },
            {"name": "demo", "file": "Demo.lean", "statement": "theorem demo : True"},
            {"name": "vague_idea", "file": "Demo.lean"},
        ],
    }
)


def _fake_synth(monkeypatch, response: str = _SYNTHESIS, status: str = "ok"):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(response=response, status=status)

    monkeypatch.setattr(planner_phase, "run_model_verification_review", fake)
    return calls


def _fake_delegate(monkeypatch, payload: str):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(planner_phase, "delegate_task", fake)
    return calls


def _fake_place(monkeypatch, *, ok: bool = True):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        names = [planner_phase.decomposer._helper_name(s) or "?" for s in kwargs["skeletons"]]
        return planner_phase.decomposer.DecomposeOutcome(
            ok=ok, reason="" if ok else "rejected", placed=tuple(names) if ok else ()
        )

    monkeypatch.setattr(planner_phase.decomposer, "place_helpers", fake)
    return calls


# ---------------------------------------------------------------------------
# Flags + guards
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("LEANFLOW_PLANNER_ENABLED", raising=False)
    assert planner_phase.planner_enabled() is False
    monkeypatch.setenv("LEANFLOW_PLANNER_ENABLED", "1")
    assert planner_phase.planner_enabled() is True


def test_max_subagents_clamped(monkeypatch):
    monkeypatch.setenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "9")
    assert planner_phase.planner_max_subagents() == 3
    monkeypatch.setenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "0")
    assert planner_phase.planner_max_subagents() == 1
    monkeypatch.setenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "junk")
    assert planner_phase.planner_max_subagents() == 3


def test_requires_goal_and_agent(enabled, monkeypatch):
    assert "no goal" in planner_phase.run_planner_phase(goal="", agent=object()).reason
    assert "no parent agent" in planner_phase.run_planner_phase(goal="g", agent=None).reason


# ---------------------------------------------------------------------------
# Lane fan-out: N1 — no lane is ever lost
# ---------------------------------------------------------------------------


def test_happy_path_merges_graph_and_prose(enabled, monkeypatch):
    delegate_calls = _fake_delegate(
        monkeypatch,
        _delegate_payload(
            '{"findings": [{"claim": "known", "source": "arxiv"}]}',
            '```json\n{"candidates": [{"name": "Nat.le_succ"}]}\n```',
            '{"hypothesis": "h", "result": "supports"}',
        ),
    )
    _fake_synth(monkeypatch)
    place_calls = _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="prove demo", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok
    assert [lane["status"] for lane in outcome.lanes] == ["completed"] * 3
    assert outcome.nodes_added == 3
    assert outcome.stubs_placed == ("demo_helper",)
    assert outcome.grounding_count == 1 and outcome.strategy_count == 2

    # Fan-out contract: one batch, isolated budget, bounded iterations.
    call = delegate_calls[0]
    assert len(call["tasks"]) == 3
    assert call["isolate_budget"] is True
    assert call["max_iterations"] == planner_phase.LANE_MAX_ITERATIONS
    assert call["empirical_task_indexes"] == frozenset({2})
    assert call["task_iteration_limits"] == {2: planner_phase.EMPIRICAL_LANE_MAX_ITERATIONS}
    assert all(
        task["_wall_timeout_s"] == planner_phase.PLANNER_LANE_WALL_TIMEOUT_S
        for task in call["tasks"]
    )

    # Only the shape-valid target-file stub went through the guarded door
    # (the non-stub 'theorem demo : True' and the statement-less node did not).
    assert [len(c["skeletons"]) for c in place_calls] == [1]

    # Graph merged through apply_delta: derived statuses, planner provenance.
    bp = plan_state.load_blueprint()
    helper = bp.node_by_id(plan_state.node_id_for("demo_helper", "Demo.lean"))
    assert helper.status == "stated" and helper.generated_by == "planner"
    vague = bp.node_by_id(plan_state.node_id_for("vague_idea", "Demo.lean"))
    assert vague.status == "conjectured"

    summary = plan_state.load_summary()
    assert summary["grounding_findings"] == ["the bound follows from AM-GM"]
    assert "## Strategy" in plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")


def test_planner_synthesis_uses_pinned_timeout(enabled, monkeypatch):
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    synth_calls = _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert outcome.ok
    assert synth_calls[0]["task"] == planner_phase.PLANNER_SYNTHESIS_TASK
    assert synth_calls[0]["timeout_s"] == planner_phase.PLANNER_SYNTHESIS_TIMEOUT_S == 300


def test_planner_synthesis_timeout_is_configurable_and_bounded(enabled, monkeypatch):
    monkeypatch.setenv("LEANFLOW_PLANNER_SYNTHESIS_TIMEOUT_S", "480")
    assert planner_phase.planner_synthesis_timeout_s() == 480
    monkeypatch.setenv("LEANFLOW_PLANNER_SYNTHESIS_TIMEOUT_S", "5000")
    assert planner_phase.planner_synthesis_timeout_s() == 600
    monkeypatch.setenv("LEANFLOW_PLANNER_SYNTHESIS_TIMEOUT_S", "1")
    assert planner_phase.planner_synthesis_timeout_s() == 30


def test_planner_lane_timeout_is_configurable_and_bounded(enabled, monkeypatch):
    monkeypatch.setenv("LEANFLOW_PLANNER_LANE_TIMEOUT_S", "480")
    assert planner_phase.planner_lane_wall_timeout_s() == 480
    monkeypatch.setenv("LEANFLOW_PLANNER_LANE_TIMEOUT_S", "5000")
    assert planner_phase.planner_lane_wall_timeout_s() == 1200
    monkeypatch.setenv("LEANFLOW_PLANNER_LANE_TIMEOUT_S", "1")
    assert planner_phase.planner_lane_wall_timeout_s() == 60


def test_lane_json_parser_and_deliverable_are_bounded():
    huge = {
        "findings": [
            {"claim": "x" * 4_000, "nested": {"levels": [[[[[[["too deep"]]]]]]]}}
            for _ in range(20)
        ]
    }
    parsed = planner_phase._extract_json_object(json.dumps(huge))

    assert parsed is not None
    normalized, was_bounded = planner_phase._normalize_lane_deliverable(parsed)
    assert was_bounded is True
    assert len(json.dumps(normalized, ensure_ascii=False)) <= (
        planner_phase.PLANNER_LANE_DELIVERABLE_MAX_CHARS
    )


def test_lane_json_parser_refuses_payload_beyond_input_ceiling():
    oversized = '{"value":"' + "x" * planner_phase.PLANNER_LANE_JSON_INPUT_MAX_CHARS + '"}'

    assert planner_phase._extract_json_object(oversized) is None


def test_false_affine_synthesis_is_rejected_before_any_planner_state_mutation(enabled, monkeypatch):
    """Replay the live false Erdős formula without persisting its plan claims."""
    synthesis = json.dumps(
        {
            "grounding": [
                "Probe whether a nonlinear construction could cover the residual family.",
                "Let n = 168*q+25. Then n+7 = 24*(7*q+4).",
            ],
            "strategy": ["Keep the speculative nonlinear route available for later testing."],
            "nodes": [
                {
                    "name": "false_affine",
                    "file": "Demo.lean",
                    "statement": (
                        "lemma false_affine (q n : ℕ) (h : n = 168*q+25) : "
                        "n+7 = 24*(7*q+4) := by sorry"
                    ),
                }
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    monkeypatch.setattr(
        planner_phase.decomposer,
        "place_helpers",
        lambda **_kwargs: pytest.fail("a refuted synthesis must not place stubs"),
    )
    original_blueprint = plan_state.load_blueprint()
    original_summary = plan_state.load_summary()

    outcome = planner_phase.run_planner_phase(
        goal="prove demo",
        target_symbol="demo",
        active_file="Demo.lean",
        agent=object(),
    )

    assert outcome.ok is False
    assert outcome.synthesis_status == planner_phase.PLANNER_ARITHMETIC_REJECTION_STATUS
    assert outcome.reason.startswith(
        planner_phase.orchestrator_arithmetic_preflight.ARITHMETIC_PREFLIGHT_REJECTION_PREFIX
    )
    assert "168*q+32 != 168*q+96" in outcome.reason
    assert outcome.nodes_added == 0
    assert outcome.stubs_placed == ()
    assert outcome.grounding_count == 0
    assert outcome.strategy_count == 0
    assert plan_state.load_blueprint() == original_blueprint
    assert plan_state.load_summary() == original_summary

    events = [
        json.loads(line)
        for line in plan_state.plan_state_paths()
        .journal_jsonl.read_text(encoding="utf-8")
        .splitlines()
    ]
    rejection = next(
        event for event in events if event["event"] == "planner-synthesis-arithmetic-rejected"
    )
    assert [
        (item["section"], item["index"], item.get("field", "")) for item in rejection["rejections"]
    ] == [("grounding", 1, "")]
    assert rejection["rejections"][0]["evidence"] == [
        {
            "kind": "affine-identity",
            "claim": "n+7=24*(7*q+4)",
            "evidence": "normalized affine forms differ: 168*q+32 != 168*q+96",
        }
    ]
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "node-created" not in journal
    assert "grounding_findings" not in journal


def test_valid_affine_synthesis_preserves_normal_planner_merge(enabled, monkeypatch):
    """A supported affine identity passes the conservative planner preflight."""
    grounding = "Let n = 168*q+25. Then n+7 = 168*q+32."
    strategy = "Let n = 168*q+25. Use n+7 = 168*q+32 before the next reduction."
    synthesis = json.dumps(
        {
            "grounding": [grounding],
            "strategy": [strategy],
            "nodes": [
                {
                    "name": "valid_affine",
                    "file": "Demo.lean",
                    "statement": (
                        "lemma valid_affine (q n : ℕ) (h : n = 168*q+25) : "
                        "n+7 = 168*q+32 := by sorry"
                    ),
                }
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="prove demo",
        target_symbol="demo",
        active_file="Demo.lean",
        agent=object(),
    )

    assert outcome.ok is True
    assert outcome.synthesis_status == "ok"
    assert outcome.nodes_added == 1
    assert outcome.stubs_placed == ("valid_affine",)
    summary = plan_state.load_summary()
    assert summary["grounding_findings"] == [grounding]
    assert summary["strategy_notes"] == [strategy]
    node = plan_state.load_blueprint().node_by_id(
        plan_state.node_id_for("valid_affine", "Demo.lean")
    )
    assert node is not None and node.status == "stated"
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-synthesis-arithmetic-rejected" not in journal


def test_empirical_enumerations_and_existential_helpers_fail_open(enabled, monkeypatch):
    """Historical examples and existential contracts are not universal assertions."""
    empirical = "Tested s=0, n=121; s=1, n=961; s=3, n=2641, and all exact checks passed."
    synthesis = json.dumps(
        {
            "grounding": [
                empirical,
                "Examples: mod17=2, mod11=1, mod53=3, and mod41=4 are covered.",
            ],
            "strategy": ["Keep the finite residue inventory and factor-pair route active."],
            "nodes": [
                {
                    "name": "factor_pair_witnesses",
                    "file": "Demo.lean",
                    "statement": (
                        "lemma factor_pair_witnesses (s : ℕ) : ∃ p₁ p₂ : ℕ, "
                        "p₁ * p₂ = (210*s+44)*(210*s+44) ∧ 7 ∣ p₁ ∧ 7 ∣ p₂ := by sorry"
                    ),
                    "notes": (
                        "M = (24*k+1)*(6*k+2), M ≡ 4 (mod 7) so 7|(M+10); "
                        "70|M*(M+10) from 2|M and 5|M or 5|(M+10)."
                    ),
                }
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="prove demo",
        target_symbol="demo",
        active_file="Demo.lean",
        agent=object(),
    )

    assert outcome.ok is True
    assert outcome.synthesis_status == "ok"
    assert plan_state.load_summary()["grounding_findings"][0] == empirical
    assert (
        "planner-synthesis-arithmetic-rejected"
        not in plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    )


def test_complete_false_rational_node_is_rejected_by_exact_counterexample() -> None:
    statement = """private lemma false_rational (q : ℕ) :
    (4 : ℚ) / ((168 * q + 25 : ℕ) : ℚ) =
      (1 : ℚ) / ((42 * q + 8 : ℕ) : ℚ) +
      (1 : ℚ) / (((168 * q + 25 : ℕ) * (7 * q + 4 : ℕ) : ℕ) : ℚ) +
      (1 : ℚ) / (((168 * q + 25 : ℕ) * (42 * q + 8 : ℕ) : ℕ) : ℚ) := by
  sorry"""

    rejections, note = planner_phase._synthesis_arithmetic_rejections(
        {"nodes": [{"name": "false_rational", "statement": statement}]}
    )

    assert rejections == (
        {
            "section": "nodes",
            "index": 0,
            "evidence": [
                {
                    "kind": "ground-rational-identity",
                    "claim": "false_rational",
                    "evidence": "exact counterexample at q=0: 4/25 != 7/50",
                }
            ],
            "field": "statement",
        },
    )
    assert "exact counterexample at q=0" in note


def test_hypothesis_bearing_affine_claims_are_not_treated_as_universal() -> None:
    rejections, note = planner_phase._synthesis_arithmetic_rejections(
        {
            "grounding": ["`normalize` proves k = 7*(k/7)+1 from k % 7 = 1."],
            "nodes": [
                {
                    "name": "conditional_identity",
                    "statement": (
                        "lemma conditional_identity (q : ℕ) (h : q = 1) : " "q = 1 := by sorry"
                    ),
                }
            ],
        }
    )

    assert rejections == ()
    assert note == ""


def test_lane_parse_failure_is_recorded_not_lost(enabled, monkeypatch):
    _fake_delegate(
        monkeypatch,
        _delegate_payload("utter prose, no json", '{"candidates": []}', "{}"),
    )
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert outcome.ok
    statuses = {lane["lane"]: lane["status"] for lane in outcome.lanes}
    assert statuses["web"] == "parse-failure"
    assert statuses["mathlib"] == "completed"
    web = next(lane for lane in outcome.lanes if lane["lane"] == "web")
    assert web["raw_summary"] == "utter prose, no json"
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert all(
        needle in journal for needle in ("planner-lanes", "parse-failure", "utter prose, no json")
    )


def test_delegate_explosion_yields_error_lanes(enabled, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("threads on fire")

    monkeypatch.setattr(planner_phase, "delegate_task", boom)
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    # Synthesis still runs (over zero deliverables); lanes carry the error.
    assert outcome.ok
    assert all(lane["status"] == "error" for lane in outcome.lanes)


def test_failed_lane_status_preserved(enabled, monkeypatch):
    _fake_delegate(
        monkeypatch,
        _delegate_payload("{}", "irrelevant", "{}", statuses=("completed", "failed", "error")),
    )
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    statuses = [lane["status"] for lane in outcome.lanes]
    assert statuses == ["completed", "failed", "error"]


@pytest.mark.parametrize("interrupted_status", ["interrupted", "cancelled", "canceled"])
def test_interrupted_lane_defers_synthesis_before_graph_mutation(
    enabled, monkeypatch, interrupted_status
):
    """An incomplete requested evidence portfolio cannot mint planner nodes."""
    _fake_delegate(
        monkeypatch,
        _delegate_payload(
            '{"findings": [{"claim": "known", "source": "paper"}]}',
            '{"candidates": [{"name": "Nat.le_trans"}]}',
            "Operation interrupted: waiting for model response",
            statuses=("completed", "completed", interrupted_status),
        ),
    )
    monkeypatch.setattr(
        planner_phase,
        "run_model_verification_review",
        lambda **_kwargs: pytest.fail("an interrupted lane must defer synthesis"),
    )
    before = plan_state.load_blueprint()

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok is False
    assert outcome.synthesis_status == "evidence-interrupted"
    assert outcome.nodes_added == 0
    assert outcome.stubs_placed == ()
    assert plan_state.load_blueprint() == before
    assert len(plan_state.load_summary()["grounding_findings"]) == 2
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-synthesis-deferred-incomplete-evidence" in journal
    assert interrupted_status in journal


def test_explicitly_unchecked_synthesis_node_is_not_admitted(enabled, monkeypatch):
    """A synthesizer cannot turn its own unchecked candidate into an obligation."""
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": ["Validate the candidate before using it."],
            "nodes": [
                {
                    "name": "checked_helper",
                    "file": "Demo.lean",
                    "statement": "lemma checked_helper : True := by sorry",
                },
                {
                    "name": "unchecked_universal_witness",
                    "file": "Demo.lean",
                    "statement": (
                        "lemma unchecked_universal_witness (n : Nat) : " "n % 3 = 0 := by sorry"
                    ),
                    "notes": (
                        "The divisibility still needs checking; if it fails, " "adjust the witness."
                    ),
                },
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    place_calls = _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok is True
    assert outcome.nodes_added == 1
    assert outcome.stubs_placed == ("checked_helper",)
    assert [call["skeletons"] for call in place_calls] == [
        ["lemma checked_helper : True := by sorry"]
    ]
    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(plan_state.node_id_for("checked_helper", "Demo.lean")) is not None
    assert (
        blueprint.node_by_id(plan_state.node_id_for("unchecked_universal_witness", "Demo.lean"))
        is None
    )
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-synthesis-node-uncertainty-rejected" in journal
    assert "needs-checking" in journal


@pytest.mark.parametrize(
    ("notes", "expected_kind"),
    [
        (
            "If B+1 is not always divisible by 3, the witness x must be changed.",
            "conditional-revision",
        ),
        (
            "The edge case t = 0 needs separate verification before assembly.",
            "needs-checking",
        ),
        (
            "This boundary branch needs independent validation.",
            "needs-checking",
        ),
        (
            "The exceptional residue needs explicit checking.",
            "needs-checking",
        ),
    ],
)
def test_residual_uncertainty_metadata_self_disqualifies_candidate(notes, expected_kind):
    admitted, rejected = planner_phase.planner_candidate_admission.partition_synthesis_nodes(
        [{"name": "candidate", "notes": notes}]
    )

    assert admitted == []
    assert rejected[0]["name"] == "candidate"
    assert expected_kind in {item["kind"] for item in rejected[0]["evidence"]}


@pytest.mark.parametrize(
    "notes",
    [
        "If h : B + 1 ∣ n, use h to rewrite the target.",
        "For the edge case t = 0, exact the previously proved base case.",
        "Split on whether B + 1 is divisible by 3 and prove both branches.",
    ],
)
def test_normal_theorem_conditions_do_not_self_disqualify_candidate(notes):
    candidate = {
        "name": "conditional_helper",
        "notes": notes,
        "statement": (
            "lemma conditional_helper : True := by "
            "-- If the witness fails, it must be changed.\n  trivial"
        ),
    }

    admitted, rejected = planner_phase.planner_candidate_admission.partition_synthesis_nodes(
        [candidate]
    )

    assert admitted == [candidate]
    assert rejected == ()


# ---------------------------------------------------------------------------
# Lane selection via probes
# ---------------------------------------------------------------------------


def test_lane_keys_select_and_alias(enabled, monkeypatch):
    calls = _fake_delegate(monkeypatch, _delegate_payload("{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object(), lane_keys=["deep-search"])

    assert outcome.ok
    assert [lane["lane"] for lane in outcome.lanes] == ["web"]
    assert len(calls[0]["tasks"]) == 1


def test_non_research_probe_selection_falls_back_to_full_wave(enabled, monkeypatch):
    calls = _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object(), lane_keys=["negation"])

    assert outcome.ok
    assert len(calls[0]["tasks"]) == 3


@pytest.mark.parametrize(("workers", "wave_sizes"), [(2, [2, 1]), (0, [1, 1, 1])])
def test_research_planner_waves_respect_background_parallelism(
    enabled, monkeypatch, workers, wave_sizes
):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", str(workers))
    calls: list[dict[str, Any]] = []

    def fake_delegate(**kwargs):
        calls.append(kwargs)
        return _delegate_payload(*("{}" for _task in kwargs["tasks"]))

    monkeypatch.setattr(planner_phase, "delegate_task", fake_delegate)
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert outcome.ok
    assert [len(call["tasks"]) for call in calls] == wave_sizes
    assert [lane["lane"] for lane in outcome.lanes] == ["web", "mathlib", "empirical"]


def test_capacity_deferred_lane_retries_after_sibling_releases_slot(enabled, monkeypatch):
    """Retry only the lane that lost a same-wave actor-capacity race."""
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "2")
    calls: list[dict[str, Any]] = []

    def fake_delegate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _delegate_payload(
                '{"findings": [{"claim": "web", "source": "paper"}]}',
                "",
                statuses=("completed", "capacity-deferred"),
            )
        if len(calls) == 2:
            return _delegate_payload('{"candidates": [{"name": "Nat.le_trans"}]}')
        return _delegate_payload('{"hypothesis": "h", "result": "supports"}')

    monkeypatch.setattr(planner_phase, "delegate_task", fake_delegate)
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert outcome.ok
    assert [len(call["tasks"]) for call in calls] == [2, 1, 1]
    assert calls[1]["tasks"][0]["goal"] == calls[0]["tasks"][1]["goal"]
    assert calls[2]["tasks"][0]["goal"] != calls[0]["tasks"][0]["goal"]
    assert [lane["lane"] for lane in outcome.lanes] == ["web", "mathlib", "empirical"]
    assert [lane["status"] for lane in outcome.lanes] == ["completed"] * 3
    assert outcome.lanes[0]["deliverable"]["findings"][0]["claim"] == "web"
    assert outcome.lanes[1]["deliverable"]["candidates"][0]["name"] == "Nat.le_trans"
    assert outcome.lanes[2]["deliverable"]["result"] == "supports"


def test_capacity_deferred_lane_retry_is_bounded(enabled, monkeypatch):
    """Keep a lane deferred when its one bounded retry still cannot acquire."""
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "2")
    calls: list[dict[str, Any]] = []

    def fake_delegate(**kwargs):
        calls.append(kwargs)
        return _delegate_payload("", statuses=("capacity-deferred",))

    monkeypatch.setattr(planner_phase, "delegate_task", fake_delegate)
    monkeypatch.setattr(
        planner_phase,
        "run_model_verification_review",
        lambda **_kwargs: pytest.fail("a still-deferred wave must not reach synthesis"),
    )

    outcome = planner_phase.run_planner_phase(
        goal="g",
        agent=object(),
        lane_keys=["deep-search"],
    )

    assert not outcome.ok
    assert outcome.synthesis_status == "capacity-deferred"
    assert [len(call["tasks"]) for call in calls] == [1, 1]
    assert calls[1]["tasks"][0]["goal"] == calls[0]["tasks"][0]["goal"]
    assert outcome.lanes == ({"lane": "web", "status": "capacity-deferred"},)


# ---------------------------------------------------------------------------
# Synthesizer failure modes keep the floor authoritative
# ---------------------------------------------------------------------------


def test_synthesizer_unavailable_fails_soft(enabled, monkeypatch):
    _fake_delegate(
        monkeypatch,
        _delegate_payload(
            '{"findings": [{"claim": "lane evidence survives", "source": "paper"}]}',
            '{"candidates": [{"name": "Nat.le_trans"}]}',
            '{"hypothesis": "small cases hold", "result": "supports"}',
        ),
    )
    _fake_synth(monkeypatch, response="", status="unavailable")

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert not outcome.ok and "unavailable" in outcome.reason
    assert outcome.grounding_count == 3
    assert outcome.lanes[0]["deliverable"]["findings"][0]["claim"] == ("lane evidence survives")
    assert plan_state.load_blueprint().nodes == ()  # nothing merged
    summary = plan_state.load_summary()
    assert len(summary["grounding_findings"]) == 3
    assert "lane evidence survives" in summary["grounding_findings"][0]
    assert "lane evidence survives" in plan_state.plan_state_paths().plan_md.read_text(
        encoding="utf-8"
    )
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-unsynthesized-findings" in journal
    assert "lane evidence survives" in journal


def test_unsynthesized_lane_projection_replaces_stale_payload_and_records_outcome(enabled):
    planner_phase._persist_unsynthesized_deliverables(
        {"web": {"finding": "old route", "raw": "x" * 4000}},
        reason="synthesizer timed out",
        target_symbol="demo",
        active_file="Demo.lean",
    )
    planner_phase._persist_unsynthesized_deliverables(
        {"web": {"finding": "new route", "raw": "y" * 4000}},
        reason="synthesizer timed out again",
        target_symbol="demo",
        active_file="Demo.lean",
    )

    grounding = plan_state.load_summary()["grounding_findings"]
    assert len(grounding) == 1
    assert "new route" in grounding[0]
    assert "old route" not in grounding[0]
    assert len(grounding[0]) <= planner_phase._UNSYNTHESIZED_GROUNDING_MAX_CHARS + 40
    outcomes = plan_state.recent_exploration_outcomes(
        plan_state.load_blueprint(),
        {"target_symbol": "demo", "active_file": "Demo.lean"},
    )
    assert outcomes[-1]["type"] == "research_preserved"
    assert outcomes[-1]["subject"] == "demo"
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "old route" in journal
    assert "new route" in journal


def test_unsynthesized_lane_projection_is_compact_and_keeps_actionable_lemmas(enabled):
    deliverable = {
        "findings": [
            {
                "claim": "Use the checked local residual lemma.",
                "source": "Demo.lean",
                "relevance": "It closes the next explicit subgoal.",
                "candidate_lemmas": ["residual_bound", "sum_filter_identity"],
                "raw_receipts": "x" * 20_000,
            }
        ],
        "providers_tried": ["local", "semantic"],
        "raw": "y" * 20_000,
    }

    planner_phase._persist_unsynthesized_deliverables(
        {"mathlib": deliverable},
        reason="synthesizer timed out",
        target_symbol="demo",
        active_file="Demo.lean",
    )

    grounding = plan_state.load_summary()["grounding_findings"]
    assert len(grounding) == 1
    assert "residual_bound" in grounding[0]
    assert "sum_filter_identity" in grounding[0]
    assert "raw_receipts" not in grounding[0]
    assert len(grounding[0]) <= planner_phase._UNSYNTHESIZED_GROUNDING_MAX_CHARS + 40


def test_interrupt_after_synthesis_never_enters_graph_or_stub_validation(enabled, monkeypatch):
    from tools.utilities.interrupt import CooperativeInterrupt, set_interrupt

    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    placement_calls = _fake_place(monkeypatch)

    def synthesize_then_interrupt(**_kwargs):
        set_interrupt(True)
        return SimpleNamespace(response=_SYNTHESIS, status="ok")

    monkeypatch.setattr(planner_phase, "run_model_verification_review", synthesize_then_interrupt)
    set_interrupt(False)
    try:
        with pytest.raises(CooperativeInterrupt, match="after synthesis review"):
            planner_phase.run_planner_phase(
                goal="g",
                target_symbol="demo",
                active_file="Demo.lean",
                agent=object(),
            )
    finally:
        set_interrupt(False)

    assert plan_state.load_blueprint().nodes == ()
    assert placement_calls == []


def test_interrupt_during_stub_validation_demotes_premerged_stated_node(enabled, monkeypatch):
    from tools.utilities.interrupt import CooperativeInterrupt

    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    monkeypatch.setattr(
        planner_phase,
        "_place_planner_stubs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CooperativeInterrupt("validation interrupted")
        ),
    )

    with pytest.raises(CooperativeInterrupt, match="validation interrupted"):
        planner_phase.run_planner_phase(
            goal="g",
            target_symbol="demo",
            active_file="Demo.lean",
            agent=object(),
        )

    helper = plan_state.load_blueprint().node_by_id(
        plan_state.node_id_for("demo_helper", "Demo.lean")
    )
    assert helper is not None
    assert helper.status == "conjectured"


def test_synthesizer_garbage_fails_soft_with_lanes_kept(enabled, monkeypatch):
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response="not json at all")

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert not outcome.ok and outcome.synthesis_status == "parse-failure"
    assert len(outcome.lanes) == 3  # N1: lane work still reported
    assert outcome.grounding_count == 3
    assert len(plan_state.load_summary()["grounding_findings"]) == 3


def test_stub_name_mismatch_is_skipped_and_journaled(enabled, monkeypatch):
    """Draft-phase name binding: the parsed declaration name is the name of
    record — a mismatched claim must never reach placement."""
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": [],
            "nodes": [
                {
                    "name": "claimed_name",
                    "file": "Demo.lean",
                    "statement": "lemma real_name : True := by sorry",
                }
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    place_calls = _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ()
    assert place_calls == []  # nothing reached the guarded door
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-stub-name-mismatch" in journal
    # The node was dropped BEFORE the graph merge: no phantom under either name.
    bp = plan_state.load_blueprint()
    assert bp.node_by_id(plan_state.node_id_for("claimed_name", "Demo.lean")) is None
    assert bp.node_by_id(plan_state.node_id_for("real_name", "Demo.lean")) is None


def test_nameless_stub_adopts_parsed_name_and_stays_tracked(enabled, monkeypatch):
    """A statement without a claimed name adopts the parsed declaration
    name — placed stubs are always graph-tracked."""
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": [],
            "nodes": [{"file": "Demo.lean", "statement": "lemma adopted : True := by sorry"}],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ("adopted",)
    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("adopted", "Demo.lean"))
    assert node is not None and node.status == "stated"


def test_lane_and_synthesis_prompts_embed_phase_fragments(enabled, monkeypatch):
    """§6.9 composition: the planner is a wired fragment consumer."""
    delegate_calls = _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    synth_calls = _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    planner_phase.run_planner_phase(goal="g", agent=object())

    web_goal = delegate_calls[0]["tasks"][0]["goal"]
    assert "[PHASE SPEC: phase-search]" in web_goal
    assert "Deliverable schema (YAML):" in web_goal  # schema rides with the body
    empirical_goal = delegate_calls[0]["tasks"][2]["goal"]
    assert "[PHASE SPEC" not in empirical_goal  # plausibility lane, not the kernel probe
    assert "at most 12 deliberately chosen small cases" in empirical_goal
    assert "at most 2 empirical_compute calls" in empirical_goal
    assert "no filesystem or project-mutation authority" in empirical_goal
    assert "trial-divide a squared denominator" in empirical_goal
    assert "complete compatible residue basis" in empirical_goal
    tasks = delegate_calls[0]["tasks"]
    assert tasks[1]["toolsets"] == ["lean-research"]
    assert tasks[2]["toolsets"] == ["empirical-compute", "lean-research"]
    assert "_pre_tool_call_callback" not in tasks[0]
    assert "_pre_tool_call_callback" not in tasks[1]
    empirical_policy = tasks[2]["_pre_tool_call_callback"]
    compute_args = {"program": "print(2 + 2)", "timeout_s": 180}
    assert empirical_policy("empirical_compute", compute_args) is None
    assert compute_args["timeout_s"] == 8
    assert delegate_calls[0]["empirical_task_indexes"] == frozenset({2})
    assert delegate_calls[0]["task_iteration_limits"] == {
        2: planner_phase.EMPIRICAL_LANE_MAX_ITERATIONS
    }
    synth_prompt = synth_calls[0]["prompt"]
    assert "[PHASE SPEC: phase-planning]" in synth_prompt
    assert "[PHASE SPEC: phase-draft]" in synth_prompt


def test_synthesis_preserves_prior_exact_target_evidence(enabled, monkeypatch):
    """A later plan turn must receive the full recovered construction."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    synth_calls = _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    planner_phase.run_planner_phase(
        goal="g",
        target_symbol="demo",
        active_file="Demo.lean",
        agent=object(),
        prior_evidence=(
            {
                "source": "lean_reasoning_help",
                "text": (
                    "Use adjacent subset sums, cancel the intersection, "
                    "then common-refine the two disjoint subfamilies."
                ),
            },
        ),
    )

    prompt = synth_calls[0]["prompt"]
    assert "Previously recovered exact-target evidence" in prompt
    assert "adjacent subset sums" in prompt
    assert "common-refine the two disjoint subfamilies" in prompt
    assert "do not replace it with a vaguer rediscovery plan" in prompt


def test_lane_prompts_are_scoped_to_the_exact_active_assignment(enabled, monkeypatch):
    """Planner fan-out must not silently fall back to the whole-file prompt."""
    delegate_calls = _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    synth_calls = _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    planner_phase.run_planner_phase(
        goal="/prove FormalConjectures/ErdosProblems/242.lean",
        target_symbol="erdos_242_residual_mod_seven_eq_five",
        active_file="FormalConjectures/ErdosProblems/242.lean",
        declaration_slice=(
            "private lemma erdos_242_residual_mod_seven_eq_five (k : ℕ) "
            "(hk : k % 7 = 5) : ∃ x y z, (4 : ℚ) / (24 * k + 1) = "
            "1 / x + 1 / y + 1 / z := by\n  sorry"
        ),
        lean_goal="k : ℕ\nhk : k % 7 = 5\n⊢ ∃ x y z, (4 : ℚ) / (168 * (k / 7) + 121) = _",
        requested_route="plan",
        failed_route_signature='{"proof_shapes":["search-only"],"route":"plan"}',
        search_signature='{"search_count":12,"used_tools":{"lean_search":7,"web_search":5}}',
        agent=object(),
    )

    tasks = delegate_calls[0]["tasks"]
    assert len(tasks) == 3
    for task in tasks:
        prompt = task["goal"]
        assert "erdos_242_residual_mod_seven_eq_five" in prompt
        assert "FormalConjectures/ErdosProblems/242.lean" in prompt
        assert "24 * k + 1" in prompt
        assert "168 * (k / 7) + 121" in prompt
        assert "Requested route: plan" in prompt
        assert '"proof_shapes":["search-only"]' in prompt
        assert '"search_count":12' in prompt
        assert "Do not broaden to the whole file" in prompt
        assert "workflow command, not a filesystem path" in prompt

    # Each lane keeps its own evidence contract after receiving the same
    # assignment envelope.
    assert '"findings"' in tasks[0]["goal"] and '"source"' in tasks[0]["goal"]
    assert '"candidate_lemmas"' in tasks[1]["goal"]
    assert '"hypothesis"' in tasks[2]["goal"] and '"counterexample"' in tasks[2]["goal"]

    synth_prompt = synth_calls[0]["prompt"]
    assert "erdos_242_residual_mod_seven_eq_five" in synth_prompt
    assert "168 * (k / 7) + 121" in synth_prompt
    assert '"search_count":12' in synth_prompt


def test_clean_room_web_lane_forbids_repository_solutions(enabled, monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv(
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        "IMO 2026 Problem 6|IMO2026 P6",
    )
    delegate_calls = _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    planner_phase.run_planner_phase(
        goal="/prove IMO2026/P6.lean",
        target_symbol="result",
        active_file="IMO2026/P6.lean",
        declaration_slice="theorem result : True := by sorry",
        agent=object(),
    )

    web_prompt = delegate_calls[0]["tasks"][0]["goal"]
    assert "clean-room run" in web_prompt
    assert "do not search, fetch, clone, or cite source-code repositories" in web_prompt
    assert "do not search for, fetch, cite, or use any existing or official solution" in web_prompt
    assert "IMO 2026 Problem 6" in web_prompt
    assert "Clone promising proof developments" not in web_prompt


def test_sibling_file_statements_defer_to_conjectures(enabled, monkeypatch):
    """This phase places only into the active file — a statement aimed at a
    sibling file must not mint a frontier-eligible stated node."""
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": [],
            "nodes": [{"file": "Other.lean", "statement": "lemma elsewhere : True := by sorry"}],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    place_calls = _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ()
    assert place_calls == []
    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("elsewhere", "Other.lean"))
    # The idea survives as a NAMED conjecture: the parsed declaration name
    # is adopted before the deferral strips the statement.
    assert node is not None and node.status == "conjectured" and node.statement == ""
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-stub-deferred" in journal


def test_malformed_statement_enters_as_conjecture_never_stated(enabled, monkeypatch):
    """A statement failing the stub-shape guard is stripped: the idea
    survives as a conjecture, never as a phantom frontier-eligible node."""
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": [],
            "nodes": [
                {"name": "bad_shape", "file": "Demo.lean", "statement": "lemma bad_shape : True"}
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    place_calls = _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ()
    assert place_calls == []
    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("bad_shape", "Demo.lean"))
    assert node is not None and node.status == "conjectured" and node.statement == ""
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-stub-shape-rejected" in journal


def test_placement_failure_demotes_stated_nodes(enabled, monkeypatch):
    """A stated node whose stub never landed on disk must not stay
    frontier-eligible — it demotes back to a conjecture, journaled."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)  # states demo_helper for Demo.lean
    _fake_place(monkeypatch, ok=False)  # the guarded door rejects the batch

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ()
    node = plan_state.load_blueprint().node_by_id(
        plan_state.node_id_for("demo_helper", "Demo.lean")
    )
    assert node is not None and node.status == "conjectured"
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner stub not placed" in journal


def test_over_cap_stubs_are_demoted_not_phantom(enabled, monkeypatch):
    """Stubs past the per-batch placement cap demote to conjectures."""
    names = [f"h{i}" for i in range(6)]
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": [],
            "nodes": [
                {
                    "name": name,
                    "file": "Demo.lean",
                    "statement": f"lemma {name} : True := by sorry",
                }
                for name in names
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    _fake_place(monkeypatch)  # places whatever reaches it (the capped batch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and len(outcome.stubs_placed) == 4  # the batch cap
    bp = plan_state.load_blueprint()
    statuses = {
        name: bp.node_by_id(plan_state.node_id_for(name, "Demo.lean")).status for name in names
    }
    assert sum(1 for s in statuses.values() if s == "stated") == 4
    assert sum(1 for s in statuses.values() if s == "conjectured") == 2


def test_duplicate_restatement_never_demotes_existing_node(enabled, monkeypatch):
    """A re-stated duplicate of an ALREADY-stated node must keep its status
    even when its (redundant) placement is rejected."""
    node_id = plan_state.node_id_for("demo_helper", "Demo.lean")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo_helper",
                    file="Demo.lean",
                    statement="lemma demo_helper : True := by sorry",
                    status="stated",
                ),
            )
        )
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)  # re-states demo_helper
    _fake_place(monkeypatch, ok=False)  # duplicate placement rejected

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok
    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"


def test_signature_conflict_is_not_placed_or_bound_to_proved_node(enabled, monkeypatch):
    """A different private declaration cannot borrow graph truth or poison placement."""
    split_id = plan_state.node_id_for("residue_split", "Demo.lean")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=split_id,
                    name="residue_split",
                    file="Demo.lean",
                    statement=(
                        "private lemma residue_split (k : ℕ) (hmod : k % 7 = 1) : "
                        "k % 35 = 1 ∨ k % 35 = 8 := by omega"
                    ),
                    status="proved",
                ),
            )
        )
    )
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": [],
            "nodes": [
                {
                    "name": "residue_split",
                    "file": "Demo.lean",
                    "statement": (
                        "private lemma residue_split (k : ℕ) (hk : 1 ≤ k) "
                        "(hmod : k % 7 = 1) : "
                        "k % 35 = 1 ∨ k % 35 = 8 := by sorry"
                    ),
                },
                {
                    "name": "fresh_branch",
                    "file": "Demo.lean",
                    "statement": "private lemma fresh_branch : True := by sorry",
                    "depends_on": ["residue_split"],
                },
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    place_calls = _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok
    assert outcome.stubs_placed == ("fresh_branch",)
    assert len(place_calls) == 1
    assert place_calls[0]["skeletons"] == ["private lemma fresh_branch : True := by sorry"]
    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(split_id).status == "proved"
    branch_id = plan_state.node_id_for("fresh_branch", "Demo.lean")
    assert not any(edge.source == branch_id and edge.target == split_id for edge in blueprint.edges)
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "plan-delta-node-signature-conflict" in journal


def test_plan_md_renders_after_demotion(enabled, monkeypatch):
    """Routing must never consume a frontier that lists failed stubs."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)  # states demo_helper
    _fake_place(monkeypatch, ok=False)  # placement fails => demotion

    planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    plan_md = plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")
    frontier = plan_md[plan_md.index("## Frontier") : plan_md.index("## Grounding")]
    assert "demo_helper" not in frontier  # demoted before the render


def test_synthesis_stubs_key_is_accepted_as_nodes(enabled, monkeypatch):
    """The draft-phase field name is tolerated: `stubs` == `nodes`."""
    synthesis = json.dumps(
        {
            "grounding": [],
            "strategy": [],
            "stubs": [
                {
                    "name": "via_alias",
                    "file": "Demo.lean",
                    "statement": "lemma via_alias : True := by sorry",
                }
            ],
        }
    )
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response=synthesis)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ("via_alias",)


def test_synthesis_prompt_draft_fragment_is_policy_only(enabled, monkeypatch):
    """phase-draft rides the synthesis prompt WITHOUT its stubs schema —
    the reply contract stays the nodes JSON."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    synth_calls = _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    planner_phase.run_planner_phase(goal="g", agent=object())

    prompt = synth_calls[0]["prompt"]
    draft_at = prompt.index("[PHASE SPEC: phase-draft]")
    assert "Deliverable schema (YAML):" not in prompt[draft_at:]
    assert "your reply contract is ONLY the nodes JSON above" in prompt


def test_rejected_stub_placement_is_journaled(enabled, monkeypatch):
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch, ok=False)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ()
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-stubs-rejected" in journal


def test_delegate_toplevel_error_object_becomes_error_lanes(enabled, monkeypatch):
    """delegate_task guard failures arrive as {'error': ...}, not exceptions."""
    _fake_delegate(monkeypatch, json.dumps({"error": "Delegation depth limit reached."}))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert all(lane["status"] == "error" for lane in outcome.lanes)
    assert "Delegation depth limit" in outcome.lanes[0]["error"]


def test_post_fanout_exception_keeps_lanes_in_outcome(enabled, monkeypatch):
    """N1: lane work done before a late failure stays in the payload."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    monkeypatch.setattr(
        planner_phase.plan_state,
        "save_summary",
        lambda payload: (_ for _ in ()).throw(OSError("disk full")),
    )

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert not outcome.ok and "OSError" in outcome.reason
    assert len(outcome.lanes) == 3


def test_revision_conflict_retry_journals_final_changes_once(enabled, monkeypatch):
    """The notebook describes the graph that was PERSISTED — one change set,
    journaled only after the save wins."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)
    real_save = plan_state.save_blueprint
    fails = {"left": 1}

    def flaky_save(bp):
        if fails["left"]:
            fails["left"] -= 1
            raise plan_state.PlanStateRevisionConflict("raced")
        return real_save(bp)

    monkeypatch.setattr(planner_phase.plan_state, "save_blueprint", flaky_save)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert outcome.ok
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    helper_creations = [
        line for line in journal.splitlines() if "node-created" in line and "demo_helper" in line
    ]
    assert len(helper_creations) == 1


def test_never_raises(enabled, monkeypatch):
    monkeypatch.setattr(
        planner_phase.plan_state,
        "load_blueprint",
        lambda: (_ for _ in ()).throw(RuntimeError("corrupt")),
    )
    outcome = planner_phase.run_planner_phase(goal="g", agent=object())
    assert not outcome.ok and "RuntimeError" in outcome.reason


# ---------------------------------------------------------------------------
# Runner wiring: mechanical-first with directive fallback
# ---------------------------------------------------------------------------


def _apply_plan_route(autonomy_state: dict[str, Any], history: list) -> str:
    return runner._orchestrator_apply_route(
        OrchestratorRoute(route="plan", reason="scope-entry planning"),
        history,
        autonomy_state,
        {},
        agent=None,
    )


def test_runner_plan_route_uses_planner_when_enabled(enabled, monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(
        runner.planner_phase,
        "run_planner_phase",
        lambda **kwargs: planner_phase.PlannerOutcome(
            ok=True, reason="planner phase completed", nodes_added=2, stubs_placed=("h1",)
        ),
    )
    history: list[dict] = []

    action = _apply_plan_route(
        {"_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}},
        history,
    )

    assert action == "continue"
    assert history and "[LEANFLOW ORCHESTRATOR ROUTE: plan]" in history[-1]["content"]
    assert "planner phase ran" in history[-1]["content"]
    assert any(a[0] == "planner" for a, _k in events)


def test_runner_plan_route_falls_back_to_directive(enabled, monkeypatch):
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(
        runner.planner_phase,
        "run_planner_phase",
        lambda **kwargs: planner_phase.PlannerOutcome(ok=False, reason="synth down"),
    )
    history: list[dict] = []

    action = _apply_plan_route(
        {"_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}},
        history,
    )

    assert action == "continue"
    assert history and "- directive:" in history[-1]["content"]


def test_runner_disabled_planner_releases_pending_capacity(monkeypatch):
    """Text-only plan fallback cannot retain a mechanical planner slot."""
    monkeypatch.delenv("LEANFLOW_PLANNER_ENABLED", raising=False)
    cleared: list[dict[str, Any]] = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(
        runner,
        "_clear_pending_plan_capacity",
        lambda state: cleared.append(state) or True,
    )
    state = {
        "_orchestrator_last_ctx": {
            "target_symbol": "demo",
            "active_file": "Demo.lean",
        },
        "prover_requested_route": {"route": "plan"},
    }
    history: list[dict] = []

    action = _apply_plan_route(state, history)

    assert action == "continue"
    assert cleared == [state]
    assert history and "- directive:" in history[-1]["content"]


def test_runner_retries_capacity_deferred_planner_at_next_boundary(enabled, monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(
        runner.planner_phase,
        "run_planner_phase",
        lambda **kwargs: planner_phase.PlannerOutcome(
            ok=False,
            reason="planner background capacity busy; lane wave deferred",
            synthesis_status="capacity-deferred",
            lanes=({"lane": "web", "status": "capacity-deferred"},),
        ),
    )
    autonomy_state = {
        "_orchestrator_last_ctx": {
            "target_symbol": "demo",
            "active_file": "Demo.lean",
        }
    }
    history: list[dict] = []

    action = _apply_plan_route(autonomy_state, history)

    assert action == "continue"
    assert autonomy_state["prover_requested_route"] == {
        "route": "plan",
        "target_symbol": "demo",
        "active_file": "Demo.lean",
        "reason": "planner background capacity deferred",
    }
    planner_event = next(details for args, details in events if args[0] == "planner")
    assert planner_event["synthesis_status"] == "capacity-deferred"


def test_runner_planner_exception_reconciles_source_before_directive_fallback(enabled, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("planner crashed")),
    )

    def reconcile(state):
        state["operational_pause"] = "paused_source_quarantine"
        return {"active": 1}

    monkeypatch.setattr(runner, "_reconcile_source_transaction_state", reconcile)
    state = {
        "_orchestrator_last_ctx": {
            "target_symbol": "demo",
            "active_file": "Demo.lean",
        }
    }

    assert _apply_plan_route(state, []) == "stop:source-quarantine"


def test_runner_planner_exception_pauses_infrastructure_when_source_is_clean(enabled, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("planner crashed")),
    )
    monkeypatch.setattr(
        runner,
        "_reconcile_source_transaction_state",
        lambda _state: {"active": 0},
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner.campaign_epoch, "record_status", lambda *_args, **_kwargs: None)
    state = {
        "_orchestrator_last_ctx": {
            "target_symbol": "demo",
            "active_file": "Demo.lean",
        }
    }

    assert _apply_plan_route(state, []) == "stop:infrastructure-pause"
    assert state["operational_pause"] == "paused_infrastructure"
    assert "planner crashed" in state["infrastructure_pause_reason"]


def test_runner_plan_route_flag_off_is_directive_only(monkeypatch, tmp_path):
    monkeypatch.delenv("LEANFLOW_PLANNER_ENABLED", raising=False)
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "ps"))
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    def explode(**kwargs):
        raise AssertionError("planner must not run when the flag is off")

    monkeypatch.setattr(runner.planner_phase, "run_planner_phase", explode)
    history: list[dict] = []

    action = _apply_plan_route(
        {"_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}},
        history,
    )

    assert action == "continue"
    assert history and "- directive:" in history[-1]["content"]
