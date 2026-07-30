from pathlib import Path

import pytest

from core.filesystem import ensure_directory


def test_ensure_directory_retries_transient_file_exists(monkeypatch, tmp_path):
    target = tmp_path / "shared" / "child"
    original_mkdir = Path.mkdir
    calls = 0

    def flaky_mkdir(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileExistsError(17, "File exists", str(tmp_path / "shared"))
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    monkeypatch.setattr("core.filesystem.time.sleep", lambda _seconds: None)

    assert ensure_directory(target) == target
    assert target.is_dir()
    assert calls >= 2


def test_ensure_directory_preserves_real_file_collision(tmp_path):
    target = tmp_path / "occupied"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        ensure_directory(target)
