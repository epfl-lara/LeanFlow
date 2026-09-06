"""Characterize bounded source authority, scheduling, and full prover transitions."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.models import Dag, Node
from leanflow_cli.workflows.prover.planning import apply_proposal
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.scheduler import ready_nodes
from leanflow_cli.workflows.prover.source import (
    SourceConflictError,
    SourceDocument,
    discover,
    sorry_spans,
)


class Verifier:
    """Record deterministic parent gates without requiring a provider or Lean fixture."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.checked: list[str] = []

    def check(self, node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        self.checked.append(node.id)
        return {"accepted": self.accepted}

    def compile_module(self, relative: str) -> dict[str, Any]:
        return {"accepted": True}

    def final(self, files: list[Path]) -> dict[str, Any]:
        return {"accepted": self.accepted}


def project(
    tmp_path: Path, source: str = "import Std\n\ntheorem goal : True := by sorry\n"
) -> Path:
    path = tmp_path / "Main.lean"
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    path.write_bytes(source.encode())
    return path


def session(**kwargs: Any) -> dict[str, Any]:
    return {
        "status": "candidate",
        "api_calls": 1,
        "final_response": json.dumps({"proof": "trivial", "notes": "closed"}),
    }


def test_literal_holes_ignore_comments_strings_quoted_names_and_identifier_suffixes() -> None:
    text = 'theorem a : True := by sorry\n/- sorry /- sorry -/ -/\n-- sorry\n#check "sorry"\n#check «sorry»\n#check sorry\'\n'
    assert len(sorry_spans(text)) == 1


def test_source_document_preserves_every_non_hole_byte() -> None:
    before = "import Std\r\n/- retained -/\r\ntheorem t : True := by\r\n  sorry\r\n"
    document = SourceDocument("Main.lean", before)
    document.replacements["0"] = "trivial"
    assert document.render().encode() == before.replace("  sorry", "  trivial").encode()


def test_discovery_requires_definition_authorization(tmp_path: Path) -> None:
    path = project(tmp_path, "def value : Nat := sorry\n")
    with pytest.raises(ValueError, match="fill_definitions"):
        discover(tmp_path, [path], fill_definitions=False)
    dag, _ = discover(tmp_path, [path], fill_definitions=True)
    assert dag.nodes[0].kind == "def"


def test_discovery_collects_multiple_holes_without_rewriting_partial_proof(tmp_path: Path) -> None:
    path = project(
        tmp_path, "theorem both : True ∧ True := by\n  constructor\n  · sorry\n  · sorry\n"
    )
    dag, _ = discover(tmp_path, [path], fill_definitions=False)
    assert dag.nodes[0].holes == [0, 1]


def test_dag_cycles_missing_dependencies_and_unsafe_paths_rejected() -> None:
    a = Node("a", "a", "theorem a : True", "A.lean", "A", dependencies=["b"])
    b = Node("b", "b", "theorem b : True", "B.lean", "B", dependencies=["a"])
    with pytest.raises(ValueError, match="cycle"):
        Dag([a, b], ["a"]).validate()
    with pytest.raises(ValueError, match="missing"):
        Dag([a], ["a"]).validate()
    a.dependencies = []
    a.file = "../Bad.lean"
    with pytest.raises(ValueError, match="invalid"):
        Dag([a], ["a"]).validate()


def test_depth_first_scheduler_deduplicates_shared_dependencies() -> None:
    helper = Node("h", "h", "theorem h : True", "H.lean", "H")
    a = Node("a", "a", "theorem a : True", "A.lean", "A", dependencies=["h"])
    b = Node("b", "b", "theorem b : True", "B.lean", "B", dependencies=["h"])
    dag = Dag([a, b, helper], ["a", "b"])
    assert [node.id for node in ready_nodes(dag, order="bottom-up", active=set(), limit=2)] == ["h"]
    assert [node.id for node in ready_nodes(dag, order="top-down", active=set(), limit=3)] == [
        "a",
        "h",
        "b",
    ]
    assert ready_nodes(dag, order="top-down", active=set(), limit=0) == []
    assert [node.id for node in ready_nodes(dag, order="top-down", active={"h"}, limit=3)] == [
        "a",
        "b",
    ]


def test_planner_cannot_mutate_root_statement_or_inject_commands() -> None:
    root = Node("r", "goal", "theorem goal : True", "Main.lean", "Main", original=True)
    dag = Dag([root], ["r"])
    with pytest.raises(ValueError, match="immutable"):
        apply_proposal(
            dag, {"nodes": [{"id": "r", "statement": "theorem goal : False"}]}, max_nodes=10
        )
    for statement in [
        "axiom magic : False\ntheorem help : True := by sorry",
        'theorem help : True := by sorry\nmacro "cheat" : tactic => `(tactic|sorry)',
    ]:
        with pytest.raises(ValueError):
            apply_proposal(
                dag, {"nodes": [{"id": "h", "name": "help", "statement": statement}]}, max_nodes=10
            )


def test_standard_runtime_completes_only_after_parent_gate(tmp_path: Path) -> None:
    path = project(tmp_path)
    verifier = Verifier()
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(job_api_calls=3),
        session=session,
        verifier=verifier,
    )
    result = runtime.run()
    assert result["status"] == "completed"
    assert result["terminal"] is True
    assert path.read_text().endswith("by trivial\n")
    assert len(verifier.checked) == 1
    assert result["metrics"]["api_calls"] == 1
    assert [job["role"] for job in result["jobs"]] == ["prover"]
    assert Path(result["changes"][0]["baseline_path"]).read_text().endswith("by sorry\n")


