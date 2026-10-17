"""Pin source enumeration and dependency-delta semantics while bounding repeated traversal work."""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import ClassVar

import pytest

from leanflow_cli.formalization.preparation_delta import managed_delta
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.workflows.prover.source import project_sources

_EXCLUDED = {".lake", ".git", ".leanflow", ".venv"}


def _write(root: Path, relative: str, content: str = "import Init\n") -> Path:
    """Create a fixture leaf and its ordinary parent directories."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _legacy_project_sources(root: Path) -> list[Path]:
    """Keep the prior enumeration expression as an independent equivalence oracle."""
    return sorted(
        path
        for path in root.rglob("*.lean")
        if not any(part in _EXCLUDED for part in path.relative_to(root).parts)
        and not path.is_symlink()
    )


def test_project_sources_preserves_nested_exclusions_lookalikes_and_sorting(tmp_path: Path):
    root = tmp_path / "root"
    kept = [
        "Z.lean",
        "A.lean",
        ".lean",
        "src/Nested.lean",
        ".lake2/Allowed.lean",
        ".github/Allowed.lean",
        ".leanflows/Allowed.lean",
        ".venv-backup/Allowed.lean",
        "src/.lake2/Allowed.lean",
    ]
    for relative in kept:
        _write(root, relative)
    for name in _EXCLUDED:
        _write(root, f"{name}/Hidden.lean")
        _write(root, f"src/{name}/nested/Hidden.lean")
    uppercase = _write(root, "src/NotLean.LEAN")
    backup = _write(root, "src/NotLean.lean.bak")

    expected = sorted(root / relative for relative in kept)
    legacy = _legacy_project_sources(root)
    if uppercase in legacy:
        expected = sorted([*expected, uppercase])
    assert legacy == expected
    assert backup not in legacy
    assert project_sources(root) == legacy


@pytest.mark.parametrize("name", sorted(_EXCLUDED))
def test_project_sources_does_not_exclude_its_root_basename(tmp_path: Path, name: str):
    root = tmp_path / name
    expected = sorted([_write(root, "Main.lean"), _write(root, "src/Other.lean")])
    _write(root, f"src/{name}/Hidden.lean")

    assert _legacy_project_sources(root) == expected
    assert project_sources(root) == expected


def test_project_sources_preserves_matching_directory_entries(tmp_path: Path):
    root = tmp_path / "root"
    inner = _write(root, "Namespace.lean/Inner.lean")
    ordinary = _write(root, ".lake.lean/More.lean")
    expected = sorted([inner.parent, inner, ordinary.parent, ordinary])

    # Path.rglob matches ordinary directory names too; enumeration must not
    # silently change that existing behavior while pruning dependency trees.
    assert _legacy_project_sources(root) == expected
    assert project_sources(root) == expected


@pytest.mark.parametrize("present", [False, True])
def test_project_sources_preserves_missing_and_regular_file_roots(tmp_path: Path, present: bool):
    root = tmp_path / "Main.lean"
    if present:
        root.write_text("import Init\n")

    assert _legacy_project_sources(root) == []
    assert project_sources(root) == []


def test_project_sources_preserves_nonfollowing_rglob_symlink_behavior(tmp_path: Path):
    root = tmp_path / "root"
    main = _write(root, "Main.lean")
    inside = _write(root, "src/Inside.lean")
    outside = tmp_path / "outside"
    _write(outside, "External.lean")
    (root / "Alias.lean").symlink_to(main)
    (root / "Missing.lean").symlink_to(root / "missing")
    (root / "src-alias").symlink_to(root / "src", target_is_directory=True)
    (root / "outside-alias").symlink_to(outside, target_is_directory=True)
    (root / "DirectoryAlias.lean").symlink_to(outside, target_is_directory=True)
    expected = sorted([main, inside])

    # Python 3.11/3.12 rglob neither traverses these directory links nor includes
    # the links themselves after the original final is_symlink filter.
    assert _legacy_project_sources(root) == expected
    assert project_sources(root) == expected


def test_project_sources_preserves_symlinked_root_without_following_child_aliases(tmp_path: Path):
    actual = tmp_path / "actual"
    _write(actual, "Main.lean")
    _write(actual, "src/Inside.lean")
    (actual / "src-alias").symlink_to(actual / "src", target_is_directory=True)
    root = tmp_path / "linked-root"
    root.symlink_to(actual, target_is_directory=True)
    expected = sorted([root / "Main.lean", root / "src/Inside.lean"])

    assert _legacy_project_sources(root) == expected
    assert project_sources(root) == expected


def test_project_sources_prunes_excluded_subtrees_before_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    expected = sorted([_write(root, "Main.lean"), _write(root, "src/deep/Other.lean")])
    for name in _EXCLUDED:
        _write(root, f"{name}/packages/deep/Hidden.lean")
        _write(root, f"src/{name}/packages/deep/Hidden.lean")
    expected.append(_write(root, "src/.github/Allowed.lean"))
    expected.sort()
    assert _legacy_project_sources(root) == expected
    actual_scandir = os.scandir
    scanned: list[Path] = []

    def record_scandir(
        path: str | os.PathLike[str],
    ) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        """Observe directory opens without timing or enumerating additional entries."""
        scanned.append(Path(path))
        return actual_scandir(path)

    monkeypatch.setattr(os, "scandir", record_scandir)
    assert project_sources(root) == expected
    assert scanned
    assert all(not _EXCLUDED.intersection(path.relative_to(root).parts) for path in scanned)
    assert root / "src/.github" in scanned


@pytest.mark.parametrize("code", [errno.EIO, errno.EACCES, errno.ENOENT])
def test_project_sources_refuses_partial_inventory_after_directory_scan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
):
    root = tmp_path / "root"
    _write(root, "Main.lean")
    hidden = _write(root, "src/Protected.lean")
    actual_scandir = os.scandir
    failure = OSError(code, "injected directory scan failure", str(hidden.parent))

    def fail_nested_scan(path):
        if Path(path) == hidden.parent:
            raise failure
        return actual_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_nested_scan)
    with pytest.raises(OSError) as error:
        project_sources(root)
    assert error.value is failure


def test_project_sources_preserves_root_stat_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    _write(root, "Main.lean")
    actual_stat = Path.stat
    failure = OSError(errno.EIO, "injected root stat failure", str(root))

    def fail_root_stat(path, *args, **kwargs):
        if path == root:
            raise failure
        return actual_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_root_stat)
    with pytest.raises(OSError) as error:
        project_sources(root)
    assert error.value is failure


@pytest.mark.parametrize("identity", [None, "directory-baseline"])
def test_delta_preserves_exact_directory_baseline_entries(tmp_path: Path, identity: str | None):
    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    _write(stage, ".lake/packages/pkg/New.olean", "new compiled output")
    baseline = {".lake/packages/pkg": identity}

    assert managed_delta(root, stage, baseline) == {".lake/packages/pkg/New.olean": None}


def test_delta_directory_membership_requires_a_component_boundary(tmp_path: Path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    _write(stage, ".lake/packages/pkg/New.olean", "new package")
    _write(stage, ".lake/packages/pkg2/Kept.olean", "changed existing package")
    baseline = {".lake/packages/pkg2/Kept.olean": "original-identity"}

    assert managed_delta(root, stage, baseline) == {
        ".lake/packages/pkg": None,
        ".lake/packages/pkg2/Kept.olean": "original-identity",
    }


def test_delta_retains_leaf_identity_and_ignores_absent_staged_entries(tmp_path: Path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    original = _write(root, ".lake/packages/pkg/Changed.olean", "original bytes")
    retained = _write(root, ".lake/packages/pkg/Retained.olean", "same bytes")
    absent = _write(root, ".lake/packages/pkg/Absent.olean", "not staged")
    baseline = {
        path.relative_to(root).as_posix(): entry_identity(path)
        for path in [original, retained, absent]
    }
    _write(stage, ".lake/packages/pkg/Changed.olean", "checked bytes")
    _write(stage, ".lake/packages/pkg/Retained.olean", "same bytes")
    _write(stage, ".lake/packages/pkg/.git/config", "never published")
    _write(stage, ".lake/packages/new/Output.olean", "new subtree")
    original.write_text("concurrent editor save")

    assert managed_delta(root, stage, baseline) == {
        ".lake/packages/pkg/Changed.olean": baseline[".lake/packages/pkg/Changed.olean"],
        ".lake/packages/new": None,
    }


class _CountedKey(str):
    """Count baseline prefix probes independently of filesystem speed."""

    startswith_calls: ClassVar[int] = 0

    def startswith(
        self, prefix: str | tuple[str, ...], start: int = 0, end: int | None = None
    ) -> bool:
        type(self).startswith_calls += 1
        return super().startswith(prefix, start, end)


def test_delta_indexes_baseline_prefixes_once_instead_of_scanning_per_directory(tmp_path: Path):
    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    baseline = {
        _CountedKey(f".lake/packages/unrelated/part-{index}/Keep.olean"): "retained"
        for index in range(4096)
    }
    expected = {}
    for index in range(64):
        relative = f".lake/packages/pkg-{index:03}/deep/Changed.olean"
        _write(stage, relative, "checked bytes")
        baseline[_CountedKey(relative)] = "original"
        expected[relative] = "original"
    _CountedKey.startswith_calls = 0

    assert managed_delta(root, stage, baseline) == expected
    # Permit a linear baseline-classification pass, but reject the old repeated
    # any(key.startswith(...)) scan over thousands of unrelated entries.
    assert _CountedKey.startswith_calls <= 2 * len(baseline)
