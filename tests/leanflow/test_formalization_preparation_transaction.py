"""Exercise infrastructure commitment, directory rollback, historical FD edits, and resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization import (
    preparation_journal,
    preparation_progress,
    preparation_resolution_progress,
)
from leanflow_cli.formalization import preparation_transaction as transaction
from leanflow_cli.formalization.preparation_journal import load_journal, load_registry


def _project(root: Path, staged: Path) -> dict[str, str | None]:
    root.mkdir()
    staged.mkdir()
    (root / "lakefile.toml").write_text('name = "old"\n')
    (staged / "lakefile.toml").write_text('name = "prepared"\n')
    (staged / "lake-manifest.json").write_text('{"packages":[]}\n')
    for parent, data in ((root, b"old build"), (staged, b"new build")):
        library = parent / ".lake/packages/sample/.lake/build/lib/lean/Example.olean"
        library.parent.mkdir(parents=True)
        library.write_bytes(data)
    source = root / "User.lean"
    source.write_text("theorem unrelated : True := by sorry\n")
    (staged / "src").mkdir()
    (staged / "src/NewRoot.lean").write_text("import Init\n")
    return {
        name: transaction.entry_identity(root / name)
        for name in ("lakefile.toml", "lake-manifest.json", ".lake", "src/NewRoot.lean")
    }


def _commit(
    root: Path,
    staged: Path,
    managed: dict,
    checkpoint=None,
    validate=lambda: None,
    cancelled=lambda: False,
):
    return transaction.commit_preparation(
        root,
        staged,
        managed=managed,
        protected=transaction.protected_inputs(root),
        receipt={"schema_version": 1, "preparation_id": "receipt"},
        validate=validate,
        cancelled=cancelled,
        checkpoint=checkpoint,
    )


def _observed(root: Path) -> dict:
    shown = next(
        row for row in transaction.preparation_conflicts(root)["transactions"] if row["active"]
    )
    return {
        key: shown[key]
        for key in (
            "transaction_id",
            "observation_id",
            "destinations",
            "retained_slots",
            "history_id",
        )
    }


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))


def test_commit_preserves_source_and_existing_inodes_and_selects_receipt_after_validation(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    old_inode = (root / "lakefile.toml").stat().st_ino
    user = (root / "User.lean").read_bytes()
    seen = []

    def validate():
        assert not (transaction.preparation_directory(root) / "receipt.json").exists()
        assert (root / "src/NewRoot.lean").read_text() == "import Init\n"
        seen.append("checked")

    journal = _commit(root, staged, managed, validate=validate)
    assert seen == ["checked"] and journal["state"] == "COMMITTED"
    assert (root / journal["writes"]["lakefile.toml"]["captured"]).stat().st_ino == old_inode
    assert (root / "User.lean").read_bytes() == user
    assert (transaction.preparation_directory(root) / "receipt.json").exists()
    transaction.assert_no_pending_preparation(root)


@pytest.mark.parametrize(
    "boundary",
    [
        "registered",
        "copied:.lake",
        "prepared",
        "captured:lakefile.toml",
        "moved:lake-manifest.json",
        "before_commit",
    ],
)
def test_interrupted_setup_restores_absent_manifest_and_new_import_root(tmp_path, boundary):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def crash(name):
        if name == boundary:
            raise SystemExit("process death")

    with pytest.raises(SystemExit):
        _commit(root, staged, managed, checkpoint=crash)
    recovered = transaction.recover_preparation(root)
    assert recovered["state"] == "ROLLED_BACK"
    assert not (root / "lake-manifest.json").exists() and not (root / "src/NewRoot.lean").exists()
    assert transaction.entry_identity(root / ".lake") == managed[".lake"]
    assert transaction.entry_identity(root / "lakefile.toml") == managed["lakefile.toml"]
    transaction.assert_no_pending_preparation(root)


def test_failure_after_installation_keeps_existing_math_source_unchanged(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def reject():
        raise ValueError("independent readiness rejected")

    with pytest.raises(ValueError, match="readiness rejected"):
        _commit(root, staged, managed, validate=reject)
    assert (root / "User.lean").read_text() == "theorem unrelated : True := by sorry\n"
    assert transaction.entry_identity(root / ".lake") == managed[".lake"]
    assert not (root / "lake-manifest.json").exists()


def test_concurrent_save_preserves_both_then_observed_resolution_allows_restart(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def save(name):
        if name == "captured:lakefile.toml":
            (root / "lakefile.toml").write_text("user save\n")

    with pytest.raises(transaction.PreparationConflict):
        _commit(root, staged, managed, checkpoint=save)
    before = transaction.load_preparation_transaction(root)
    assert (root / "lakefile.toml").read_text() == "user save\n"
    assert (root / before["writes"]["lakefile.toml"]["captured"]).read_text() == 'name = "old"\n'
    observed = _observed(root)
    resolved = transaction.resolve_preparation_conflict(
        root, observed, "Reconciled all versions; keep canonical editor result"
    )
    assert resolved["conflicts"] == before["conflicts"] and resolved["state"] == "RESOLVED"
    assert not (transaction.preparation_directory(root) / "receipt.json").exists()
    for expected, relative in resolved["resolutions"][-1]["archives"].items():
        assert transaction.entry_identity(root / relative) == expected
    transaction.recover_preparation(root)
    transaction.assert_no_pending_preparation(root)


def test_resolution_rejects_wrong_transaction_or_hash_drift(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    with (root / "lakefile.toml").open("a") as editor:

        def save(name):
            if name == "installed:lakefile.toml":
                editor.write("user descriptor edit\n")
                editor.flush()

        with pytest.raises(transaction.PreparationConflict):
            _commit(root, staged, managed, checkpoint=save)
    observed = _observed(root)
    with pytest.raises(transaction.PreparationConflict, match="transaction"):
        transaction.resolve_preparation_conflict(
            root, {**observed, "transaction_id": "wrong"}, "Reviewed"
        )
    (root / "lakefile.toml").write_text("another save")
    with pytest.raises(transaction.PreparationConflict, match="versions changed"):
        transaction.resolve_preparation_conflict(root, observed, "Reviewed")


def test_archive_copy_process_exit_is_visible_and_retry_preserves_independent_partial(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def save(name):
        if name == "captured:lakefile.toml":
            (root / "lakefile.toml").write_text("canonical editor version\n")

    with pytest.raises(transaction.PreparationConflict):
        _commit(root, staged, managed, checkpoint=save)
    original = _observed(root)
    source = (root / "User.lean").read_bytes()
    canonical = transaction.entry_identity(root / ".lake")
    script = """
