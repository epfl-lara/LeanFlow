"""Pin formalization controller lifecycle, source preservation and native isolation."""

import json

import pytest

from agent.runtime.harness import HarnessResult
from agent.runtime.harness.events import usage_delta
from agent.runtime.harness.models import NativeLaunchFailure
from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization.preparation_campaign import (
    admit_author,
    campaign_budget,
    campaign_lock,
    select_campaign,
)
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    load_document,
)
from leanflow_cli.native import harness_workflow as runner
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.runtime.project_operations import ProjectBusyError, project_setup


@pytest.fixture(autouse=True)
def local_dependency_fingerprints(monkeypatch, tmp_path):
    """Keep unit-test generations independent of the host's installed toolchain."""
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.lean_environment._toolchain_roots", lambda project: []
    )
    monkeypatch.delenv("LEAN_PATH", raising=False)
    paper = tmp_path / "paper.tex"
    paper.write_text(r"\begin{theorem}True holds.\end{theorem}")
    record = create_document(tmp_path, paper)
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_ID", record["document_id"])
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_REVISION", record["revision_id"])
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.preflight_dependencies", lambda *args: {}
    )
    monkeypatch.setattr(
        "leanflow_cli.formalization.proposal.check_proposal",
        lambda **kwargs: {"accepted": False, "error": "Invalid source translation"},
    )


def _environment(root, **overrides):
    """Select the actual frozen fixture without rerunning intake during resume."""
    import os

    return {
        "LEANFLOW_PROJECT_ROOT": str(root),
        **{
            name: os.environ[name]
            for name in (
                "LEANFLOW_NATIVE_WORKFLOW_KIND",
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID",
                "LEANFLOW_FORMALIZATION_REVISION",
            )
        },
        **overrides,
    }


def test_formalization_uses_selected_native_runtime_with_unlimited_defaults(tmp_path, monkeypatch):
    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")
    seen = []

    def invoke(request, on_event=None):
        seen.append(request)
        request.on_started()
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Missing source theorem"}',
            session_id="native-session",
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    result = runner.run_workflow(
        {
            **_environment(tmp_path),
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_NATIVE_PROVIDER": "claude",
        }
    )
    assert result["status"] == "blocked"
    assert len(seen) == 1 and seen[0].backend == "claude"
    assert not seen[0].restricted_tools
    assert seen[0].deadline is None and seen[0].model == seen[0].effort == ""
    assert seen[0].cwd != tmp_path