def test_parent_gate_rejection_never_installs_child_proof(tmp_path: Path) -> None:
    path = project(tmp_path)
    before = path.read_bytes()
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(job_api_calls=1, max_restarts=0),
        session=session,
        verifier=Verifier(False),
    )
    result = runtime.run()
    assert result["status"] == "blocked"
    assert path.read_bytes() == before


def test_global_budget_caps_all_retry_jobs(tmp_path: Path) -> None:
    path = project(tmp_path)

    def unsuccessful(**kwargs: Any) -> dict[str, Any]:
        scratch = Path(kwargs["context"]["scratch_file"])
        scratch.write_text(
            scratch.read_text().replace("sorry", "have h : True := by sorry; exact h")
        )
        return {
            "status": "budget_exhausted",
            "api_calls": kwargs["api_budget"],
            "final_response": json.dumps({"notes": kwargs["context"]["job_id"], "promising": True}),
        }

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(job_api_calls=2, total_api_calls=3),
        session=unsuccessful,
        verifier=Verifier(),
    )
    result = runtime.run()
    assert result["status"] == "budget_exhausted"
    assert [job["api_budget"] for job in result["jobs"]] == [2, 1]
    assert result["metrics"]["api_calls"] == 3
    assert path.read_text().endswith("by sorry\n")


def test_source_changed_by_worker_is_never_accepted(tmp_path: Path) -> None:
    path = project(tmp_path)
    other = tmp_path / "Existing.lean"
    other.write_text("def original : Nat := 7\n")

    def unsafe(**kwargs: Any) -> dict[str, Any]:
        other.write_text("def original : Nat := 9\n")
        return session(**kwargs)

    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=unsafe, verifier=Verifier()
    )
    result = runtime.run()
    assert result["status"] == "source_conflict"
    assert "saved baselines" in result["next_step"]
    assert "protected source changed" in result["error"]
    assert path.read_text().endswith("by sorry\n")


def test_multi_hole_candidate_preserves_existing_constructor(tmp_path: Path) -> None:
    source = "theorem both : True ∧ True := by\n  constructor\n  · sorry\n  · sorry\n"
    path = project(tmp_path, source)

    def multiple(**kwargs: Any) -> dict[str, Any]:
        return {
            "status": "candidate",
            "api_calls": 1,
            "final_response": json.dumps({"proofs": ["trivial", "trivial"]}),
        }

    result = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=multiple, verifier=Verifier()
    ).run()
    assert result["status"] == "completed"
    assert path.read_text() == source.replace("sorry", "trivial")


