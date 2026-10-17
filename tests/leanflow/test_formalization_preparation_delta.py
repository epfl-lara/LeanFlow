"""Preserve existing dependency metadata and publish only independently checked changed leaves."""

import pytest

from leanflow_cli.formalization import preparation_audit, preparation_delta, preparation_transaction
from leanflow_cli.formalization.preparation_fs_inputs import copy_entry, entry_identity


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))


def test_delta_avoids_whole_lake_replacement_and_preserves_all_existing_git_metadata(tmp_path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    package = root / ".lake/packages/library"
    (package / ".git/hooks").mkdir(parents=True)
    (package / ".git/config").write_text("original credential-bearing config retained in canonical")
    (package / ".git/hooks/pre-commit").write_text("unexecuted original hook")
    (package / "Shared.olean").write_bytes(b"unchanged compiled library")
    (package / "changed.trace").write_bytes(b"old trace")
    baseline = preparation_delta.infrastructure_baseline(root)
    stage.mkdir()
    copy_entry(root / ".lake", stage / ".lake")
    (stage / ".lake/packages/library/.git/config").unlink()
    (stage / ".lake/packages/library/changed.trace").write_bytes(b"new trace")
    new = stage / ".lake/packages/new_library"
    new.mkdir()
    (new / "Built.olean").write_bytes(b"new dependency")
    inode = (package / "Shared.olean").stat().st_ino
    config_inode = (package / ".git/config").stat().st_ino
    delta = preparation_delta.managed_delta(root, stage, baseline)
    assert set(delta) == {".lake/packages/library/changed.trace", ".lake/packages/new_library"}
    journal = preparation_transaction.commit_preparation(
        root,
        stage,
        managed=delta,
        protected={},
        receipt={},
        validate=lambda: None,
        cancelled=lambda: False,
    )
    assert set(journal["writes"]) == set(delta)
    assert (package / "Shared.olean").stat().st_ino == inode
    assert (package / ".git/config").stat().st_ino == config_inode
    assert (package / ".git/hooks/pre-commit").read_text() == "unexecuted original hook"
    assert not new.exists()
    assert (
        preparation_delta.managed_delta(root, root, preparation_delta.infrastructure_baseline(root))
        == {}
    )


def test_delta_never_adopts_a_concurrent_leaf_save_as_original(tmp_path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    (root / ".lake").mkdir(parents=True)
    (root / ".lake/output").write_text("before")
    baseline = preparation_delta.infrastructure_baseline(root)
    (stage / ".lake").mkdir(parents=True)
    (stage / ".lake/output").write_text("checked")
    (root / ".lake/output").write_text("concurrent user save")
    delta = preparation_delta.managed_delta(root, stage, baseline)
    with pytest.raises(preparation_transaction.PreparationConflict):
        preparation_transaction.commit_preparation(
            root,
            stage,
            managed=delta,
            protected={},
            receipt={},
            validate=lambda: None,
            cancelled=lambda: False,
        )
    assert (root / ".lake/output").read_text() == "concurrent user save"


def test_retained_audit_cache_reads_bytes_once_then_detects_fd_edits_additions_and_loss(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    retained = root / "retained"
    retained.mkdir(parents=True)
    file = retained / "compiled"
    file.write_bytes(b"initial")
    actual = preparation_audit.entry_identity
    calls = []
    monkeypatch.setattr(
        preparation_audit,
        "entry_identity",
        lambda path, **kw: calls.append(path) or actual(path, **kw),
    )
    first = preparation_audit.retained_identity(root, retained)
    assert first == entry_identity(retained)
    assert preparation_audit.retained_identity(root, retained) == first and len(calls) == 1
    with file.open("ab") as descriptor:
        descriptor.write(b"late descriptor save")
        descriptor.flush()
        second = preparation_audit.retained_identity(root, retained)
    assert second != first and len(calls) == 2
    (retained / "added").write_bytes(b"new")
    third = preparation_audit.retained_identity(root, retained)
    assert third != second and len(calls) == 3
    (retained / "added").unlink()
    assert preparation_audit.retained_identity(root, retained) == second and len(calls) == 4


def test_exact_nonextension_source_is_protected_but_incidental_files_are_not(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "input.data").write_text("authoritative source")
    (root / "paper.aux").write_text("build output")
    (root / "Local.lean").write_text("import Init")
    protected = preparation_transaction.protected_inputs(root, source_paths=["input.data"])
    assert set(protected) == {"input.data", "Local.lean"}


def test_existing_package_links_keep_tree_boundary_and_original_inode_through_delta(tmp_path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    package = root / ".lake/packages/batteries"
    (package / "docs").mkdir(parents=True)
    (package / "README.md").write_text("package documentation")
    link = package / "docs/README.md"
    link.symlink_to("../README.md")
    directory_link = package / "documentation"
    directory_link.symlink_to("docs", target_is_directory=True)
    before = link.lstat()
    baseline = preparation_delta.infrastructure_baseline(root)
    relative = link.relative_to(root).as_posix()
    assert baseline[relative] == entry_identity(link, link_root=root / ".lake")
    stage.mkdir()
    copy_entry(root / ".lake", stage / ".lake")
    assert preparation_delta.managed_delta(root, stage, baseline) == {}
    assert link.lstat().st_ino == before.st_ino and link.lstat().st_mode == before.st_mode
    assert link.readlink().as_posix() == "../README.md"
    assert directory_link.is_symlink()


def test_baseline_rejects_package_link_escaping_the_dependency_tree(tmp_path):
    root = tmp_path / "root"
    docs = root / ".lake/packages/batteries/docs"
    docs.mkdir(parents=True)
    (root / "outside.txt").write_text("outside authority")
    (docs / "README.md").symlink_to("../../../../outside.txt")
    with pytest.raises(ValueError, match="escaping link"):
        preparation_delta.infrastructure_baseline(root)


@pytest.mark.parametrize("change", ["new", "changed", "escaping"])
def test_delta_rejects_new_changed_or_escaping_package_links(tmp_path, change):
    root, stage = tmp_path / "root", tmp_path / "stage"
    package = root / ".lake/packages/batteries"
    (package / "docs").mkdir(parents=True)
    (package / "README.md").write_text("package documentation")
    (package / "alternative.md").write_text("independent documentation")
    (package / "docs/README.md").symlink_to("../README.md")
    baseline = preparation_delta.infrastructure_baseline(root)
    stage.mkdir()
    copy_entry(root / ".lake", stage / ".lake")
    staged_link = stage / ".lake/packages/batteries/docs/README.md"
    if change == "new":
        staged_link = staged_link.with_name("new-link.md")
        staged_link.symlink_to("../README.md")
    else:
        staged_link.unlink()
        staged_link.symlink_to(
            "../alternative.md" if change == "changed" else "../../../../outside.txt"
        )
    with pytest.raises(ValueError, match="aliased|escaping"):
        preparation_delta.managed_delta(root, stage, baseline)
    assert (package / "docs/README.md").readlink().as_posix() == "../README.md"
