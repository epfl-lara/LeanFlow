"""Honor configured storage limits before third-party dependencies are installed."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.runtime.install_storage import bootstrap_storage_policy


def test_bootstrap_policy_without_site_packages(tmp_path):
    """Read ordinary numeric settings with a genuinely dependency-free interpreter."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "model:\n  provider: codex\nstorage:\n  min_free_gib: 5\n  automatic_cleanup: false\n"
    )
    root = Path(__file__).resolve().parents[2]
    code = "from leanflow_cli.runtime.install_storage import bootstrap_storage_policy; from pathlib import Path; import sys,json; p=bootstrap_storage_policy(Path(sys.argv[1])); print(json.dumps([p.min_free_gib,p.automatic_cleanup]))"
    result = subprocess.run(
        [sys.executable, "-S", "-c", code, str(config)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [5, False]


@pytest.mark.parametrize(
    "section",
    [
        "storage: {min_free_gib: 5}",
        "storage:\n  min_free_gib: nan",
        "storage:\n  min_free_gib: -1",
        "storage:\n  min_free_gib: 5\nother: yes\nstorage:\n  min_free_gib: 0",
    ],
)
def test_ambiguous_or_invalid_bootstrap_settings_fail(tmp_path, section):
    path = tmp_path / "config.yaml"
    path.write_text(section)
    with pytest.raises(ValueError):
        bootstrap_storage_policy(path)
