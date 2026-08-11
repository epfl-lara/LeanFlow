from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from leanflow_cli.workflows import scratch_artifact_cleanup as cleanup


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _initialize_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)


def _scratch_entry(
    root: Path,
    *,
    process_id: int = 111,
    active_file: str = "Main.lean",
    isolation_version: int = 0,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, object]:
    return {
        "state": "done",
        "process_id": process_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "spec": {
            "job_id": f"campaign.orchestrator.ds-{process_id}",
            "scope": {
                "scratch_only": True,
                **({"isolation_version": isolation_version} if isolation_version else {}),
            },
            "inputs": {"active_file": str(root / active_file)},
        },
    }


def _checkpoint(
    state_root: Path,
    *,
    checkpoint_id: str,
    artifact: Path,
    created_at: datetime,
    initial: bool,
) -> Path:
    path = state_root / "verified-patch-checkpoints" / f"{checkpoint_id}.json"
    action = "Add" if initial else "Update"
    _write_json(
        path,
        {
            "checkpoint_id": checkpoint_id,
            "created_at": created_at.isoformat(),
            "file_path": str(artifact),
            "cwd": str(artifact.parent),
            "before_bytes": 0 if initial else 12,
            "patch": f"*** Begin Patch\n*** {action} File: {artifact}\n*** End Patch",
        },
    )
    return path


def _activity_event(
    *,
    event_type: str,
    tool: str,
    process_id: int,
    timestamp: datetime,
    arguments: dict[str, object],
    result: object = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "tool": tool,
        "process_id": process_id,
        "arguments": arguments,
    }
    if event_type == "tool-result":
        details.update({"is_error": False, "result": result})
    return {
        "type": event_type,
        "timestamp": timestamp.isoformat(),
        "details": details,
    }


def _write_activity(state_root: Path, *events: dict[str, object]) -> None:
    path = state_root / "activity" / "agents" / "prove-worker.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_cleanup_removes_attributed_patch_artifact_and_shared_metadata(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _initialize_git(root)
    main = root / "Main.lean"
    main.write_text("import Mathlib\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "Main.lean"], check=True)
    state_root = root / ".leanflow" / "workflow-state"
    started = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    created = started + timedelta(seconds=5)
    finished = started + timedelta(minutes=2)
    isolated_running = _scratch_entry(
        root,
        process_id=222,
        isolation_version=cleanup.SCRATCH_ISOLATION_VERSION,
        started_at=started,
        finished_at=finished,
    )
    isolated_running["state"] = "running"
    isolated_running["finished_at"] = ""
    _write_json(
        state_root / "summary.json",
        {
            "dispatch_ledger": [
                _scratch_entry(root, started_at=started, finished_at=finished),
                isolated_running,
            ]
        },
    )

    artifact = root / "Generation73Scratch.lean"
    artifact.write_text("import Mathlib\n", encoding="utf-8")
    modified = created + timedelta(seconds=90)
    os.utime(artifact, (modified.timestamp(), modified.timestamp()))
    first = _checkpoint(
        state_root,
        checkpoint_id="vpatch-add",
        artifact=artifact,
        created_at=created,
        initial=True,
    )
    second = _checkpoint(
        state_root,
        checkpoint_id="vpatch-update",
        artifact=artifact,
        created_at=created + timedelta(seconds=3),
        initial=False,
    )
    unrelated = _checkpoint(
        state_root,
        checkpoint_id="vpatch-unrelated",
        artifact=root / "Other.lean",
        created_at=created,
        initial=False,
    )
    _write_json(
        state_root / "verified_patch_status.json",
        {"version": 1, "latest": {"path": str(artifact), "cwd": str(root)}},
    )
    _write_activity(
        state_root,
        _activity_event(
            event_type="tool-result",
            tool="apply_verified_patch",
            process_id=111,
            # The initial verification may take much longer than checkpoint
            # creation; the exact checkpoint id, not a short time heuristic,
            # proves which write produced the artifact.
            timestamp=created + timedelta(seconds=70),
            arguments={
                "path": str(artifact),
                "cwd": str(root),
                "patch": f"*** Begin Patch\n*** Add File: {artifact}\n*** End Patch",
            },
            result=json.dumps({"patch_applied": True, "checkpoint_id": "vpatch-add"}),
        ),
    )

    result = cleanup.cleanup_legacy_scratch_artifacts(cwd=root)

    assert result["status"] == "completed"
    assert result["patch_artifacts_removed"] == 1
    assert result["checkpoints_removed"] == 2
    assert result["verified_patch_status_cleared"] == 1
    assert not artifact.exists()
    assert not first.exists()
    assert not second.exists()
    assert unrelated.exists()
    status = json.loads((state_root / "verified_patch_status.json").read_text())
    assert status["latest"] == {}
    summary = json.loads((state_root / "summary.json").read_text())
    assert cleanup.MIGRATION_KEY in summary["maintenance_migrations"]

    repeated = cleanup.cleanup_legacy_scratch_artifacts(cwd=root)

    assert repeated["status"] == "already_completed"


def test_cleanup_removes_exact_old_axiom_harness_but_preserves_decoys(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _initialize_git(root)
    source = root / "Main.lean"
    source_lines = ["import Mathlib", "", "namespace Demo"] + [
        f"-- stable source line {index}" for index in range(40)
    ]
    source_lines += ["theorem demo : True := by trivial", "", "end Demo"]
    source.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "Main.lean"], check=True)
    state_root = root / ".leanflow" / "workflow-state"
    started = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    event_at = started + timedelta(seconds=10)
    finished = started + timedelta(minutes=1)
    _write_json(
        state_root / "summary.json",
        {"dispatch_ledger": [_scratch_entry(root, started_at=started, finished_at=finished)]},
    )

    harness = root / "tmpabcdefgh.lean"
    harness_lines = source_lines[:30] + ["#print axioms demo"] + source_lines[30:]
    harness.write_text("\n".join(harness_lines) + "\n", encoding="utf-8")
    os.utime(harness, (event_at.timestamp(), event_at.timestamp()))
    wrong_target = root / "tmpijklmnop.lean"
    wrong_target.write_text(
        "\n".join(source_lines[:30] + ["#print axioms other"] + source_lines[30:]) + "\n",
        encoding="utf-8",
    )
    os.utime(wrong_target, (event_at.timestamp(), event_at.timestamp()))
    user_file = root / "tmpqrstuvwx.lean"
    user_file.write_text("import Mathlib\n", encoding="utf-8")
    os.utime(user_file, (event_at.timestamp(), event_at.timestamp()))
    _write_activity(
        state_root,
        _activity_event(
            event_type="tool-call",
            tool="lean_axioms",
            process_id=111,
            timestamp=event_at,
            arguments={"cwd": str(root), "file_path": str(source), "target": "Demo.demo"},
        ),
    )

    result = cleanup.cleanup_legacy_scratch_artifacts(cwd=root)

    assert result["status"] == "completed"
    assert result["axiom_harnesses_removed"] == 1
    assert not harness.exists()
    assert wrong_target.exists()
    assert user_file.exists()