import json, os, sys
from pathlib import Path
from leanflow_cli.formalization import preparation_transaction as transaction
root = Path(sys.argv[1])
observed = json.loads(sys.argv[2])
def exit_during_copy(name):
    if name.startswith('resolution_archive_copy:'):
        path = root / name.split(':', 1)[1]
        if path.is_dir():
            os._exit(89)
transaction.resolve_preparation_conflict(root, observed, 'Preserve all actual versions', checkpoint=exit_during_copy)
"""
    result = subprocess.run([sys.executable, "-c", script, str(root), json.dumps(original)])
    assert result.returncode == 89
    journal = transaction.load_preparation_transaction(root)
    intent = journal["archive_copy_intents"][0]
    partial = root / intent["path"]
    assert partial.is_dir() and transaction.entry_identity(partial) != intent["expected"]
    shown = _observed(root)
    assert shown["retained_slots"][intent["path"]] == transaction.entry_identity(partial)
    with pytest.raises(transaction.PreparationConflict, match="versions changed"):
        transaction.resolve_preparation_conflict(root, original, "Old observation")
    (partial / "independent.txt").write_text("User reconciliation inside interrupted archive\n")
    with pytest.raises(transaction.PreparationConflict, match="versions changed"):
        transaction.resolve_preparation_conflict(root, shown, "Stale partial observation")
    actual = transaction.entry_identity(partial)
    resolved = transaction.resolve_preparation_conflict(
        root, _observed(root), "Reviewed partial and canonical versions"
    )
    assert resolved["state"] == "RESOLVED"
    assert transaction.entry_identity(partial) == actual
    assert actual in resolved["resolutions"][-1]["archives"]
    assert transaction.entry_identity(root / ".lake") == canonical
    assert (root / "User.lean").read_bytes() == source
    transaction.assert_no_pending_preparation(root)
    (partial / "independent.txt").write_text("Later edit\n")
    with pytest.raises(transaction.PreparationConflict):
        transaction.assert_no_pending_preparation(root)
    assert any(
        "archive version changed" in issue
        for issue in load_journal(root, resolved["transaction_id"])["conflicts"]
    )


def test_legacy_unregistered_partial_archive_is_observed_and_preserved_on_retry(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def save(name):
        if name == "captured:lakefile.toml":
            (root / "lakefile.toml").write_text("user version\n")

    with pytest.raises(transaction.PreparationConflict):
        _commit(root, staged, managed, checkpoint=save)
    before = _observed(root)
    expected = before["destinations"][".lake"]
    legacy = (
        root / f".leanflow/preparation-transactions/{before['transaction_id']}/versions/{expected}"
    )
    legacy.mkdir(parents=True)
    (legacy / "user.txt").write_text("Preserve legacy interrupted bytes")
    actual = transaction.entry_identity(legacy)
    observed = _observed(root)
    assert observed["retained_slots"][legacy.relative_to(root).as_posix()] == actual
    with pytest.raises(transaction.PreparationConflict, match="versions changed"):
        transaction.resolve_preparation_conflict(root, before, "Old observation")
    result = transaction.resolve_preparation_conflict(
        root, observed, "Reconciled legacy partial archive"
    )
    assert result["state"] == "RESOLVED" and transaction.entry_identity(legacy) == actual
    assert actual in result["resolutions"][-1]["archives"]
    transaction.assert_no_pending_preparation(root)


def test_resolution_intent_bytes_and_destination_observations_scale_linearly(tmp_path, monkeypatch):
    measurements = []
    for count in (8, 16):
        root, staged = tmp_path / f"root-{count}", tmp_path / f"stage-{count}"
        for directory in (root, staged):
            (directory / ".lake").mkdir(parents=True)
        (root / "lakefile.toml").write_text('name = "old"\n')
        (staged / "lakefile.toml").write_text('name = "new"\n')
        names = ["lakefile.toml"]
        for index in range(count):
            relative = f".lake/leaf-{index}.bin"
            names.append(relative)
            (root / relative).write_text(f"old bytes {index}")
            (staged / relative).write_text(f"new bytes {index}")
        managed = {name: transaction.entry_identity(root / name) for name in names}

        def editor(name):
            if name == "captured:lakefile.toml":
                (root / "lakefile.toml").write_text("canonical user choice\n")

        with pytest.raises(transaction.PreparationConflict):
            _commit(root, staged, managed, checkpoint=editor)
        observed = _observed(root)
        stats = {"bytes": 0, "snapshots": 0, "intents": 0, "destinations": 0}
        actual_json = preparation_progress.durable_json
        actual_publish = preparation_resolution_progress._publish_record
        actual_identity = preparation_journal.entry_identity

        def write(path, value):
            stats["bytes"] += len(
                (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            )
            if path.parent.name == "transactions":
                stats["snapshots"] += 1
            else:
                stats["intents"] += 1
            if path.parent.name == "transactions":
                actual_json(path, value)
            else:
                actual_publish(path, value)

        def inspect(path):
            if path.relative_to(root).as_posix() in managed:
                stats["destinations"] += 1
            return actual_identity(path)

        with monkeypatch.context() as patched:
            patched.setattr(preparation_progress, "durable_json", write)
            patched.setattr(preparation_resolution_progress, "_publish_record", write)
            patched.setattr(preparation_journal, "entry_identity", inspect)
            resolved = transaction.resolve_preparation_conflict(
                root, observed, "Reviewed all leaf versions"
            )
        assert resolved["state"] == "RESOLVED"
        assert stats["snapshots"] == 1 and stats["intents"] == len(
            resolved["resolutions"][-1]["archives"]
        )
        assert stats["destinations"] == 2 * len(managed)
        measurements.append(stats)
    assert measurements[1]["bytes"] < 2.3 * measurements[0]["bytes"], measurements


def test_lost_full_journal_keeps_archive_intent_bytes_in_exact_loss_observation(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def editor(name):
        if name == "captured:lakefile.toml":
            (root / "lakefile.toml").write_text("canonical user choice\n")

    with pytest.raises(transaction.PreparationConflict):
        _commit(root, staged, managed, checkpoint=editor)
    observed = _observed(root)
    journal = load_journal(root, observed["transaction_id"])
    expected = observed["destinations"][".lake"]
    payload = {
        "path": f".leanflow/preparation-transactions/{journal['transaction_id']}/versions/{expected}",
        "expected": expected,
        "source": ".lake",
        "observation_id": observed["observation_id"],
    }
    from leanflow_cli.formalization.evidence import identity

    preparation_resolution_progress.save_archive_intent(
        transaction.preparation_directory(root),
        journal,
        {**payload, "intent_id": identity(payload)},
    )
    directory = transaction.preparation_directory(root)
    record = next((directory / "archive-intents").rglob("*.json"))
    (directory / "transactions" / f"{journal['transaction_id']}.json").unlink()
    loss = transaction.preparation_conflicts(root)["transactions"][0]
    assert loss["unavailable_journal"] is True and loss["planned_versions"] is None
    assert str(record) in loss["retained_slots"]
    first = {
        key: loss[key]
        for key in (
            "transaction_id",
            "observation_id",
            "destinations",
            "retained_slots",
            "history_id",
        )
    }
    record.write_bytes(record.read_bytes() + b" ")
    with pytest.raises(transaction.PreparationConflict, match="versions changed"):
        transaction.resolve_preparation_conflict(root, first, "Stale intent loss acknowledgment")


def test_archive_intent_exclusive_publication_preserves_actual_concurrent_creator(
    tmp_path, monkeypatch
):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def editor(name):
        if name == "captured:lakefile.toml":
            (root / "lakefile.toml").write_text("canonical user choice\n")

    with pytest.raises(transaction.PreparationConflict):
        _commit(root, staged, managed, checkpoint=editor)
    observed = _observed(root)
    journal = load_journal(root, observed["transaction_id"])
    expected = observed["destinations"][".lake"]
    payload = {
        "path": f".leanflow/preparation-transactions/{journal['transaction_id']}/versions/{expected}",
        "expected": expected,
        "source": ".lake",
        "observation_id": observed["observation_id"],
    }
    from leanflow_cli.formalization.evidence import identity

    link = preparation_resolution_progress.os.link
    winner = []

    def concurrent_creator(source, target, **kwargs):
        script = "import json, os, sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({'independent_creator_pid': os.getpid()}))"
        result = subprocess.run([sys.executable, "-c", script, str(target)])
        assert result.returncode == 0
        winner.append((Path(target), Path(target).read_bytes()))
        return link(source, target, **kwargs)

    monkeypatch.setattr(preparation_resolution_progress.os, "link", concurrent_creator)
    with pytest.raises(ValueError, match="Existing preparation archive intent changed"):
        preparation_resolution_progress.save_archive_intent(
            transaction.preparation_directory(root),
            journal,
            {**payload, "intent_id": identity(payload)},
        )
    assert len(winner) == 1 and winner[0][0].read_bytes() == winner[0][1]
    assert json.loads(winner[0][1])["independent_creator_pid"] != os.getpid()
    assert not (root / payload["path"]).exists()


def test_old_committed_descriptor_remains_audited_after_newer_transaction(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    with (root / "lakefile.toml").open("a") as editor:
        first = _commit(root, staged, managed)
        second_stage = tmp_path / "second"
        second_stage.mkdir()
        (second_stage / "lakefile.toml").write_text('name = "second"\n')
        second = _commit(
            root,
            second_stage,
            {"lakefile.toml": transaction.entry_identity(root / "lakefile.toml")},
        )
        editor.write("first historical descriptor edit\n")
        editor.flush()
        with pytest.raises(transaction.PreparationConflict, match="Retained user version"):
            transaction.assert_no_pending_preparation(root)
        assert load_registry(root)["active"] == first["transaction_id"]
        assert load_registry(root)["current"] == second["transaction_id"]
        resolved = transaction.resolve_preparation_conflict(
            root, _observed(root), "Keep latest checked canonical state"
        )
        archive_hashes = set(resolved["resolutions"][-1]["archives"])
        editor.write("second historical descriptor edit\n")
        editor.flush()
        with pytest.raises(transaction.PreparationConflict, match="Retained user version"):
            transaction.assert_no_pending_preparation(root)
        again = transaction.resolve_preparation_conflict(
            root, _observed(root), "Reviewed later retained edit"
        )
    assert len(again["resolutions"]) == 2
    assert (root / "lakefile.toml").read_text() == 'name = "second"\n'
    assert archive_hashes <= set(again["resolutions"][0]["archives"])
    assert load_journal(root, first["transaction_id"])["conflicts"]
    transaction.assert_no_pending_preparation(root)


def test_simultaneous_setup_descriptor_conflicts_have_independent_observed_resolution(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    with (root / "lakefile.toml").open("a") as first_editor:
        first = _commit(root, staged, managed)
        with (root / "lakefile.toml").open("a") as second_editor:
            second_stage = tmp_path / "second"
            second_stage.mkdir()
            (second_stage / "lakefile.toml").write_text('name = "second"\n')
            second = _commit(
                root,
                second_stage,
                {"lakefile.toml": transaction.entry_identity(root / "lakefile.toml")},
            )
            for editor in (first_editor, second_editor):
                editor.write("retained concurrent save\n")
                editor.flush()
            shown = transaction.preparation_conflicts(root)["transactions"]
            assert {row["transaction_id"] for row in shown if row["queued"]} == {
                first["transaction_id"],
                second["transaction_id"],
            }
            assert len(load_registry(root)["conflict_queue"]) == 2
            for journal in (second, first):
                row = next(
                    item for item in shown if item["transaction_id"] == journal["transaction_id"]
                )
                observed = {
                    key: row[key]
                    for key in (
                        "transaction_id",
                        "observation_id",
                        "destinations",
                        "retained_slots",
                        "history_id",
                    )
                }
                resolved = transaction.resolve_preparation_conflict(
                    root, observed, "Reviewed retained setup edits"
                )
                assert resolved["conflicts"] and resolved["resolutions"]
                if journal is second:
                    with pytest.raises(transaction.PreparationConflict):
                        transaction.assert_no_pending_preparation(root)
    assert load_registry(root)["conflict_queue"] == []
    assert (root / "lakefile.toml").read_text() == 'name = "second"\n'
    transaction.recover_preparation(root)
    transaction.assert_no_pending_preparation(root)


def test_resolved_setup_descriptor_remains_recoverable_while_next_setup_is_inflight(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    with (root / "lakefile.toml").open("a") as editor:
        first = _commit(root, staged, managed)
        editor.write("first retained save\n")
        editor.flush()
        transaction.preparation_conflicts(root)
        transaction.resolve_preparation_conflict(root, _observed(root), "Reviewed first save")
        second_stage = tmp_path / "second"
        second_stage.mkdir()
        (second_stage / "lakefile.toml").write_text('name = "second"\n')

        def crash(boundary):
            if boundary == "before_commit":
                raise SystemExit("pending next setup")

        with pytest.raises(SystemExit):
            _commit(
                root,
                second_stage,
                {"lakefile.toml": transaction.entry_identity(root / "lakefile.toml")},
                checkpoint=crash,
            )
        pending = load_registry(root)["active"]
        editor.write("second retained save\n")
        editor.flush()
        transaction.preparation_conflicts(root)
        assert load_registry(root)["active"] == pending
        assert load_registry(root)["conflict_queue"] == [first["transaction_id"]]
        with pytest.raises(transaction.PreparationConflict):
            transaction.recover_preparation(root)
        assert (root / "lakefile.toml").read_text() == 'name = "prepared"\n'
        transaction.resolve_preparation_conflict(root, _observed(root), "Reviewed later save")
    transaction.assert_no_pending_preparation(root)


@pytest.mark.parametrize(
    "code", ["theorem bad : True := by sorry\n", "def data := 0\n", "#eval 1\n"]
)
def test_setup_rejects_math_in_created_library_roots(tmp_path, code):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    (staged / "src/NewRoot.lean").write_text(code)
    with pytest.raises(ValueError, match="imports only"):
        _commit(root, staged, managed)
    assert not (root / "src/NewRoot.lean").exists()
    assert (root / "lakefile.toml").read_text() == 'name = "old"\n'


def test_existing_mathematics_cannot_be_owned_even_when_bytes_equal(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    (staged / "User.lean").write_bytes((root / "User.lean").read_bytes())
    with pytest.raises(ValueError, match="existing mathematical source"):
        _commit(
            root, staged, {**managed, "User.lean": transaction.entry_identity(root / "User.lean")}
        )


def test_entry_identity_ignores_top_entry_basename_but_binds_complete_modes_and_contents(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "value").write_bytes(b"same")
    (two / "value").write_bytes(b"same")
    assert transaction.entry_identity(one) == transaction.entry_identity(two)
    (two / "value").chmod(0o600)
    assert transaction.entry_identity(one) != transaction.entry_identity(two)


def test_missing_resolution_archive_remains_inspectable_and_exactly_reconcilable(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)

    def save(name):
        if name == "captured:lakefile.toml":
            (root / "lakefile.toml").write_text("user save\n")

    with pytest.raises(transaction.PreparationConflict):
        _commit(root, staged, managed, checkpoint=save)
    first = transaction.resolve_preparation_conflict(root, _observed(root), "Reviewed save")
    expected, archived = next(
        (checksum, relative)
        for checksum, relative in first["resolutions"][-1]["archives"].items()
        if (root / relative).is_file()
    )
    (root / archived).unlink()
    with pytest.raises(transaction.PreparationConflict):
        transaction.assert_no_pending_preparation(root)
    observed = _observed(root)
    assert observed["retained_slots"][archived] is None
    final = transaction.resolve_preparation_conflict(
        root,
        observed,
        "Acknowledged the lost kept copy; preserve recorded identity and current files",
    )
    assert final["resolutions"][0] == first["resolutions"][0]
    assert final["resolutions"][-1]["unavailable_versions"][archived] == expected
    assert not (root / archived).exists()
    transaction.recover_preparation(root)
    transaction.assert_no_pending_preparation(root)


def test_historical_resolution_keeps_unrelated_current_preparation_receipt(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    with (root / "lakefile.toml").open("a") as editor:
        first = _commit(root, staged, managed)
        second_stage = tmp_path / "second"
        second_stage.mkdir()
        (second_stage / "lakefile.toml").write_text('name = "second"\n')
        second = _commit(
            root,
            second_stage,
            {"lakefile.toml": transaction.entry_identity(root / "lakefile.toml")},
        )
        receipt = (transaction.preparation_directory(root) / "receipt.json").read_bytes()
        editor.write("late historical save\n")
        editor.flush()
        transaction.preparation_conflicts(root)
        observed = _observed(root)
        assert observed["transaction_id"] == first["transaction_id"]
        transaction.resolve_preparation_conflict(root, observed, "Keep current second preparation")
    assert load_registry(root)["selected"] == second["transaction_id"]
    assert (transaction.preparation_directory(root) / "receipt.json").read_bytes() == receipt
    transaction.assert_no_pending_preparation(root)


def test_editor_metadata_and_tex_build_outputs_do_not_block_preparation(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _project(root, staged)
    (root / "paper.tex").write_text("source mathematical document\n")
    source = (root / "paper.tex").read_bytes()

    def ordinary_activity(name):
        if name == "prepared":
            (root / "paper.aux").write_text("latex output")
            (root / "paper.log").write_text("latex output")
            (root / ".DS_Store").write_bytes(b"finder metadata")
            (root / "notes.txt").write_text("ordinary unrelated note")

    committed = _commit(root, staged, managed, checkpoint=ordinary_activity)
    assert committed["state"] == "COMMITTED"
    assert (root / "paper.tex").read_bytes() == source


WORKER = r"""
import json, os, sys
from pathlib import Path
from leanflow_cli.formalization import preparation_transaction as tx
root, staged = Path(sys.argv[1]), Path(sys.argv[2])
mode, stop = sys.argv[3], int(sys.argv[4])
events = []
def checkpoint(name):
    events.append(name)
    if len(events) == stop or (mode == 'prepare' and name == 'before_commit'):
        os._exit(91)