def test_research_creates_reviewed_helper_and_proves_bottom_up(tmp_path: Path) -> None:
    path = project(tmp_path)
    roles: list[str] = []

    def planned(**kwargs: Any) -> dict[str, Any]:
        role, context = kwargs["role"], kwargs["context"]
        roles.append(role)
        if role == "review":
            report = {"accepted": True}
        elif role == "orchestrator" and len(roles) == 1:
            report = {"plan": "Prove a small helper, then use it."}
        elif role == "orchestrator":
            root = context["dag"]["nodes"][0]
            report = {
                "plan": "Use help.",
                "nodes": [
                    {
                        "id": "helper",
                        "name": "help",
                        "statement": "theorem help : True ∧ True := by sorry",
                        "file": "LeanFlowProofs/Help.lean",
                    },
                    {"id": root["id"], "statement": root["statement"], "dependencies": ["helper"]},
                ],
            }
        else:
            report = {
                "proof": "trivial" if context["assignment"]["id"] == "helper" else "exact help"
            }
        return {"status": "candidate", "api_calls": 1, "final_response": json.dumps(report)}

    result = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="research"),
        session=planned,
        verifier=Verifier(),
    ).run()
    assert result["status"] == "completed", result.get("error")
    assert roles == ["orchestrator", "orchestrator", "review", "prover", "prover"]
    assert result["metrics"]["plan_refinements"] == 0
    assert "import LeanFlowProofs.Help" in path.read_text()
    assert (tmp_path / "LeanFlowProofs/Help.lean").read_text().endswith("by trivial\n")


def test_resume_preserves_original_configuration_and_progress(tmp_path: Path) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(total_api_calls=4),
        session=session,
        verifier=Verifier(),
    )
    original = runtime.run()
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(total_api_calls=999),
        run_id=runtime.run_id,
        resume=True,
        session=session,
        verifier=Verifier(),
    )
    result = resumed.run()
    assert result["status"] == "completed"
    assert resumed.config.total_api_calls == 4
    assert len(result["jobs"]) == len(original["jobs"])


def test_provider_failure_stops_without_replanning_or_futile_retries(tmp_path: Path) -> None:
    path = project(tmp_path)

    def unavailable(**kwargs: Any) -> dict[str, Any]:
        return {
            "status": "provider_error",
            "api_calls": 1,
            "error": "Connection error.",
            "final_response": "",
        }

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        session=unavailable,
        verifier=Verifier(),
    )
    state = runtime.run()
    assert state["status"] == "provider_error"
    assert state["error"] == "Connection error."
    assert len(state["jobs"]) == 1
    assert path.read_text().endswith("by sorry\n")


def test_retry_retains_partial_scratch_and_private_notes(tmp_path: Path) -> None:
    path = project(tmp_path)
    calls = 0

    def continuing(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        scratch = Path(kwargs["context"]["scratch_file"])
        notes = Path(kwargs["workspace"]) / "PLAN_job.md"
        if calls == 1:
            scratch.write_text(
                scratch.read_text().replace("sorry", "\n  have h : True := by sorry\n  exact h")
            )
            notes.write_text("Finish the concrete h subgoal.")
            return {
                "status": "budget_exhausted",
                "api_calls": 1,
                "final_response": json.dumps(
                    {"notes": "one local subgoal remains", "promising": True}
                ),
            }
        assert "have h : True" in scratch.read_text()
        assert "concrete h" in notes.read_text()
        return session(**kwargs)

    state = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(max_restarts=1),
        session=continuing,
        verifier=Verifier(),
    ).run()
    assert state["status"] == "completed"
    assert len(state["jobs"]) == 2


def test_interrupted_job_resumes_same_admission_ledger_and_scratch(tmp_path: Path) -> None:
    path = project(tmp_path)
    first = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(job_api_calls=3, total_api_calls=3),
        session=session,
        verifier=Verifier(),
    )
    node = first.dag.nodes[0]
    job, _ = first._new_job("prover", node=node)
    node.status, node.attempts = "running", 1
    job["input_tokens"] = 10
    workspace = Path(job["workspace"])
    ledger = workspace.parent / ".runtime" / workspace.name / "request-count.json"
    ledger.write_text(json.dumps({"limit": 3, "used": 1}))
    first._persist()

    def resumed_session(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["workspace"] == workspace
        assert kwargs["api_budget"] == 3
        assert kwargs["context"]["original_file"] == "Main.lean"
        assert kwargs["context"]["scratch_holes"] == [0]
        return {
            "status": "candidate",
            "api_calls": 2,
            "input_tokens": 5,
            "final_response": json.dumps({"proof": "trivial"}),
        }

    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        session=resumed_session,
        verifier=Verifier(),
        run_id=first.run_id,
        resume=True,
    )
    state = resumed.run()
    assert state["status"] == "completed"
    assert len(state["jobs"]) == 1
    assert state["metrics"]["api_calls"] == 2
    assert state["metrics"]["input_tokens"] == 15
    assert state["metrics"]["reserved_api_calls"] == 0
    assert state["dag"]["nodes"][0]["attempts"] == 1


