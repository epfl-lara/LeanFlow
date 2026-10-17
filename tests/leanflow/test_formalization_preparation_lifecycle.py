"""Keep private setup paths on uncertain cleanup and dispose only owned stopped inputs."""

import pytest

from leanflow_cli.formalization.preparation_lifecycle import private_stage


def test_verified_private_stage_disposal_preserves_canonical_and_link_targets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    original = root / "Main.lean"
    original.write_text("import Init\n")
    with private_stage(root) as lease:
        lease.require_cleanup()
        (lease.path / "cache").write_bytes(b"private cache")
        (lease.path / "link").symlink_to(original)
        lease.verify_cleanup({"cleanup_verified": True})
    assert not lease.directory.exists()
    assert original.read_text() == "import Init\n"


def test_unverified_cleanup_retains_private_inputs_after_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(RuntimeError, match="uncertain process"):
        with private_stage(root) as lease:
            lease.require_cleanup()
            (lease.path / "cache").write_bytes(b"still in use")
            raise RuntimeError("uncertain process")
    assert (lease.path / "cache").read_bytes() == b"still in use"
    assert (lease.directory / "owner.json").is_file()


def test_replaced_owner_marker_cannot_authorize_disposal(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(ValueError, match="ownership changed"):
        with private_stage(root) as lease:
            (lease.directory / "owner.json").write_text("{}")
    assert lease.path.exists()


def test_checked_private_compiler_copy_is_released_before_later_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    with private_stage(root) as lease:
        inputs = lease.path / ".leanflow-toolchain-private"
        inputs.mkdir()
        (inputs / "compiler").write_bytes(b"installed compiler input")
        lease.require_cleanup()
        with pytest.raises(ValueError, match="verified private ownership"):
            lease.dispose_compiler_inputs()
        assert inputs.exists()
        lease.verify_cleanup({"cleanup_verified": True})
        lease.dispose_compiler_inputs()
        assert not inputs.exists()


def test_private_stage_requires_state_outside_project(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    with pytest.raises(ValueError, match="disjoint private"):
        with private_stage(tmp_path):
            pytest.fail("A stage inside the canonical project must not be admitted")


def test_verified_cleanup_disposes_readonly_private_dependency_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    with private_stage(root) as lease:
        readonly = lease.path / "package/library"
        readonly.mkdir(parents=True)
        (readonly / "Cache.olean").write_bytes(b"private dependency cache")
        readonly.chmod(0o555)
        readonly.parent.chmod(0o555)
    assert not lease.directory.exists()


def test_precommit_marker_requires_observed_cleanup_and_binds_exact_stage(tmp_path, monkeypatch):
    from leanflow_cli.formalization.evidence import identity

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    with private_stage(root) as lease:
        with pytest.raises(ValueError, match="verified original controller"):
            lease.precommit_cleanup()
        lease.require_cleanup()
        lease.verify_cleanup({"cleanup_verified": True})
        marker = lease.precommit_cleanup()
        assert marker["phase"] == "PRECOMMIT" and marker["cleanup_verified"] is True
        assert marker["owner"] == {"pid": lease.owner["pid"], "start": lease.owner["start"]}
        assert marker["stage"] == {
            "path": str(lease.path),
            "device": lease.path.stat().st_dev,
            "inode": lease.path.stat().st_ino,
        }
        assert marker["marker_id"] == identity(
            {key: value for key, value in marker.items() if key != "marker_id"}
        )
        lease.require_cleanup()
        with pytest.raises(ValueError, match="verified original controller"):
            lease.precommit_cleanup()
        lease.verify_cleanup({"cleanup_verified": True})
