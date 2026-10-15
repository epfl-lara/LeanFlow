"""Verify resumable disk-pressure stops across native workflow entrypoints."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from leanflow_cli.formalization.records import create_document
from leanflow_cli.native import harness_workflow
from leanflow_cli.runtime.storage import StoragePressure
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.storage_admission import StorageAdmission


def _pressure(*args, **kwargs):
    raise StoragePressure(1024, 10 * 1024**3)


def test_prover_pressure_keeps_source_checkpoint_and_prevents_dispatch(tmp_path, monkeypatch):
    source = "theorem target : True := by sorry\n"
    target = tmp_path / "Main.lean"
    target.write_text(source)
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    calls = []
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(mode="standard"),
        run_id="storage-test",
        session=lambda **kwargs: calls.append(kwargs),
        verifier=object(),
    )
    monkeypatch.setattr(StorageAdmission, "check", _pressure)
    result = runtime.run()
    assert result["status"] == "storage_pressure"
    assert result["stop_code"] == "storage_pressure"
    assert "resume" in result["next_step"]
    assert calls == []
    assert target.read_text() == source
    saved = json.loads((runtime.store.directory / "state.json").read_text())
    assert saved["terminal"] is True and saved["status"] == "storage_pressure"
    assert saved["metrics"]["api_calls"] == 0


def test_formalization_pressure_saves_state_and_keeps_source(tmp_path, monkeypatch):
    target = tmp_path / "Main.lean"
    target.write_text("theorem target : True := by sorry\n")
    paper = tmp_path / "paper.tex"
    source = r"\begin{theorem}True holds.\end{theorem}"
    paper.write_text(source)
    record = create_document(tmp_path, paper)
    monkeypatch.setattr(StorageAdmission, "check", _pressure)
    result = harness_workflow.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
            "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
            "LEANFLOW_NATIVE_PROVIDER": "codex",
            "LEANFLOW_WORKFLOW_RUN_ID": "storage-native-test",
        }
    )
    assert result["status"] == "storage_pressure"
    assert "resume" in result["next_step"]
    assert target.read_text().endswith("sorry\n")
    assert paper.read_text() == source
    saved = json.loads(
        (tmp_path / ".leanflow/native-workflows/storage-native-test/state.json").read_text()
    )
    assert saved["status"] == "storage_pressure" and saved["terminal"] is True


def test_storage_periodic_checks_are_throttled_but_admission_is_mandatory(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.storage_admission.ensure_storage_capacity",
        lambda root: calls.append(root),
    )
    admission = StorageAdmission(Path(tmp_path))
    admission.check()
    admission.check()
    admission.check(force=True)
    assert calls == [tmp_path, tmp_path]


def test_quiet_prover_session_is_cancelled_on_disk_pressure_and_retains_progress(
    tmp_path, monkeypatch
):
    target = tmp_path / "Main.lean"
    target.write_text("theorem target : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    started = threading.Event()

    def check(self, **kwargs):
        if started.is_set():
            _pressure()

    def session(**kwargs):
        started.set()
        cancelled = kwargs["config"]["_cancelled"]
        while not cancelled():
            time.sleep(0.01)
        return {"status": "cancelled", "api_calls": 1, "final_response": "retained output"}

    class Verifier:
        def check(self, *args, **kwargs):
            return {"accepted": True}

        def compile_module(self, *args, **kwargs):
            return {"accepted": True}

        def final(self, *args, **kwargs):
            return {"accepted": True}

    monkeypatch.setattr(StorageAdmission, "check", check)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(mode="standard"),
        run_id="quiet-storage",
        session=session,
        verifier=Verifier(),
    )
    result = runtime.run()
    assert started.is_set()
    assert result["status"] == "storage_pressure"
    assert result["metrics"]["api_calls"] == 1
    assert target.read_text().endswith("sorry\n")
    assert (runtime.store.directory / "state.json").exists()
