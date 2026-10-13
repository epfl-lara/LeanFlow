"""Keep optional native builds out of ordinary Codex and Claude installations."""

import subprocess

import pytest

from leanflow_cli.runtime import install_native
from leanflow_cli.runtime.lemex_install import LemexInstallError


@pytest.mark.parametrize(
    "provider,requested", [("codex", False), ("claude", False), ("lemex", True), ("codex", True)]
)
def test_native_build_requires_explicit_option_or_selected_lemex(monkeypatch, provider, requested):
    calls = []
    monkeypatch.setattr(install_native, "load_config", lambda: {"model": {"provider": provider}})

    def ensure(**kwargs):
        calls.append(kwargs)
        raise LemexInstallError("No compatible Lemex installation found")

    monkeypatch.setattr(install_native, "ensure_lemex", ensure)
    if provider == "lemex" or requested:
        with pytest.raises(LemexInstallError):
            install_native.install_native_provider(with_lemex=requested)
    else:
        assert not install_native.install_native_provider(with_lemex=requested)
    assert calls == [{"build": provider == "lemex" or requested, "refresh": True}]


def test_existing_native_package_is_adopted_without_build(monkeypatch):
    monkeypatch.setattr(install_native, "load_config", lambda: {"model": {"provider": "codex"}})

    def ensure(**kwargs):
        assert kwargs == {"build": False, "refresh": True}
        return {"executable": "lemex", "source": "adopted", "version": "compatible"}

    monkeypatch.setattr(install_native, "ensure_lemex", ensure)
    assert install_native.install_native_provider()


@pytest.mark.parametrize("provider", ["codex", "claude", "lemex"])
@pytest.mark.parametrize(
    "error", [OSError("unreadable package"), subprocess.CalledProcessError(1, "probe")]
)
def test_optional_native_adoption_failure_does_not_abort_other_providers(
    monkeypatch, provider, error
):
    """Propagate mandatory native failures while keeping other provider installations usable."""
    monkeypatch.setattr(install_native, "load_config", lambda: {"model": {"provider": provider}})

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(install_native, "ensure_lemex", fail)
    if provider == "lemex":
        with pytest.raises(type(error)):
            install_native.install_native_provider()
    else:
        assert not install_native.install_native_provider()
