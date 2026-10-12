"""Keep native workflow publication tied to verified, unchanged source snapshots."""

import hashlib
import json

import pytest

from agent.runtime.harness import HarnessResult
from leanflow_cli.native import harness_artifacts as artifacts
from leanflow_cli.native import harness_workflow as runner
from leanflow_cli.runtime.project_operations import ProjectBusyError, project_setup


@pytest.fixture(autouse=True)
def local_dependency_fingerprints(monkeypatch):
    """Keep unit-test generations independent of the host's installed toolchain."""
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment._toolchain_roots", lambda project: []
    )
    monkeypatch.delenv("LEAN_PATH", raising=False)


def test_publication_rejects_canonical_concurrent_changes(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem p : True := by trivial\n")
    baseline = artifacts.source_files(tmp_path)
    proposal = "theorem p : True := by exact True.intro\n"
    receipt = {
        "accepted": True,
        "environment_generation": artifacts.lean_environment_generation(tmp_path),
        "files": {"Main.lean": proposal},
        "sha256": {"Main.lean": hashlib.sha256(proposal.encode()).hexdigest()},
    }
    source.write_text("-- user edit\n" + source.read_text())
    with pytest.raises(ValueError, match="changed"):
        artifacts.promote(tmp_path, baseline, receipt)
    assert source.read_text().startswith("-- user edit")


def test_publication_uses_verified_bytes_and_rejects_changed_receipt(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem p : True := by trivial\n")
    baseline = artifacts.source_files(tmp_path)
    receipt = {
        "accepted": True,
        "environment_generation": artifacts.lean_environment_generation(tmp_path),
        "files": {"Main.lean": "malicious"},
        "sha256": {"Main.lean": "bad"},
    }
    with pytest.raises(ValueError, match="digest"):
        artifacts.promote(tmp_path, baseline, receipt)
    assert source.read_bytes() == baseline["Main.lean"]


def test_review_uses_selected_native_runtime_with_unlimited_defaults(tmp_path, monkeypatch):
    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")
    seen = []

    def invoke(request, on_event=None):
        seen.append(request)
        request.on_started()
        return HarnessResult(
            status="completed", final_response="Reviewed.", session_id="native-session"
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    result = runner.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "review",
            "LEANFLOW_NATIVE_PROVIDER": "claude",
        }
    )
    assert result["status"] == "completed"
    assert len(seen) == 1 and seen[0].backend == "claude"
    assert not seen[0].restricted_tools
    assert seen[0].deadline is None and seen[0].model == seen[0].effort == ""
    assert seen[0].cwd != tmp_path


def test_setup_ownership_rejects_native_workflow_before_project_writes(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(
        runner, "_run_controller", lambda *a, **kw: pytest.fail("Controller started")
    )
    with project_setup(root):
        with pytest.raises(ProjectBusyError, match="setup"):
            runner.run_workflow({"LEANFLOW_PROJECT_ROOT": str(root)})
    assert not (root / ".leanflow").exists()


def test_native_workflow_excludes_setup_for_entire_controller(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()

    def controller(*args, **kwargs):
        with pytest.raises(ProjectBusyError, match="running"):
            with project_setup(root):
                pytest.fail("Setup entered during a native workflow")
        return {"status": "completed"}

    monkeypatch.setattr(runner, "_run_controller", controller)
    assert runner.run_workflow({"LEANFLOW_PROJECT_ROOT": str(root)}) == {"status": "completed"}
    with project_setup(root):
        pass


def test_failed_proposal_never_touches_canonical_source_and_obeys_global_calls(
    tmp_path, monkeypatch
):
    source = tmp_path / "Main.lean"
    source.write_text("theorem p : True := by trivial\n")
    before = source.read_bytes()

    def invoke(request, on_event=None):
        request.on_started()
        (request.cwd / "Main.lean").write_text("theorem p : True := by sorry\n")
        return HarnessResult(status="completed", final_response="done", session_id="s")

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    result = runner.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "refactor",
            "LEANFLOW_NATIVE_PROVIDER": "codex",
            "LEANFLOW_PROVER_AGENT_CALLS": "1",
        }
    )
    assert result["status"] == "limit_reached"
    assert source.read_bytes() == before


@pytest.mark.parametrize("dependency_changes", [False, True])
def test_verification_never_imports_agent_dependency_artifacts(
    tmp_path, monkeypatch, dependency_changes
):
    root = tmp_path / "canonical"
    root.mkdir()
    (root / "Main.lean").write_text("theorem p : True := by sorry\n")
    dependency = root / ".lake" / "packages" / "dep" / "Lib.olean"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"canonical dependency")
    baseline = artifacts.source_files(root)
    snapshot = artifacts.create_snapshot(root, tmp_path / "agent")
    (snapshot / "Main.lean").write_text("theorem p : True := by trivial\n")
    (snapshot / ".lake/packages/dep/Lib.olean").write_bytes(b"agent forged dependency")
    observed = []

    def build(**kwargs):
        observed.append((kwargs["project_root"] / ".lake/packages/dep/Lib.olean").read_bytes())
        assert kwargs["timeout_s"] is None
        if dependency_changes:
            dependency.write_bytes(b"externally updated dependency")
        return {"success": True}

    monkeypatch.setattr("leanflow_cli.workflows.prover.check_process.isolated_command", build)
    monkeypatch.setattr(
        artifacts,
        "compiled_type_profiles",
        lambda **kwargs: {
            "accepted": True,
            "profiles": {"p": {"sha256": "same statement", "axioms": []}},
        },
    )
    result = artifacts.verify_proposal(
        root=root,
        snapshot=snapshot,
        baseline=baseline,
        directory=tmp_path / "checks",
        workflow="refactor",
        active_file="Main.lean",
        allowed_axioms=(),
        deadline=None,
    )
    assert result["accepted"] is not dependency_changes, result
    if dependency_changes:
        assert "dependencies changed" in result["error"]
    assert observed == [b"canonical dependency"]
    assert (root / "Main.lean").read_bytes() == baseline["Main.lean"]


