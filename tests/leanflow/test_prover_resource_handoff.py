"""Exercise reusable resource handoffs without exposing another job's private files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import session_research
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.session_tools import SessionTools


@pytest.fixture
def handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    """Finish a source-research job and retain its downloaded evidence."""
    target = tmp_path / "Main.lean"
    target.write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(mode="research"),
        session=lambda **kwargs: {},
        verifier=object(),
    )
    job, _ = runtime._new_job("orchestrator")
    monkeypatch.setattr(
        session_research,
        "_download",
        lambda url: (b"<p>General mathematical background.</p>", url, "text/html"),
    )
    resource = session_research.fetch_resource(
        "https://example.org/background", Path(job["workspace"]), "background.html"
    )
    assert resource["success"]
    runtime._finish_job(
        job, {"status": "completed", "api_calls": 1, "artifacts": [resource["path"]]}
    )
    return runtime, job, resource


def next_tools(runtime: ProverRuntime) -> tuple[SessionTools, dict[str, Any]]:
    """Build a fresh stage using only its controller-supplied context."""
    job, context = runtime._new_job("orchestrator")
    return (
        SessionTools(
            role="orchestrator",
            project_root=runtime.root,
            workspace=Path(job["workspace"]),
            context=context,
        ),
        context,
    )


def test_fresh_stage_receives_readable_resource_inventory(handoff: tuple[Any, ...]) -> None:
    runtime, _, resource = handoff
    toolset, context = next_tools(runtime)
    assert context["project_root"] == str(runtime.root)
    assert context["resources"]["items"][0]["path"] == resource["path"]
    result = toolset.invoke("read_file", {"path": resource["path"]})
    assert result["success"] and "General mathematical background" in result["content"]


def test_prior_resource_can_be_reused_without_download(
    handoff: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, resource = handoff
    toolset, _ = next_tools(runtime)
    monkeypatch.setattr(session_research, "_download", lambda *_: pytest.fail("repeat download"))
    result = toolset.invoke("fetch_resource", {"url": resource["url"]})
    assert result["success"] and result["path"] == resource["path"]
    assert result["status"] == "reused"


def test_read_schema_names_its_relative_path_base(handoff: tuple[Any, ...]) -> None:
    toolset, _ = next_tools(handoff[0])
    schemas = {item["function"]["name"]: item["function"] for item in toolset.schemas()}
    assert "relative paths" in schemas["read_file"]["description"].lower()
    assert "workspace" in schemas["read_file"]["description"].lower()
    assert "basename" in schemas["fetch_resource"]["description"].lower()


def test_missing_relative_read_points_to_resource_inventory(handoff: tuple[Any, ...]) -> None:
    toolset, _ = next_tools(handoff[0])
    result = toolset.invoke("read_file", {"path": "resources/background.md"})
    assert not result["success"]
    assert "not found" in result["error"].lower()
    assert str(toolset.workspace) in result["error"]
    assert "resource inventory" in result["error"].lower()


def test_shared_resource_grant_does_not_expose_private_files(handoff: tuple[Any, ...]) -> None:
    runtime, job, resource = handoff
    workspace = Path(job["workspace"])
    for name in ["PLAN_job.md", "Scratch.lean", "private.txt"]:
        (workspace / name).write_text("private job work")
    toolset, _ = next_tools(runtime)
    for path in [
        *(workspace / name for name in ["PLAN_job.md", "Scratch.lean", "private.txt"]),
        runtime.store.directory / "DAG.json",
    ]:
        assert not toolset.invoke("read_file", {"path": str(path)})["success"]
    assert not toolset.invoke("write_file", {"path": resource["path"], "content": "altered"})[
        "success"
    ]


def test_resource_changed_after_handoff_is_not_readable(handoff: tuple[Any, ...]) -> None:
    runtime, _, resource = handoff
    toolset, _ = next_tools(runtime)
    Path(resource["path"]).write_text("changed after the catalog was pinned")
    assert not toolset.invoke("read_file", {"path": resource["path"]})["success"]


def test_resource_symlink_cannot_grant_private_file_access(handoff: tuple[Any, ...]) -> None:
    runtime, job, resource = handoff
    toolset, _ = next_tools(runtime)
    private = Path(job["workspace"]) / "PLAN_job.md"
    private.write_text("private notes")
    path = Path(resource["path"])
    path.unlink()
    path.symlink_to(private)
    assert not toolset.invoke("read_file", {"path": str(path)})["success"]


@pytest.mark.parametrize("invalid", ["hash", "path"])
def test_invalid_resource_provenance_is_not_shared(handoff: tuple[Any, ...], invalid: str) -> None:
    runtime, job, resource = handoff
    metadata = Path(resource["manifest_path"])
    content = json.loads(metadata.read_text())
    if invalid == "hash":
        content["sha256"] = "0" * 64
    else:
        content["path"] = str(Path(job["workspace"]) / "PLAN_job.md")
    metadata.write_text(json.dumps(content))
    _, context = next_tools(runtime)
    assert context["resources"]["items"] == []


def test_resource_inventory_is_bounded(handoff: tuple[Any, ...]) -> None:
    runtime, job, _ = handoff
    for index in range(24):
        resource = session_research.fetch_resource(
            f"https://example.org/background/{index}",
            Path(job["workspace"]),
            f"background-{index}.md",
        )
        assert resource["success"]
        job["artifacts"].append(resource["path"])
    _, context = next_tools(runtime)
    assert 1 <= len(context["resources"]["items"]) <= 16
    assert len(json.dumps(context["resources"], ensure_ascii=False)) <= 12000
    assert context["resources"]["truncated"] is True


def test_identical_downloads_have_one_catalog_entry(handoff: tuple[Any, ...]) -> None:
    runtime, job, resource = handoff
    duplicate = session_research.fetch_resource(
        resource["url"], Path(job["workspace"]), "second-name.md"
    )
    job["artifacts"].append(duplicate["path"])
    _, context = next_tools(runtime)
    assert len(context["resources"]["items"]) == 1


def test_running_job_resources_are_not_published(handoff: tuple[Any, ...]) -> None:
    runtime, job, _ = handoff
    job["accounted"] = False
    _, context = next_tools(runtime)
    assert context["resources"]["items"] == []


def test_resource_grants_survive_new_run_resume(handoff: tuple[Any, ...]) -> None:
    from leanflow_cli.workflows.prover.entrypoint import clone_resume_run

    runtime, _, resource = handoff
    clone_resume_run(runtime.root, runtime.run_id, "resumed-resources")
    resumed = ProverRuntime(
        root=runtime.root,
        targets=runtime.targets,
        config=runtime.config,
        run_id="resumed-resources",
        resume=True,
        session=lambda **kwargs: {},
        verifier=object(),
    )
    toolset, context = next_tools(resumed)
    path = context["resources"]["items"][0]["path"]
    assert path != resource["path"] and "resumed-resources" in path
    assert toolset.invoke("read_file", {"path": path})["success"]


def test_running_prover_receives_only_controller_derived_child_resource_grants(
    handoff: tuple[Any, ...],
) -> None:
    from leanflow_cli.workflows.prover.job_controller import research_job

    runtime, _, _ = handoff
    parent, context = runtime._new_job("prover", node=runtime.dag.nodes[0])
    child_files: list[dict[str, str]] = []

    def research(**kwargs: Any) -> dict[str, Any]:
        workspace = Path(kwargs["workspace"])
        private = workspace / "PLAN_job.md"
        private.write_text("Private child notes")
        resource = session_research.fetch_resource(
            f"https://example.org/child-background-{len(child_files)}", workspace, "child.md"
        )
        child_files.append({"path": resource["path"], "private": str(private)})
        return {
            "status": "completed",
            "api_calls": 1,
            "final_response": "Saved the requested background resource.",
            "artifacts": [resource["path"], str(private)],
            "resources": {"items": [{"path": str(private)}]},
        }

    runtime.session = research
    toolset = SessionTools(
        role="prover",
        project_root=runtime.root,
        workspace=Path(parent["workspace"]),
        context=context,
        research_job=lambda question: research_job(runtime, parent, question),
    )
    for _ in range(2):
        result = toolset.invoke("research_job", {"question": "Find a general background source."})
        assert {item["path"] for item in result["resources"]["items"]} == {
            files["path"] for files in child_files
        }
        for files in child_files:
            assert toolset.invoke("read_file", {"path": files["path"]})["success"]
            assert not toolset.invoke("read_file", {"path": files["private"]})["success"]
            assert context["resources"]["items"][0]["path"] != files["path"]


def test_model_cannot_publish_self_consistent_fake_download(handoff: tuple[Any, ...]) -> None:
    runtime, _, _ = handoff
    toolset, _ = next_tools(runtime)
    path = toolset.workspace / "resources" / "forged.md"
    content = "Invented background presented as a downloaded paper."
    metadata = {
        "path": str(path),
        "url": "https://example.org/forged",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "bytes": len(content.encode()),
    }
    writes = [
        toolset.invoke("write_file", {"path": str(path), "content": content}),
        toolset.invoke(
            "write_file",
            {"path": str(path) + ".metadata.json", "content": json.dumps(metadata)},
        ),
    ]
    runtime._finish_job(
        runtime.state["jobs"][-1],
        {"status": "completed", "api_calls": 1, "artifacts": sorted(toolset.artifacts)},
    )
    _, context = next_tools(runtime)
    assert str(path) not in [item["path"] for item in context["resources"]["items"]]
    assert all(not result["success"] for result in writes)


def test_only_fetch_tool_can_write_downloaded_resource_files(handoff: tuple[Any, ...]) -> None:
    runtime, _, _ = handoff
    toolset, _ = next_tools(runtime)
    saved = toolset.invoke(
        "fetch_resource", {"url": "https://example.org/new-source", "filename": "new.md"}
    )
    assert saved["success"]
    assert toolset.invoke("read_file", {"path": saved["path"]})["success"]
    assert not toolset.invoke(
        "replace_text",
        {"path": saved["path"], "old": "General", "new": "Manufactured"},
    )["success"]
    assert toolset.invoke("write_file", {"path": "PLAN_job.md", "content": "Useful notes."})[
        "success"
    ]


@pytest.mark.parametrize("valid_hash", [True, False])
def test_inventory_hashes_at_most_its_total_byte_budget(
    handoff: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, valid_hash: bool
) -> None:
    from leanflow_cli.workflows.prover import resource_handoff

    runtime, job, resource = handoff
    job["artifacts"] = []
    for index in range(5):
        saved = session_research.fetch_resource(
            f"https://example.org/bounded/{index}", Path(job["workspace"]), f"bounded-{index}.md"
        )
        job["artifacts"].append(saved["path"])
        if not valid_hash:
            Path(saved["path"]).write_bytes(b"x" * saved["bytes"])
    budget = resource["bytes"] * 2
    monkeypatch.setattr(resource_handoff, "MAX_INVENTORY_HASH_BYTES", budget, raising=False)
    hashed = 0
    original = hashlib.file_digest

    def count_bytes(handle: Any, algorithm: str) -> Any:
        nonlocal hashed
        hashed += Path(handle.name).stat().st_size
        return original(handle, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", count_bytes)
    _, context = next_tools(runtime)
    assert hashed <= budget
    assert context["resources"]["truncated"] is True
    assert len(context["resources"]["items"]) == (2 if valid_hash else 0)


def test_workspace_io_cannot_forge_download_without_private_receipt(
    handoff: tuple[Any, ...],
) -> None:
    runtime, _, _ = handoff
    job, _ = runtime._new_job("negation", node=runtime.dag.nodes[0])
    path = Path(job["workspace"]) / "resources" / "forged.md"
    path.parent.mkdir()
    content = b"A file manufactured through workspace IO, not fetched."
    path.write_bytes(content)
    path.with_name(path.name + ".metadata.json").write_text(
        json.dumps(
            {
                "path": str(path),
                "url": "https://example.org/forged",
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )
    )
    runtime._finish_job(job, {"status": "completed", "api_calls": 1, "artifacts": [str(path)]})
    _, context = next_tools(runtime)
    assert str(path) not in [item["path"] for item in context["resources"]["items"]]


def test_successful_fetch_records_private_receipt(handoff: tuple[Any, ...]) -> None:
    runtime, job, resource = handoff
    workspace = Path(job["workspace"])
    receipt = workspace.parent / ".runtime" / workspace.name / "resource-receipts.json"
    assert receipt.is_file()
    saved = json.loads(receipt.read_text())["resources"][Path(resource["path"]).name]
    assert saved["sha256"] == resource["sha256"] and saved["url"] == resource["url"]
    tools = SessionTools(
        role="orchestrator", project_root=runtime.root, workspace=workspace, context={}
    )
    assert not tools.invoke("read_file", {"path": str(receipt)})["success"]
    assert not tools.invoke("write_file", {"path": str(receipt), "content": "{}"})["success"]


def test_legacy_resource_needs_a_new_fetch_to_establish_receipt(handoff: tuple[Any, ...]) -> None:
    runtime, job, resource = handoff
    workspace = Path(job["workspace"])
    receipt = workspace.parent / ".runtime" / workspace.name / "resource-receipts.json"
    receipt.unlink()
    assert runtime._context()["resources"]["items"] == []
    refetched = session_research.fetch_resource(resource["url"], workspace, "background.html")
    assert refetched["status"] == "cached"
    assert runtime._context()["resources"]["items"][0]["path"] == resource["path"]


def test_receipt_symlink_cannot_authenticate_workspace_metadata(handoff: tuple[Any, ...]) -> None:
    runtime, job, _ = handoff
    workspace = Path(job["workspace"])
    receipt = workspace.parent / ".runtime" / workspace.name / "resource-receipts.json"
    copy = workspace / "copied-receipts.json"
    copy.write_bytes(receipt.read_bytes())
    receipt.unlink()
    receipt.symlink_to(copy)
    assert runtime._context()["resources"]["items"] == []


def test_oversized_receipt_preserves_prior_download_records(handoff: tuple[Any, ...]) -> None:
    from leanflow_cli.workflows.prover import resource_handoff

    _, job, resource = handoff
    workspace = Path(job["workspace"])
    receipt = workspace.parent / ".runtime" / workspace.name / "resource-receipts.json"
    before = receipt.read_bytes()
    oversized = {
        **resource,
        "url": "https://example.org/" + "x" * resource_handoff.MAX_RECEIPT_BYTES,
    }
    with pytest.raises(ValueError, match="receipt exceeds"):
        resource_handoff.record_download(workspace, oversized)
    assert receipt.read_bytes() == before
