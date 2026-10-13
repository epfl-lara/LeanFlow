"""Protect live interpreters and command pointers during local upgrades."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from leanflow_cli.runtime import runtime_install as installer


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build small source inputs and an isolated home for installer tests."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("storage:\n  min_free_gib: 0\n", encoding="utf-8")
    root = tmp_path / "source"
    (root / "leanflow_cli").mkdir(parents=True)
    (root / "leanflow_cli" / "main.py").write_text("print('test')\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    return root


def _complete(plan: installer.InstallPlan, source: Path, bin_dir: Path) -> None:
    """Publish a tiny executable runtime without fetching dependencies."""
    (plan.venv_dir / "bin").mkdir(parents=True)
    for name in ("python", "leanflow"):
        path = plan.venv_dir / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    installer.finalize_install(source, plan.venv_dir, bin_dir)


def _shell_companion(tmp_path: Path) -> Path:
    """Supply compatible source metadata without reaching Git or package registries."""
    companion = tmp_path / "LeanProbe"
    (companion / "src/lean_probe").mkdir(parents=True)
    (companion / "pyproject.toml").write_text(
        '[project]\nname = "lean-probe"\nversion = "0.4.1.dev0"\n', encoding="utf-8"
    )
    (companion / "src/lean_probe/__init__.py").write_text("API_REVISION = 1\n", encoding="utf-8")
    return companion


def test_identity_uses_source_not_git_or_dependency_caches(source: Path) -> None:
    before = installer.source_identity(source)
    (source / ".lake").mkdir()
    (source / ".lake" / "large-cache").write_text("cache", encoding="utf-8")
    (source / "leanflow_cli" / "__pycache__").mkdir()
    (source / "leanflow_cli" / "__pycache__" / "x.pyc").write_bytes(b"bytecode")
    assert installer.source_identity(source) == before
    (source / "leanflow_cli" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    assert installer.source_identity(source) != before
    assert installer.source_identity(source, mode="wheel") != installer.source_identity(source)


def test_prepare_leaves_legacy_runtime_and_pointers_untouched(source: Path) -> None:
    home = source.parent / "home"
    legacy = home / "venv"
    legacy.mkdir(parents=True)
    (legacy / "proof-worker").write_text("live", encoding="utf-8")
    pointer = home / "install-root"
    pointer.write_text(f"venv_dir={legacy}\n", encoding="utf-8")
    plan = installer.prepare_install(source)
    assert plan.venv_dir == home / "runtimes" / plan.identity / "venv"
    assert not plan.reuse
    assert not plan.venv_dir.exists()
    assert (legacy / "proof-worker").read_text() == "live"
    assert pointer.read_text() == f"venv_dir={legacy}\n"
    marker = json.loads((plan.venv_dir.parent / ".leanflow-storage.json").read_text())
    assert marker["ready"] is False


def test_optional_search_has_its_own_reusable_runtime(source: Path) -> None:
    """Do not reuse a minimal environment when local ML dependencies are requested."""
    minimal = installer.prepare_install(source)
    _complete(minimal, source, source.parent / "bin")
    search = installer.prepare_install(source, local_search=True)
    assert search.venv_dir != minimal.venv_dir
    _complete(search, source, source.parent / "bin")
    assert installer.prepare_install(source, local_search=True).reuse
    assert installer.prepare_install(source).identity == minimal.identity


def test_completed_same_source_runtime_reused_without_mutation(source: Path) -> None:
    first = installer.prepare_install(source)
    _complete(first, source, source.parent / "bin")
    marker = first.venv_dir.parent / ".leanflow-storage.json"
    before = marker.read_bytes()
    second = installer.prepare_install(source)
    assert second.venv_dir == first.venv_dir
    assert second.reuse
    assert marker.read_bytes() == before


def test_recreate_prepares_sibling_without_deleting_old_environment(source: Path) -> None:
    first = installer.prepare_install(source)
    _complete(first, source, source.parent / "bin")
    saved = (first.venv_dir / "bin" / "python").read_bytes()
    second = installer.prepare_install(source, recreate=True)
    assert second.venv_dir != first.venv_dir
    assert not second.reuse
    assert (first.venv_dir / "bin" / "python").read_bytes() == saved


def test_explicit_live_venv_is_never_modified(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = source.parent / "running"
    explicit.mkdir()
    proof = explicit / "keep"
    proof.write_text("working", encoding="utf-8")
    monkeypatch.setattr(
        installer, "active_runtime_paths", lambda: (f"123 {explicit}/bin/python workflow prove",)
    )
    with pytest.raises(RuntimeError, match="is in use"):
        installer.prepare_install(source, explicit_venv=explicit, recreate=True)
    assert proof.read_text() == "working"


def test_explicit_idle_venv_still_requires_fresh_path(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = source.parent / "idle"
    explicit.mkdir()
    monkeypatch.setattr(installer, "active_runtime_paths", lambda: ())
    with pytest.raises(RuntimeError, match="fresh --venv-dir"):
        installer.prepare_install(source, explicit_venv=explicit, recreate=True)
    assert explicit.exists()


def test_process_inspection_failure_refuses_existing_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise OSError("ps unavailable")

    monkeypatch.setattr(installer.subprocess, "run", unavailable)
    with pytest.raises(RuntimeError, match="Cannot inspect active runtimes"):
        installer.active_runtime_paths()


def test_json_writer_available_before_python_dependencies(tmp_path: Path) -> None:
    """Keep install ownership persistence independent of site packages."""
    root = Path(__file__).resolve().parents[2]
    target = tmp_path / "marker.json"
    environment = dict(os.environ, PYTHONPATH=str(root))
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys; from core.utils import atomic_json_write; "
            "atomic_json_write(sys.argv[1], {'installed': False})",
            str(target),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text()) == {"installed": False}


def test_finalize_preserves_spaces_and_shell_characters(source: Path) -> None:
    plan = installer.prepare_install(source)
    bin_dir = source.parent / "bin $literal with spaces"
    _complete(plan, source, bin_dir)
    wrapper = bin_dir / "leanflow"
    assert subprocess.run([str(wrapper), "--help"], check=False).returncode == 0
    assert "OPENLEANFLOW_HOME" not in wrapper.read_text()
    pointer = (source.parent / "home" / "install-root").read_text()
    assert f"venv_dir={plan.venv_dir}\n" in pointer


def test_metadata_failure_restores_previous_wrapper(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = installer.prepare_install(source)
    _complete(plan, source, source.parent / "bin")
    wrapper = source.parent / "bin" / "leanflow"
    wrapper.write_text("#!/bin/sh\n# old runtime\n", encoding="utf-8")
    original = installer._atomic_text

    def fail_metadata(path: Path, text: str, *, executable: bool = False) -> None:
        if path.name == "install-root":
            raise OSError("disk full")
        original(path, text, executable=executable)

    monkeypatch.setattr(installer, "_atomic_text", fail_metadata)
    with pytest.raises(OSError, match="disk full"):
        installer.finalize_install(source, plan.venv_dir, source.parent / "bin")
    assert wrapper.read_text() == "#!/bin/sh\n# old runtime\n"


def test_cli_runtime_lease_pins_interpreter_and_source_release(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.runtime import storage

    home = source.parent / "home"
    runtime = home / "runtimes" / "runtime"
    release = home / "releases" / "release"
    storage.register_entry(runtime, kind="runtime")
    storage.register_entry(release, kind="runtime-release")
    monkeypatch.setattr(installer.sys, "prefix", str(runtime / "venv"))
    monkeypatch.setattr(
        installer, "__file__", str(release / "source" / "leanflow_cli" / "runtime" / "x.py")
    )
    active: list[Path] = []

    @contextmanager
    def lease(path: Path, home: Path | None = None) -> Iterator[None]:
        assert home == source.parent / "home"
        active.append(path)
        try:
            yield
        finally:
            active.remove(path)

    monkeypatch.setattr(storage, "lease_entry", lease)
    with installer.running_runtime_lease():
        assert active == [runtime, release]
    assert active == []


def test_runtime_lease_supports_a_different_run_home(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.runtime import storage

    original_home = source.parent / "home"
    runtime = original_home / "runtimes" / "runtime"
    storage.register_entry(runtime, kind="runtime")
    monkeypatch.setattr(installer.sys, "prefix", str(runtime / "venv"))
    monkeypatch.setenv("LEANFLOW_HOME", str(source.parent / "experiment-home"))
    with installer.running_runtime_lease():
        assert storage.leases.entry_is_leased(runtime, original_home)
    assert not storage.leases.entry_is_leased(runtime, original_home)


def test_healthy_mcp_backends_reused_without_install(source: Path, monkeypatch: pytest.MonkeyPatch):
    from leanflow_cli.cli import mcp_bootstrap

    monkeypatch.setattr(
        mcp_bootstrap,
        "managed_mcp_server_status",
        lambda: {"lean": {"healthy": True, "venv_dir": "/fixture/lean"}},
    )
    monkeypatch.setattr(installer, "mcp_protocol_ready", lambda _: True)

    def unexpected() -> None:
        raise AssertionError("must not upgrade an existing backend")

    monkeypatch.setattr(mcp_bootstrap, "bootstrap_lean_mcp", unexpected)
    installer.ensure_backends()


def test_unhealthy_active_mcp_backend_is_replaced_without_modification(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.cli import mcp_bootstrap

    replaced: list[bool] = []
    monkeypatch.setattr(mcp_bootstrap, "bootstrap_lean_mcp", lambda: replaced.append(True))
    monkeypatch.setattr(
        mcp_bootstrap,
        "managed_mcp_server_status",
        lambda: {"lean": {"healthy": bool(replaced), "venv_dir": "/fresh/lean"}},
    )
    monkeypatch.setattr(installer, "mcp_protocol_ready", lambda _: True)
    installer.ensure_backends()
    assert replaced == [True]


def test_present_backend_with_incompatible_protocol_is_repaired(source: Path, monkeypatch) -> None:
    """Do not treat an existing executable as proof that its server API imports."""
    from leanflow_cli.cli import mcp_bootstrap

    monkeypatch.setattr(
        mcp_bootstrap,
        "managed_mcp_server_status",
        lambda: {"lean": {"healthy": True, "venv_dir": "/fixture/lean"}},
    )
    repaired = []
    monkeypatch.setattr(installer, "mcp_protocol_ready", lambda _: bool(repaired))
    monkeypatch.setattr(mcp_bootstrap, "bootstrap_lean_mcp", lambda: repaired.append(True))
    installer.ensure_backends()
    assert repaired == [True]


def test_failed_new_mcp_protocol_blocks_publication(source: Path, monkeypatch) -> None:
    from leanflow_cli.cli import mcp_bootstrap

    monkeypatch.setattr(mcp_bootstrap, "bootstrap_lean_mcp", lambda: None)
    monkeypatch.setattr(
        mcp_bootstrap,
        "managed_mcp_server_status",
        lambda: {"lean": {"healthy": True, "venv_dir": "/broken/lean"}},
    )
    monkeypatch.setattr(installer, "mcp_protocol_ready", lambda _: False)
    with pytest.raises(RuntimeError, match="did not produce validated"):
        installer.ensure_backends()


def test_storage_registration_failure_keeps_previous_commands(source: Path, monkeypatch) -> None:
    from leanflow_cli.runtime import storage

    first = installer.prepare_install(source)
    _complete(first, source, source.parent / "bin")
    wrapper = source.parent / "bin/leanflow"
    pointer = source.parent / "home/install-root"
    before = (wrapper.read_bytes(), pointer.read_bytes())
    second = installer.prepare_install(source, recreate=True)
    (second.venv_dir / "bin").mkdir(parents=True)
    (second.venv_dir / "bin/leanflow").write_text("#!/bin/sh\nexit 0\n")

    def fail(*args, **kwargs):
        raise OSError("registration failed")

    monkeypatch.setattr(storage, "register_entry", fail)
    with pytest.raises(OSError, match="registration failed"):
        installer.finalize_install(source, second.venv_dir, source.parent / "bin")
    assert (wrapper.read_bytes(), pointer.read_bytes()) == before


def test_source_changes_during_install_keep_previous_commands(source: Path) -> None:
    first = installer.prepare_install(source)
    _complete(first, source, source.parent / "bin")
    wrapper = source.parent / "bin/leanflow"
    pointer = source.parent / "home/install-root"
    before = (wrapper.read_bytes(), pointer.read_bytes())
    second = installer.prepare_install(source, recreate=True)
    (second.venv_dir / "bin").mkdir(parents=True)
    (second.venv_dir / "bin/leanflow").write_text("#!/bin/sh\nexit 0\n")
    (source / "leanflow_cli/main.py").write_text("changed during package install\n")
    with pytest.raises(RuntimeError, match="Source inputs changed"):
        installer.finalize_install(source, second.venv_dir, source.parent / "bin")
    assert (wrapper.read_bytes(), pointer.read_bytes()) == before


def test_failed_shell_install_never_switches_existing_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real shell installer stopping at failed environment creation."""
    root = Path(__file__).resolve().parents[2]
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    (home / "config.yaml").write_text("storage:\n  min_free_gib: 0\n", encoding="utf-8")
    bin_dir.mkdir()
    (home / "install-root").write_text("old metadata\n", encoding="utf-8")
    (bin_dir / "leanflow").write_text("old wrapper\n", encoding="utf-8")
    python = tmp_path / "fake-python"
    python.write_text(
        '#!/bin/bash\nif [[ "$1" == "-c" ]]; then exit 0; fi\n'
        'if [[ "$1" == "-m" && "$2" == "venv" ]]; then exit 42; fi\n'
        f'exec {shlex.quote(sys.executable)} -S "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    environment = dict(os.environ)
    environment.update(
        LEANFLOW_HOME=str(home),
        LEANFLOW_BIN_DIR=str(bin_dir),
        LEANFLOW_INSTALL_PYTHON=str(python),
    )
    environment.pop("LEANFLOW_VENV_DIR", None)
    result = subprocess.run(
        [
            str(root / "scripts" / "install-internal.sh"),
            "--skip-os-tools",
            "--skip-leanexplore-data",
            "--lean-probe-source",
            str(_shell_companion(tmp_path)),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 42, result.stderr
    assert (home / "install-root").read_text() == "old metadata\n"
    assert (bin_dir / "leanflow").read_text() == "old wrapper\n"
    markers = list((home / "runtimes").glob("*/.leanflow-storage.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text())["ready"] is False


def test_shell_install_skips_old_default_python_before_importing_code(tmp_path: Path) -> None:
    """Select a supported interpreter when macOS python3 still resolves to 3.9."""
    root = Path(__file__).resolve().parents[2]
    probes = tmp_path / "python-probes"
    probes.mkdir()
    old_invoked = tmp_path / "old-helper-invoked"
    supported_invoked = tmp_path / "supported-helper-invoked"
    old = probes / "python3"
    old.write_text(
        '#!/bin/bash\nif [[ "$1" == "-c" ]]; then exit 1; fi\n'
        f"touch {shlex.quote(str(old_invoked))}\nexit 99\n",
        encoding="utf-8",
    )
    old.chmod(0o755)
    supported = probes / "python3.13"
    supported.write_text(
        '#!/bin/bash\nif [[ "$1" == "-c" ]]; then exit 0; fi\n'
        'if [[ "$1" == "-m" && "$2" == "venv" ]]; then exit 42; fi\n'
        'if [[ "$1" == "-m" ]]; then '
        f"touch {shlex.quote(str(supported_invoked))}; fi\n"
        f'exec {shlex.quote(sys.executable)} -S "$@"\n',
        encoding="utf-8",
    )
    supported.chmod(0o755)
    environment = dict(os.environ)
    environment.update(
        PATH=f"{probes}:{environment['PATH']}",
        LEANFLOW_HOME=str(tmp_path / "home"),
        LEANFLOW_BIN_DIR=str(tmp_path / "bin"),
    )
    environment.pop("LEANFLOW_INSTALL_PYTHON", None)
    environment.pop("LEANFLOW_VENV_DIR", None)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("storage:\n  min_free_gib: 0\n", encoding="utf-8")
    result = subprocess.run(
        [
            str(root / "scripts/install-internal.sh"),
            "--skip-os-tools",
            "--skip-leanexplore-data",
            "--lean-probe-source",
            str(_shell_companion(tmp_path)),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 42, result.stderr
    assert not old_invoked.exists()
    assert supported_invoked.exists()
    assert "Python: python3.13" in result.stdout


@pytest.mark.parametrize("via_environment", [False, True])
def test_explicit_old_python_fails_before_installation(
    tmp_path: Path, via_environment: bool
) -> None:
    """Give a clear error for an explicitly requested unsupported interpreter."""
    root = Path(__file__).resolve().parents[2]
    helper_invoked = tmp_path / "helper-invoked"
    old = tmp_path / "python3.9"
    old.write_text(
        '#!/bin/bash\nif [[ "$1" == "-c" ]]; then exit 1; fi\n'
        f"touch {shlex.quote(str(helper_invoked))}\nexit 99\n",
        encoding="utf-8",
    )
    old.chmod(0o755)
    home = tmp_path / "home"
    environment = dict(os.environ)
    environment.update(LEANFLOW_HOME=str(home), LEANFLOW_BIN_DIR=str(tmp_path / "bin"))
    arguments = [str(root / "scripts/install-internal.sh"), "--skip-os-tools"]
    if via_environment:
        environment["LEANFLOW_INSTALL_PYTHON"] = str(old)
    else:
        environment.pop("LEANFLOW_INSTALL_PYTHON", None)
        arguments.extend(["--python", str(old)])
    result = subprocess.run(
        arguments,
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "Python 3.12+ with venv support" in result.stderr
    assert "explicitly selected interpreter" in result.stderr
    assert not helper_invoked.exists()
    assert not home.exists()