def test_changed_dependency_invalidates_an_accepted_publication_receipt(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem p : True := by sorry\n")
    dependency = tmp_path / ".lake/packages/dep/Lib.olean"
    dependency.parent.mkdir(parents=True)
    dependency.write_bytes(b"accepted library")
    baseline = artifacts.source_files(tmp_path)
    proposed = "theorem p : True := by trivial\n"
    receipt = {
        "accepted": True,
        "environment_generation": artifacts.lean_environment_generation(tmp_path),
        "files": {"Main.lean": proposed},
        "sha256": {"Main.lean": hashlib.sha256(proposed.encode()).hexdigest()},
    }
    dependency.write_bytes(b"modified library")
    with pytest.raises(ValueError, match="dependencies changed"):
        artifacts.promote(tmp_path, baseline, receipt)
    assert source.read_bytes() == baseline["Main.lean"]


def test_original_definition_value_cannot_change_behind_equal_statement_type(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = "def limit : Nat := 5\ntheorem p : limit = limit := by rfl\n"
    (root / "Main.lean").write_text(source)
    baseline = artifacts.source_files(root)
    snapshot = artifacts.create_snapshot(root, tmp_path / "agent")
    (snapshot / "Main.lean").write_text(source.replace(":= 5", ":= 6"))
    result = artifacts.verify_proposal(
        root=root,
        snapshot=snapshot,
        baseline=baseline,
        directory=tmp_path / "checks",
        workflow="refactor",
        active_file="Main.lean",
        allowed_axioms=(),
        deadline=None,
    )
    assert not result["accepted"] and "non-proof definition" in result["error"]


@pytest.mark.parametrize("deadline,cancelled", [(0.0, None), (None, lambda: True)])
def test_global_stop_prevents_otherwise_accepted_promotion(tmp_path, deadline, cancelled):
    source = tmp_path / "Main.lean"
    source.write_text("theorem p : True := by sorry\n")
    baseline = artifacts.source_files(tmp_path)
    text = "theorem p : True := by trivial\n"
    receipt = {
        "accepted": True,
        "environment_generation": artifacts.lean_environment_generation(tmp_path),
        "files": {"Main.lean": text},
        "sha256": {"Main.lean": hashlib.sha256(text.encode()).hexdigest()},
    }
    with pytest.raises(InterruptedError):
        artifacts.promote(tmp_path, baseline, receipt, deadline=deadline, cancelled=cancelled)
    assert source.read_bytes() == baseline["Main.lean"]


def test_honest_blocker_ends_unlimited_workflow(tmp_path, monkeypatch):
    (tmp_path / "Main.lean").write_text("theorem p : True := by sorry\n")
    calls = []

    def invoke(request, on_event=None):
        calls.append(request)
        request.on_started()
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Missing source theorem"}',
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    result = runner.run_workflow(
        {"LEANFLOW_PROJECT_ROOT": str(tmp_path), "LEANFLOW_NATIVE_WORKFLOW_KIND": "draft"}
    )
    assert result["status"] == "blocked"
    assert result["error"] == "Missing source theorem" and len(calls) == 1


def test_publication_conflict_never_rolls_back_a_concurrent_user_edit(tmp_path, monkeypatch):
    first, second = tmp_path / "A.lean", tmp_path / "B.lean"
    first.write_text("original A")
    second.write_text("original B")
    baseline = artifacts.source_files(tmp_path)
    proposed = {"A.lean": "accepted A", "B.lean": "accepted B"}
    receipt = {
        "accepted": True,
        "environment_generation": artifacts.lean_environment_generation(tmp_path),
        "files": proposed,
        "sha256": {
            name: hashlib.sha256(text.encode()).hexdigest() for name, text in proposed.items()
        },
    }
    replace = artifacts.os.replace

    def concurrent_replace(before, after):
        replace(before, after)
        if after == first:
            first.write_text("user A")
            second.write_text("user B")

    monkeypatch.setattr(artifacts.os, "replace", concurrent_replace)
    with pytest.raises(ValueError, match="changed before publication"):
        artifacts.promote(tmp_path, baseline, receipt)
    assert first.read_text() == "user A" and second.read_text() == "user B"


def test_native_session_is_persisted_before_the_invocation_returns(tmp_path, monkeypatch):
    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")

    def invoke(request, on_event=None):
        request.on_started()
        on_event({"type": "thread.started", "thread_id": "recoverable-session"})
        path = next((tmp_path / ".leanflow/native-workflows").glob("*/state.json"))
        assert json.loads(path.read_text())["session_id"] == "recoverable-session"
        return HarnessResult(
            status="completed", final_response="Reviewed", session_id="recoverable-session"
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    assert runner.run_workflow({"LEANFLOW_PROJECT_ROOT": str(tmp_path)})["status"] == "completed"


@pytest.mark.parametrize(
    "setting,value",
    [
        ("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1"),
        ("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "true"),
        ("LEANFLOW_PROVER_ALLOW_INTERNET", "false"),
    ],
)
@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_restrictions_route_native_workflows_through_local_tools(
    tmp_path, monkeypatch, setting, value, backend
):
    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")
    seen = []
    toolsets = []
    tool_factory = runner.SessionTools

    def tools(**kwargs):
        toolset = tool_factory(**kwargs)
        toolsets.append(toolset)
        return toolset

    def invoke(request, on_event=None):
        seen.append(request)
        request.on_started()
        return HarnessResult(status="completed", final_response="Reviewed.")

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "SessionTools", tools)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    monkeypatch.setattr("leanflow_cli.runtime.lemex_install.resolve_lemex_executable", lambda: None)
    monkeypatch.setattr(
        "leanflow_cli.runtime.lemex_install.managed_distribution_root", lambda: None
    )
    result = runner.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "review",
            "LEANFLOW_NATIVE_PROVIDER": backend,
            setting: value,
        }
    )
    assert result["status"] == "completed"
    assert result["research_policy"] == "restricted-v1"
    assert len(seen) == 1 and seen[0].backend == backend
    assert seen[0].restricted_tools
    assert seen[0].deadline is None and seen[0].model == seen[0].effort == ""
    assert seen[0].mcp_config.is_file()
    assert len(toolsets) == 1 and not toolsets[0].allow_internet
    assert not {"web_search", "fetch_resource"}.intersection(
        tool["function"]["name"] for tool in toolsets[0].schemas()
    )


@pytest.mark.parametrize("saved_policy", ["online-v1", None, "legacy-filtered"])
def test_clean_room_rejects_online_or_unclassified_checkpoints_before_launch(
    tmp_path, monkeypatch, saved_policy
):
    directory = tmp_path / ".leanflow/native-workflows/old-run"
    directory.mkdir(parents=True)
    state = {"version": 2, "backend": "codex", "workflow": "review", "baseline": {}}
    if saved_policy is not None:
        state["research_policy"] = saved_policy
    path = directory / "state.json"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    monkeypatch.setattr(runner, "run_harness", lambda *a, **kw: pytest.fail("Unsafe native launch"))
    with pytest.raises(ValueError, match="earlier online or unclassified context"):
        runner.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                "LEANFLOW_NATIVE_RESUME_RUN_ID": "old-run",
                "LEANFLOW_DISABLE_SOLUTION_RESEARCH": "1",
            }
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("repeat_restriction", [False, True])
def test_clean_room_checkpoint_resume_preserves_local_tools(
    tmp_path, monkeypatch, repeat_restriction
):
    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")
    calls = []

    def invoke(request, on_event=None):
        calls.append(request)
        request.on_started()
        return HarnessResult(status="completed", final_response="Reviewed.")

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(tmp_path),
        "LEANFLOW_WORKFLOW_RUN_ID": "clean-run",
        "LEANFLOW_DISABLE_SOLUTION_RESEARCH": "1",
    }
    first = runner.run_workflow(environment)
    assert first["status"] == "completed"
    if not repeat_restriction:
        environment.pop("LEANFLOW_DISABLE_SOLUTION_RESEARCH")
    resumed = runner.run_workflow({**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "clean-run"})
    assert resumed["status"] == "completed"
    assert resumed["research_policy"] == "restricted-v1"
    assert len(calls) == 2 and all(call.restricted_tools for call in calls)
    limits = json.loads((tmp_path / ".leanflow/native-workflows/clean-run/limits.json").read_text())
    assert len(limits["invocations"]) == 2