def test_parallel_research_results_are_consumed_in_completion_order(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.planning_controller import launch_research_requests

    path = project(tmp_path)
    fast_started = threading.Event()

    def research(**kwargs: Any) -> dict[str, Any]:
        if kwargs["prompt"].endswith("slow"):
            assert fast_started.wait(2)
            time.sleep(0.02)
            report = "slow-result"
        else:
            fast_started.set()
            report = "fast-result"
        return {"status": "complete", "api_calls": 1, "final_response": report}

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="research", parallelism=2),
        session=research,
        verifier=Verifier(),
    )
    launch_research_requests(runtime, [{"question": "slow"}, {"question": "fast"}])
    plan = runtime.state["plan_markdown"]
    assert plan.index("fast-result") < plan.index("slow-result")
    assert runtime.state["metrics"]["api_calls"] == 2


def test_materialization_rolls_back_original_imports_and_lake_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover.planning_controller import materialize

    path = project(tmp_path)
    lakefile = tmp_path / "lakefile.toml"
    before, lake_before = path.read_bytes(), lakefile.read_bytes()
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=session, verifier=Verifier()
    )
    root = runtime.dag.nodes[0]
    dag, skeletons = apply_proposal(
        runtime.dag,
        {
            "nodes": [
                {
                    "id": "helper",
                    "name": "help",
                    "statement": "theorem help : True ∧ True := by sorry",
                    "file": "LeanFlowProofs/Help.lean",
                },
                {"id": root.id, "statement": root.statement, "dependencies": ["helper"]},
            ]
        },
        max_nodes=10,
    )

    def fail(*args: Any) -> None:
        raise OSError("simulated baseline write failure")

    monkeypatch.setattr(runtime, "_change", fail)
    with pytest.raises(OSError, match="simulated"):
        materialize(runtime, dag, skeletons)
    assert path.read_bytes() == before
    assert lakefile.read_bytes() == lake_before
    assert not (tmp_path / "LeanFlowProofs/Help.lean").exists()
    runtime._assert_sources()


def test_rejected_submission_feedback_stays_inside_same_job(tmp_path: Path) -> None:
    path = project(tmp_path)
    before = path.read_bytes()

    class CandidateVerifier(Verifier):
        def check(self, node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
            accepted = "by trivial" in file.read_text()
            return {"accepted": accepted, "error": "unknown identifier bad" if not accepted else ""}

    def persistent(**kwargs: Any) -> dict[str, Any]:
        feedback = kwargs["config"]["_candidate_feedback"]
        rejected = feedback(json.dumps({"proof": "bad"}))
        assert rejected["accepted"] is False
        assert "unknown identifier" in rejected["feedback"]
        assert path.read_bytes() == before
        accepted = feedback(json.dumps({"proof": "trivial"}))
        assert accepted["accepted"] is True
        assert path.read_bytes() == before
        return {
            "status": "candidate",
            "api_calls": 2,
            "final_response": json.dumps({"proof": "trivial"}),
        }

    state = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(job_api_calls=3, max_restarts=0),
        session=persistent,
        verifier=CandidateVerifier(),
    ).run()
    assert state["status"] == "completed"
    assert len(state["jobs"]) == 1
    assert state["metrics"]["api_calls"] == 2