def test_cleanup_preserves_active_tracked_and_later_modified_candidates(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _initialize_git(root)
    started = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    created = started + timedelta(seconds=5)
    finished = started + timedelta(minutes=1)
    state_root = root / ".leanflow" / "workflow-state"
    active = root / "Active.lean"
    tracked = root / "Tracked.lean"
    modified = root / "Modified.lean"
    symlink_target = root / "SymlinkTarget.lean"
    symlinked = root / "SymlinkScratch.lean"
    for path in (active, tracked, modified):
        path.write_text("import Mathlib\n", encoding="utf-8")
    symlink_target.write_text("import Mathlib\n", encoding="utf-8")
    symlinked.symlink_to(symlink_target)
    subprocess.run(["git", "-C", str(root), "add", "Tracked.lean"], check=True)
    os.utime(active, (created.timestamp(), created.timestamp()))
    os.utime(tracked, (created.timestamp(), created.timestamp()))
    late = finished + timedelta(minutes=1)
    os.utime(modified, (late.timestamp(), late.timestamp()))
    entries = [
        _scratch_entry(
            root,
            process_id=111,
            active_file="Active.lean",
            started_at=started,
            finished_at=finished,
        )
    ]
    _write_json(state_root / "summary.json", {"dispatch_ledger": entries})
    events = []
    for path in (active, tracked, modified, symlinked):
        _checkpoint(
            state_root,
            checkpoint_id=f"vpatch-{path.stem.lower()}",
            artifact=path,
            created_at=created,
            initial=True,
        )
        events.append(
            _activity_event(
                event_type="tool-result",
                tool="apply_verified_patch",
                process_id=111,
                timestamp=created + timedelta(seconds=1),
                arguments={
                    "path": str(path),
                    "cwd": str(root),
                    "patch": f"*** Begin Patch\n*** Add File: {path}\n*** End Patch",
                },
                result={
                    "patch_applied": True,
                    "checkpoint_id": f"vpatch-{path.stem.lower()}",
                },
            )
        )
    _write_activity(state_root, *events)

    result = cleanup.cleanup_legacy_scratch_artifacts(cwd=root)

    assert result["status"] == "completed"
    assert result["artifacts_removed"] == 0
    assert result["artifacts_preserved"] == 4
    assert active.exists()
    assert tracked.exists()
    assert modified.exists()
    assert symlinked.is_symlink()


def test_cleanup_is_not_applicable_without_workflow_state(tmp_path):
    result = cleanup.cleanup_legacy_scratch_artifacts(cwd=tmp_path)

    assert result["status"] == "not_applicable"


def test_cleanup_defers_marker_while_a_scratch_job_is_unfinished(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _initialize_git(root)
    state_root = root / ".leanflow" / "workflow-state"
    started = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    entry = _scratch_entry(
        root,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
    )
    entry["state"] = "running"
    entry["finished_at"] = ""
    _write_json(state_root / "summary.json", {"dispatch_ledger": [entry]})

    result = cleanup.cleanup_legacy_scratch_artifacts(cwd=root)

    assert result["status"] == "deferred"
    assert result["unfinished_scratch_jobs"] == 1
    summary = json.loads((state_root / "summary.json").read_text())
    assert "maintenance_migrations" not in summary