def test_concurrent_resume_cannot_double_spend_one_campaign(tmp_path):
    import fcntl

    directory = tmp_path / ".leanflow/native-workflows/resume-me"
    directory.mkdir(parents=True)
    with (directory / "controller.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="active controller"):
            runner.run_workflow(
                {
                    "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                    "LEANFLOW_NATIVE_RESUME_RUN_ID": "resume-me",
                }
            )


def test_generic_prompt_keeps_nested_snapshot_paths_exact(tmp_path):
    from leanflow_cli.native.harness_context import prepare_context

    snapshot = tmp_path / ".leanflow/native-workflows/run/project"
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(parents=True)
    extracted = tmp_path / ".leanflow/source.txt"
    extracted.write_text("A mathematical statement.")
    context_file = tmp_path / ".leanflow/context.md"
    context_file.write_text(f"Read {extracted}")
    context, prompt, _ = prepare_context(
        root=tmp_path,
        snapshot=snapshot,
        scratch=scratch,
        environment={
            "LEANFLOW_FORMALIZATION_CONTEXT": str(context_file),
            "LEANFLOW_FORMALIZATION_EXTRACTED_TEXT": str(extracted),
        },
        allowed=(),
    )
    assert context["project_root"] == str(snapshot)
    assert f'"project_root": "{snapshot}"' in prompt
    copied = scratch / "formalization-extracted_text.txt"
    assert f"Read {copied}" in prompt and copied.read_text() == "A mathematical statement."
    assert (scratch / "formalization-context.txt").read_text() == f"Read {copied}"
    assert str(snapshot) + "/.leanflow/native-workflows" not in prompt


def test_terminal_status_failure_restores_process_signal_handlers(tmp_path, monkeypatch):
    """Keep a failed status write from leaving cancellation handlers installed."""
    original_handlers = {runner.signal.SIGINT: object(), runner.signal.SIGTERM: object()}
    handlers = dict(original_handlers)

    def set_handler(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    class FailingObserver:
        def __init__(self, *args):
            pass

        def start(self, state):
            pass

        def publish(self, state, *args, **kwargs):
            if state.get("terminal"):
                raise OSError("Status storage unavailable")

        def event(self, *args):
            pass

    def invoke(request, on_event=None):
        request.on_started()
        return HarnessResult(status="completed", final_response="Reviewed")

    monkeypatch.setattr(runner.signal, "signal", set_handler)
    monkeypatch.setattr(runner, "NativeObserver", FailingObserver)
    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "review")
    monkeypatch.delenv("LEANFLOW_NATIVE_RESUME_RUN_ID", raising=False)
    monkeypatch.delenv("LEANFLOW_WORKFLOW_RUN_ID", raising=False)
    with pytest.raises(OSError, match="Status storage unavailable"):
        runner.run_workflow()
    assert handlers == original_handlers