def test_standard_prover_child_research_has_separate_bounded_accounting(tmp_path: Path) -> None:
    path = project(tmp_path)

    def auxiliary(**kwargs: Any) -> dict[str, Any]:
        if kwargs["role"] == "prover":
            report = kwargs["config"]["_research_job"](
                "Compute the parity of the first ten triangular numbers."
            )
            assert report["status"] == "complete"
            assert report["report"] == "Recorded parity table."
            return session(**kwargs)
        assert kwargs["role"] == "research"
        assert "_research_job" not in kwargs["config"]
        assert kwargs["api_budget"] == 2
        return {"status": "complete", "api_calls": 1, "final_response": "Recorded parity table."}

    state = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(job_api_calls=2, orchestrator_api_calls=2, total_api_calls=4),
        session=auxiliary,
        verifier=Verifier(),
    ).run()
    assert state["status"] == "completed"
    assert state["metrics"]["api_calls"] == 2
    assert state["metrics"]["reserved_api_calls"] == 0
    assert state["jobs"][1]["parent_job_id"] == state["jobs"][0]["id"]


def test_reviewed_direction_changes_charge_once_but_splits_do_not(tmp_path: Path) -> None:
    path = project(tmp_path)
    change_kind = "direction"

    def planning(**kwargs: Any) -> dict[str, Any]:
        if kwargs["role"] == "review":
            response = {"accepted": True, "change_kind": change_kind}
        else:
            response = {
                "plan": "Continue the concrete outline.",
                "nodes": [],
                "change_kind": change_kind,
            }
        return {"status": "complete", "api_calls": 1, "final_response": json.dumps(response)}

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="research", plan_refinements=1),
        session=planning,
        verifier=Verifier(),
    )
    affected = {runtime.dag.roots[0]}
    assert runtime._research_plan("initial")
    assert runtime.state["metrics"]["plan_refinements"] == 0
    assert runtime._research_plan("changed mathematical direction", affected=affected)
    assert runtime.state["metrics"]["plan_refinements"] == 1
    assert not runtime._research_plan("another direction", affected=affected)
    change_kind = "decomposition"
    assert runtime._research_plan("split the same strategy", affected=affected)
    assert runtime.state["metrics"]["plan_refinements"] == 1


def test_replanning_retires_unused_unresolved_helper_and_its_import(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.planning_controller import materialize

    path = project(tmp_path)
    before = path.read_bytes()
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=session, verifier=Verifier()
    )
    root = runtime.dag.nodes[0]
    expanded, skeletons = apply_proposal(
        runtime.dag,
        {
            "nodes": [
                {
                    "id": "helper",
                    "name": "help",
                    "statement": "theorem help : True ∧ True := by sorry",
                    "file": "LeanFlowProofs/Help.lean",
                },
                {"id": root.id, "dependencies": ["helper"]},
            ]
        },
        max_nodes=10,
    )
    materialize(runtime, expanded, skeletons)
    runtime.dag = expanded
    assert b"import LeanFlowProofs.Help" in path.read_bytes()
    revised, skeletons = apply_proposal(
        expanded, {"nodes": [{"id": root.id, "dependencies": []}]}, max_nodes=10
    )
    materialize(runtime, revised, skeletons)
    runtime.dag = revised
    assert path.read_bytes() == before
    assert not (tmp_path / "LeanFlowProofs/Help.lean").exists()
    assert "helper" not in revised.by_id()
    runtime._assert_sources()


