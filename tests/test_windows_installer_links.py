from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_windows_installers_do_not_reference_legacy_repositories():
    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    install_cmd = (REPO_ROOT / "scripts" / "install.cmd").read_text(encoding="utf-8")

    for source in (install_ps1, install_cmd):
        assert "morph-labs/gauss-agent" not in source
        assert "NousResearch/gauss-agent" not in source


def test_windows_installers_use_current_bootstrap_targets():
    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    install_cmd = (REPO_ROOT / "scripts" / "install.cmd").read_text(encoding="utf-8")

    assert '$DefaultRepoUrl = "https://github.com/math-inc/OpenGauss.git"' in install_ps1
    assert 'powershell -ExecutionPolicy ByPass -NoProfile -File "%~dp0install.ps1" %*' in install_cmd