def test_preflight_only_retry_admits_first_author_without_resetting_usage(tmp_path, monkeypatch):
    """Resume a bound failed preflight without inventing an earlier author checkpoint."""
    record = load_document(tmp_path, _environment(tmp_path)["LEANFLOW_FORMALIZATION_DOCUMENT_ID"])
    run_id = "preflight-only"
    directory = tmp_path / ".leanflow/native-workflows" / run_id
    with campaign_lock(tmp_path, run_id):
        binding = select_campaign(
            tmp_path,
            record,
            run_id=run_id,
            limits={"agent_calls": 3},
            backend="codex",
            selection_token=current_selection_token(tmp_path, record["document_id"]),
            source_draft_only=False,
            effective_draft=False,
            resume=False,
        )
        budget_path = campaign_budget(tmp_path, binding)
        budget = CampaignLimits(budget_path, binding["limits"])
        call = budget.reserve()
        budget.started_call(call)
        budget.finish(call, {"input_tokens": 11, "output_tokens": 7}, started=True)
    assert not (directory / "state.json").exists()
    seen = []

    def invoke(request, on_event=None):
        seen.append(request)
        request.on_started()
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Fixture"}',
            session_id="first-author",
            usage={"input_tokens": 2, "output_tokens": 1},
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "leanflow_cli.native.harness_context.load_skill", lambda *args, **kwargs: None
    )
    result = runner.run_workflow(
        {**_environment(tmp_path), "LEANFLOW_NATIVE_RESUME_RUN_ID": run_id}
    )
    assert result["status"] == "blocked" and len(seen) == 1
    assert not seen[0].session_id
    state = json.loads((directory / "state.json").read_text())
    assert state["campaign_binding_id"] == binding["binding_id"]
    ledger = json.loads(budget_path.read_text())
    assert len(ledger["invocations"]) == 2
    assert ledger["invocations"][call]["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert not (directory / "limits.json").exists()


def test_missing_genuine_author_state_cannot_become_first_author_retry(tmp_path, monkeypatch):
    record = load_document(tmp_path, _environment(tmp_path)["LEANFLOW_FORMALIZATION_DOCUMENT_ID"])
    directory = tmp_path / ".leanflow/native-workflows/lost-author"
    with campaign_lock(tmp_path, "lost-author"):
        binding = select_campaign(
            tmp_path,
            record,
            run_id="lost-author",
            limits={},
            backend="codex",
            selection_token=current_selection_token(tmp_path, record["document_id"]),
            source_draft_only=False,
            effective_draft=False,
            resume=False,
        )
        admit_author(tmp_path, binding, resume=False, state_path=directory / "state.json")
    monkeypatch.setattr(runner, "run_harness", lambda *args, **kwargs: pytest.fail("New author"))
    with pytest.raises(ValueError, match="author state is missing"):
        runner.run_workflow(
            {**_environment(tmp_path), "LEANFLOW_NATIVE_RESUME_RUN_ID": "lost-author"}
        )
    assert not (directory / "state.json").exists()


def test_setup_ownership_rejects_native_workflow_before_project_writes(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(
        runner, "_run_controller", lambda *a, **kw: pytest.fail("Controller started")
    )
    with project_setup(root):
        with pytest.raises(ProjectBusyError, match="setup"):
            runner.run_workflow({**_environment(tmp_path), "LEANFLOW_PROJECT_ROOT": str(root)})
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
    assert runner.run_workflow({**_environment(tmp_path), "LEANFLOW_PROJECT_ROOT": str(root)}) == {
        "status": "completed"
    }
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
        return HarnessResult(
            status="completed", final_response="done", session_id="s", cleanup_verified=True
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    result = runner.run_workflow(
        {
            **_environment(tmp_path),
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_NATIVE_PROVIDER": "codex",
            "LEANFLOW_PROVER_AGENT_CALLS": "1",
        }
    )
    assert result["status"] == "limit_reached"
    assert source.read_bytes() == before


def test_honest_blocker_ends_unlimited_workflow(tmp_path, monkeypatch):
    (tmp_path / "Main.lean").write_text("theorem p : True := by sorry\n")
    calls = []

    def invoke(request, on_event=None):
        calls.append(request)
        request.on_started()
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Missing source theorem"}',
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    result = runner.run_workflow(
        {
            **_environment(tmp_path),
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
        }
    )
    assert result["status"] == "blocked"
    assert result["error"] == "Missing source theorem" and len(calls) == 1


def test_native_session_is_persisted_before_the_invocation_returns(tmp_path, monkeypatch):
    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")

    def invoke(request, on_event=None):
        request.on_started()
        on_event({"type": "thread.started", "thread_id": "recoverable-session"})
        path = next((tmp_path / ".leanflow/native-workflows").glob("*/state.json"))
        assert json.loads(path.read_text())["session_id"] == "recoverable-session"
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Missing source theorem"}',
            session_id="recoverable-session",
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    assert (
        runner.run_workflow({**_environment(tmp_path), "LEANFLOW_PROJECT_ROOT": str(tmp_path)})[
            "status"
        ]
        == "blocked"
    )


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
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Missing source theorem"}',
            cleanup_verified=True,
        )

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
            **_environment(tmp_path),
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_NATIVE_PROVIDER": backend,
            setting: value,
        }
    )
    assert result["status"] == "blocked"
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
    record = load_document(tmp_path, _environment(tmp_path)["LEANFLOW_FORMALIZATION_DOCUMENT_ID"])
    with campaign_lock(tmp_path, "old-run"):
        binding = select_campaign(
            tmp_path,
            record,
            run_id="old-run",
            limits={},
            backend="codex",
            selection_token=current_selection_token(tmp_path, record["document_id"]),
            source_draft_only=False,
            effective_draft=False,
            resume=False,
            budget_path=directory / "limits.json",
        )
        admit_author(tmp_path, binding, resume=False, state_path=directory / "state.json")
    state = {
        "version": 2,
        "run_id": "old-run",
        "campaign_binding_id": binding["binding_id"],
        "backend": "codex",
        "workflow": "formalize",
        "baseline": {},
    }
    if saved_policy is not None:
        state["research_policy"] = saved_policy
    path = directory / "state.json"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    monkeypatch.setattr(runner, "run_harness", lambda *a, **kw: pytest.fail("Unsafe native launch"))
    with pytest.raises(ValueError, match="earlier online or unclassified context"):
        runner.run_workflow(
            {
                **_environment(tmp_path),
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
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Missing source theorem"}',
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(tmp_path),
        "LEANFLOW_WORKFLOW_RUN_ID": "clean-run",
        **_environment(tmp_path),
        "LEANFLOW_DISABLE_SOLUTION_RESEARCH": "1",
    }
    first = runner.run_workflow(environment)
    assert first["status"] == "blocked"
    if not repeat_restriction:
        environment.pop("LEANFLOW_DISABLE_SOLUTION_RESEARCH")
    resumed = runner.run_workflow(
        {**_environment(tmp_path), **environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "clean-run"}
    )
    assert resumed["status"] == "blocked"
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
                    **_environment(tmp_path),
                    "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                    "LEANFLOW_NATIVE_RESUME_RUN_ID": "resume-me",
                }
            )