def test_interrupted_proof_commit_recovers_candidate_without_new_model_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project(tmp_path)
    before = path.read_bytes()
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=session, verifier=Verifier()
    )
    node = runtime.dag.nodes[0]

    def crash() -> None:
        raise SystemExit("simulated crash after source replacement")

    monkeypatch.setattr(runtime, "_persist", crash)
    with pytest.raises(SystemExit, match="simulated"):
        runtime._accept(node, ["trivial"], "controller")
    assert path.read_bytes() != before
    assert (runtime.store.directory / "source-transaction.json").is_file()

    def forbidden(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("recovered verified candidate does not need a model request")

    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        session=forbidden,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    assert path.read_bytes() == before
    assert resumed.dag.nodes[0].status == "candidate"
    state = resumed.run()
    assert state["status"] == "completed"
    assert state["metrics"]["api_calls"] == 0
    assert not (runtime.store.directory / "source-transaction.json").exists()


def test_interrupted_proof_journal_refuses_untracked_external_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=session, verifier=Verifier()
    )
    monkeypatch.setattr(runtime, "_persist", lambda: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        runtime._accept(runtime.dag.nodes[0], ["trivial"], "controller")
    path.write_text("-- human edit after interrupted commit\n" + path.read_text())
    edited = path.read_bytes()
    with pytest.raises(SourceConflictError, match="source changed during interrupted"):
        ProverRuntime(
            root=tmp_path,
            targets=[path],
            config=ProverConfig(),
            session=session,
            verifier=Verifier(),
            run_id=runtime.run_id,
            resume=True,
        )
    assert path.read_bytes() == edited


def test_helper_compilation_pause_retains_checked_candidate_without_model_retry(
    tmp_path: Path,
) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=session, verifier=Verifier()
    )
    node = runtime.dag.nodes[0]
    runtime.documents[node.file].generated = True
    runtime._persist()

    class FailingBuild(Verifier):
        def compile_module(self, relative: str) -> dict[str, Any]:
            return {"accepted": False, "error": "temporary build environment unavailable"}

    runtime.verifier = FailingBuild()
    state = runtime.run()
    assert state["status"] == "environment_error"
    assert state["dag"]["nodes"][0]["status"] == "candidate"
    assert state["dag"]["nodes"][0]["candidate"] == ["trivial"]
    assert path.read_text().endswith("by sorry\n")

    def no_new_model(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("integration retry should reuse the existing candidate")

    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        session=no_new_model,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    result = resumed.run()
    assert result["status"] == "completed"
    assert result["metrics"]["api_calls"] == 1
    assert len(result["jobs"]) == 1


def test_session_closes_exact_private_submission_checker_workspace(tmp_path: Path) -> None:
    path = project(tmp_path)

    class ClosingVerifier(Verifier):
        def __init__(self) -> None:
            super().__init__()
            self.closed: list[Path] = []

        def close(self, workspace: Path) -> None:
            self.closed.append(workspace)

    verifier = ClosingVerifier()
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), session=session, verifier=verifier
    )
    state = runtime.run()
    assert state["status"] == "completed"
    assert verifier.closed == [
        runtime.store.directory / "checks" / state["jobs"][0]["id"],
        runtime.store.directory / "checks",
    ]


def test_negation_provider_failure_resumes_same_job_and_decomposition_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover.negation import NegationTask
    from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

    path = project(tmp_path)
    attempts = 0

    def negation_task(root: Path, node: Node) -> NegationTask:
        return NegationTask(
            node=Node(
                "negative",
                "negative",
                "theorem negative : False",
                node.file,
                node.module,
                holes=[0],
            ),
            source="import Std\ntheorem negative : False := by sorry\n",
        )

    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.negation_job.prepare_negation", negation_task
    )

    def provider(**kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        assert kwargs["role"] == "negation"
        assert kwargs["api_budget"] == 3
        assert kwargs["context"]["assignment"]["name"] == "negative"
        workspace = Path(kwargs["workspace"])
        if attempts == 1:
            Path(kwargs["context"]["scratch_file"]).write_text(
                "import Std\ntheorem negative : False := by\n  have partial : True := by trivial\n  sorry\n"
            )
            ledger = workspace.parent / ".runtime" / workspace.name / "request-count.json"
            ledger.write_text(json.dumps({"used": 1, "limit": 3}))
            return {"status": "provider_error", "api_calls": 1, "error": "temporary outage"}
        assert "have partial" in Path(kwargs["context"]["scratch_file"]).read_text()
        return {
            "status": "budget_exhausted",
            "api_calls": 3,
            "final_response": json.dumps({"notes": "no counterexample"}),
        }

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="research", job_api_calls=3),
        session=provider,
        verifier=Verifier(),
    )
    node = runtime.dag.nodes[0]
    node.status, node.attempts = "blocked", 4
    with pytest.raises(InfrastructureFailure, match="temporary outage"):
        runtime._recover(node)
    runtime._persist()
    assert node.status == "blocked"
    assert node.decompositions == 1
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        session=provider,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
    )
    monkeypatch.setattr(resumed, "_research_plan", lambda *args, **kwargs: False)
    state = resumed.run()
    assert state["status"] == "budget_exhausted"
    assert len(state["jobs"]) == 1
    assert state["jobs"][0]["resumed"] is True
    assert state["metrics"]["api_calls"] == 3
    assert state["metrics"]["decompositions"] == 1
    assert state["dag"]["nodes"][0]["attempts"] == 4