if mode == 'recover':
    tx.recover_preparation(root, checkpoint=checkpoint)
else:
    managed = {name: tx.entry_identity(root/name) for name in ('lakefile.toml','lake-manifest.json','.lake','src/NewRoot.lean')}
    tx.commit_preparation(root, staged, managed=managed, protected=tx.protected_inputs(root), receipt={'schema_version':1,'preparation_id':'receipt'}, validate=lambda:None, cancelled=lambda:False, checkpoint=checkpoint)
print(json.dumps(events))
"""


def _spawn(root: Path, staged: Path, mode: str, stop: int = 0):
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), str(staged), mode, str(stop)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode in {0, 91}, result.stderr
    return result


def test_real_process_exit_at_every_preparation_and_rollback_checkpoint(tmp_path):
    baseline, staged = tmp_path / "baseline", tmp_path / "stage"
    _project(baseline, staged)
    events = json.loads(_spawn(baseline, staged, "publish").stdout)
    assert {"registered", "before_commit", "journal:COMMITTED", "settled"} <= set(events)
    for index, boundary in enumerate(events, start=1):
        root, source = tmp_path / f"root-{index}", tmp_path / f"stage-{index}"
        managed = _project(root, source)
        assert _spawn(root, source, "publish", index).returncode == 91
        transaction.recover_preparation(root)
        committed = boundary in {"journal:COMMITTED", "committed", "settled"}
        assert (root / "lake-manifest.json").exists() is committed, (index, boundary)
        assert (root / "src/NewRoot.lean").exists() is committed, (index, boundary)
        assert (root / "User.lean").read_text() == "theorem unrelated : True := by sorry\n"
        if not committed:
            assert transaction.entry_identity(root / ".lake") == managed[".lake"]
        transaction.assert_no_pending_preparation(root)
    recover_root, recover_stage = tmp_path / "recover-baseline", tmp_path / "recover-stage"
    _project(recover_root, recover_stage)
    assert _spawn(recover_root, recover_stage, "prepare").returncode == 91
    recover_events = json.loads(_spawn(recover_root, recover_stage, "recover").stdout)
    for index, boundary in enumerate(recover_events, start=1):
        root, source = tmp_path / f"undo-{index}", tmp_path / f"undo-stage-{index}"
        managed = _project(root, source)
        assert _spawn(root, source, "prepare").returncode == 91
        assert _spawn(root, source, "recover", index).returncode == 91
        assert transaction.recover_preparation(root)["state"] == "ROLLED_BACK", (index, boundary)
        assert transaction.entry_identity(root / ".lake") == managed[".lake"]
        assert not (root / "lake-manifest.json").exists()
        transaction.assert_no_pending_preparation(root)