def test_formalization_prompt_keeps_nested_snapshot_paths_exact(tmp_path):
    from leanflow_cli.native.harness_context import prepare_context

    snapshot = tmp_path / ".leanflow/native-workflows/run/project"
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(parents=True)
    extracted = tmp_path / ".leanflow/source.txt"
    extracted.write_text("A mathematical statement.")
    context_file = tmp_path / ".leanflow/context.md"
    context_file.write_text(f"Read {extracted}")
    context, prompt = prepare_context(
        root=tmp_path,
        snapshot=snapshot,
        scratch=scratch,
        environment={
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
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
        return HarnessResult(
            status="completed",
            final_response='{"status":"blocked","reason":"Missing source theorem"}',
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner.signal, "signal", set_handler)
    monkeypatch.setattr(runner, "NativeObserver", FailingObserver)
    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.delenv("LEANFLOW_NATIVE_RESUME_RUN_ID", raising=False)
    monkeypatch.delenv("LEANFLOW_WORKFLOW_RUN_ID", raising=False)
    with pytest.raises(OSError, match="Status storage unavailable"):
        runner.run_workflow()
    assert handlers == original_handlers


@pytest.mark.parametrize("kind", ["", "prove", "draft", "review", "refactor", "golf", "unknown"])
def test_retired_native_workflows_are_rejected_before_any_controller_effect(
    tmp_path, monkeypatch, kind
):
    root = tmp_path / "untouched"
    root.mkdir()
    monkeypatch.setattr(
        runner, "project_activity", lambda *args: pytest.fail("Project authority was acquired")
    )
    monkeypatch.setattr(
        runner, "run_harness", lambda *args, **kwargs: pytest.fail("Native agent started")
    )
    with pytest.raises(ValueError, match="accepts only formalize"):
        runner.run_workflow(
            {"LEANFLOW_PROJECT_ROOT": str(root), "LEANFLOW_NATIVE_WORKFLOW_KIND": kind}
        )
    assert list(root.iterdir()) == []


def test_formalization_source_capture_retains_math_and_configuration_only(tmp_path):
    root = tmp_path / "canonical"
    root.mkdir()
    expected = {
        "Main.lean": b"import Init\n",
        "lakefile.toml": b'name = "example"\n',
        "lean-toolchain": b"leanprover/lean4:v4.33.1",
        "lake-manifest.json": b"{}",
        "Blueprint.md": b"# Source notes\n",
    }
    for name, data in expected.items():
        (root / name).write_bytes(data)
    for skipped in (".lake", ".git", ".leanflow", ".leanflow-job", ".venv", "node_modules"):
        (root / skipped).mkdir()
        (root / skipped / "Unowned.lean").write_text("axiom forged : False")
    (root / "paper.tex").write_text("Frozen separately as source evidence.")
    assert source_files(root) == expected


def test_formalization_source_capture_refuses_a_source_alias(tmp_path):
    original = tmp_path / "original.lean"
    original.write_text("theorem p : True := by trivial")
    root = tmp_path / "canonical"
    root.mkdir()
    (root / "Main.lean").symlink_to(original)
    with pytest.raises(ValueError, match="file symlinks"):
        source_files(root)
    assert original.read_text() == "theorem p : True := by trivial"


def test_hard_kill_during_independent_check_does_not_leave_a_false_native_live_marker(
    tmp_path, monkeypatch
):
    """Persist verified author/tool closure before a crash in later controller-only checks."""
    import multiprocessing
    import os

    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")
    (tmp_path / "lake-manifest.json").write_text('{"packages":[]}')
    (tmp_path / ".lake/packages").mkdir(parents=True)
    environment = _environment(tmp_path, LEANFLOW_WORKFLOW_RUN_ID="post-author-crash")
    state_path = tmp_path / ".leanflow/native-workflows/post-author-crash/state.json"
    observed = tmp_path / "check-entry.json"

    def invoke(request, on_event=None):
        assert json.loads(state_path.read_text())["native_cleanup_unverified"] is True
        request.on_started()
        return HarnessResult("completed", "{}", "verified-closed", cleanup_verified=True)

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)

    def killed_controller():
        def check_then_kill(**kwargs):
            observed.write_bytes(state_path.read_bytes())
            os._exit(91)

        from leanflow_cli.formalization import proposal

        proposal.check_proposal = check_then_kill
        runner.run_workflow(environment)

    child = multiprocessing.get_context("fork").Process(target=killed_controller)
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
        child.join(2)
        pytest.fail("Controller failed to reach bounded post-author check")
    assert child.exitcode == 91
    checkpoint = json.loads(observed.read_text())
    assert checkpoint["native_process_cleanup_verified"] is True
    assert checkpoint["native_cleanup_unverified"] is False

    def resumed(request, on_event=None):
        request.on_started()
        return HarnessResult(
            "completed",
            '{"status":"blocked","reason":"Await source repair"}',
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", resumed)
    result = runner.run_workflow(
        {**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "post-author-crash"}
    )
    assert result["status"] == "blocked", result.get("error")
    assert not result["native_cleanup_unverified"]


@pytest.mark.parametrize("verified_no_launch", [False, True])
def test_prelaunch_failure_only_releases_resume_guard_with_explicit_closure_evidence(
    tmp_path, monkeypatch, verified_no_launch
):
    """Distinguish proven no-launch failures from exceptions that may hide live helpers."""
    from agent.runtime.harness.models import BackendError, NativeLaunchFailure

    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")
    (tmp_path / "lake-manifest.json").write_text('{"packages":[]}')
    (tmp_path / ".lake/packages").mkdir(parents=True)
    environment = _environment(tmp_path, LEANFLOW_WORKFLOW_RUN_ID="prelaunch-failure")
    directory = tmp_path / ".leanflow/native-workflows/prelaunch-failure"
    calls = []

    def invoke(request, on_event=None):
        calls.append(request)
        assert json.loads((directory / "state.json").read_text())["native_cleanup_unverified"]
        (request.cwd / "retained.txt").write_text("Private invocation input")
        if len(calls) == 1:
            if verified_no_launch:
                raise NativeLaunchFailure("Executable failed before author launch")
            raise BackendError("Preflight helper closure could not be established")
        request.on_started()
        return HarnessResult(
            "completed",
            '{"status":"blocked","reason":"Source repair needed"}',
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *a, **kw: object())
    monkeypatch.setattr("leanflow_cli.native.harness_context.load_skill", lambda *a, **kw: None)
    result = runner.run_workflow(environment)
    assert result["native_cleanup_unverified"] is not verified_no_launch
    assert result["status"] == ("startup_error" if verified_no_launch else "error")
    invocations = json.loads((directory / "limits.json").read_text())["invocations"]
    assert len(invocations) == 1
    assert next(iter(invocations.values()))["status"] == "not_started"
    resume = {**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "prelaunch-failure"}
    if verified_no_launch:
        retried = runner.run_workflow(resume)
        assert retried["status"] == "blocked" and len(calls) == 2
    else:
        with pytest.raises(ValueError, match="closure is unverified"):
            runner.run_workflow(resume)
        assert len(calls) == 1
    assert (directory / "project/retained.txt").read_text() == "Private invocation input"


@pytest.mark.parametrize("failure", ["exception", "exception_with_session", "returned"])
def test_verified_continuation_launch_failure_preserves_native_identity_and_capped_usage(
    tmp_path, monkeypatch, failure
):
    """Keep a known cumulative baseline through a no-launch attempt and charge only paid deltas."""
    (tmp_path / "Main.lean").write_text("theorem p : True := by trivial\n")
    source_before = source_files(tmp_path)
    paper_before = (tmp_path / "paper.tex").read_bytes()
    environment = _environment(
        tmp_path,
        LEANFLOW_WORKFLOW_RUN_ID="continuation-launch-failure",
        LEANFLOW_NATIVE_PROVIDER="codex",
        LEANFLOW_PROVER_AGENT_CALLS="3",
        LEANFLOW_PROVER_TOKENS="20",
    )
    directory = tmp_path / ".leanflow/native-workflows/continuation-launch-failure"
    calls = []
    first_total = {"input_tokens": 2, "output_tokens": 1}
    final_total = {"input_tokens": 5, "output_tokens": 3}

    def invoke(request, on_event=None):
        calls.append(request)
        assert request.session_id == ("" if len(calls) == 1 else "known-native-session")
        assert request.previous_usage == (None if len(calls) == 1 else first_total)
        if len(calls) == 2:
            if failure.startswith("exception"):
                raise NativeLaunchFailure(
                    "Verified helper failure before author launch",
                    session_id=request.session_id if failure == "exception_with_session" else "",
                )
            return HarnessResult(
                "startup_error", error="Native executable is missing", cleanup_verified=True
            )
        request.on_started()
        total = first_total if len(calls) == 1 else final_total
        usage = usage_delta(
            total, request.previous_usage, resumed=bool(request.session_id), cumulative=True
        )
        return HarnessResult(
            "completed",
            '{"status":"blocked","reason":"The source interpretation requires clarification"}',
            session_id="known-native-session",
            usage=usage,
            usage_total=total,
            cleanup_verified=True,
        )

    monkeypatch.setattr(runner, "run_harness", invoke)
    monkeypatch.setattr(runner, "build_runtime_isolation", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "leanflow_cli.native.harness_context.load_skill", lambda *args, **kwargs: None
    )
    initial = runner.run_workflow(environment)
    assert initial["status"] == "blocked" and initial["usage_total"] == first_total
    resume = {**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "continuation-launch-failure"}
    failed = runner.run_workflow(resume)
    assert failed["status"] == "startup_error" and not failed["native_cleanup_unverified"]
    assert failed["session_id"] == "known-native-session" and failed["usage_total"] == first_total
    saved = json.loads((directory / "state.json").read_text())
    assert saved["session_id"] == "known-native-session" and saved["usage_total"] == first_total
    invocations = list(json.loads((directory / "limits.json").read_text())["invocations"].values())
    assert len(invocations) == 2
    assert [row["usage"] for row in invocations if row["status"] == "finished"] == [first_total]
    assert [row["usage"] for row in invocations if row["status"] == "not_started"] == [None]
    completed = runner.run_workflow(resume)
    assert completed["status"] == "blocked" and len(calls) == 3
    assert (
        completed["session_id"] == "known-native-session"
        and completed["usage_total"] == final_total
    )
    invocations = list(json.loads((directory / "limits.json").read_text())["invocations"].values())
    charged = [row for row in invocations if row["status"] == "finished"]
    assert len(invocations) == 3 and len(charged) == 2
    assert all(row["usage"] is not None for row in charged)
    assert sum(row["usage"]["input_tokens"] for row in charged) == 5
    assert sum(row["usage"]["output_tokens"] for row in charged) == 3
    assert {"input_tokens": 3, "output_tokens": 2} in [row["usage"] for row in charged]
    budget = CampaignLimits(directory / "limits.json", {"agent_calls": 3, "tokens": 20})
    next_invocation = budget.reserve()
    budget.finish(next_invocation, None, started=False)
    assert (
        source_files(tmp_path) == source_before
        and (tmp_path / "paper.tex").read_bytes() == paper_before
    )
