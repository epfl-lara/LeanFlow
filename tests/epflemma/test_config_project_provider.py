from __future__ import annotations

import yaml

from epflemma_cli.config import get_config_path, load_config
from epflemma_cli.project import discover_epflemma_project, initialize_epflemma_project
from epflemma_cli.runtime_provider import RuntimeProviderError, resolve_runtime_provider


def test_load_config_imports_legacy_gauss_payload(monkeypatch, tmp_path):
    legacy_home = tmp_path / "legacy-gauss"
    legacy_home.mkdir()
    monkeypatch.setenv("GAUSS_HOME", str(legacy_home))
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "open-home"))

    (legacy_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "gauss": {
                    "project": {"template_source": "legacy-template"},
                    "autoformalize": {"managed_state_dir": ".gauss/workflow-state"},
                },
                "model": {"provider": "zai", "default": "zai-org/GLM-5"},
                "toolsets": ["gauss-native"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (legacy_home / ".env").write_text("GLM_API_KEY=legacy-key\n", encoding="utf-8")

    config = load_config()

    assert config["epflemma"]["project"]["template_source"] == "legacy-template"
    assert config["epflemma"]["workflow"]["managed_state_dir"] == ".gauss/workflow-state"
    assert config["toolsets"] == ["epflemma-native"]
    assert get_config_path().exists()


def test_initialize_project_uses_epflemma_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")

    project = initialize_epflemma_project(root)
    loaded = discover_epflemma_project(root)

    assert project.manifest_path == root / ".epflemma" / "project.yaml"
    assert loaded.manifest_path == project.manifest_path
    assert not (root / ".gauss" / "project.yaml").exists()


def test_initialize_project_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")

    project = initialize_epflemma_project(root)
    manifest_before = project.manifest_path.read_text(encoding="utf-8")

    reloaded = initialize_epflemma_project(root)
    manifest_after = project.manifest_path.read_text(encoding="utf-8")

    assert reloaded.manifest_path == project.manifest_path
    assert manifest_after == manifest_before


def test_discover_project_imports_legacy_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    root = tmp_path / "LegacyProject"
    (root / ".gauss").mkdir(parents=True)
    (root / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    (root / ".gauss" / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "legacy-demo",
                "kind": "lean4",
                "lean_root": ".",
                "paths": {
                    "runtime": ".gauss/runtime",
                    "cache": ".gauss/cache",
                    "workflows": ".gauss/workflows",
                },
                "blueprint": {"markers": ["lean-toolchain"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    project = discover_epflemma_project(root)

    assert project.name == "legacy-demo"
    assert project.manifest_path == root / ".epflemma" / "project.yaml"
    assert project.runtime_dir == root / ".epflemma" / "runtime"


def test_runtime_provider_resolves_direct_custom_and_local(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.delenv("EPFLEMMA_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("EPFLEMMA_OPENAI_API_KEY", raising=False)

    direct = resolve_runtime_provider(requested="zai")
    assert direct["provider"] == "zai"
    assert direct["api_key"] == "glm-key"

    monkeypatch.setenv("OPENAI_BASE_URL", "https://rcp.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "rcp-key")
    custom = resolve_runtime_provider(requested="custom")
    assert custom["provider"] == "custom"
    assert custom["base_url"] == "https://rcp.example/v1"

    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "local", "default": "google/gemma-4-31B-it"},
                "local_models": {
                    "default_runtime": "vllm",
                    "active_runtime": "vllm",
                    "active_model": "google/gemma-4-31B-it",
                    "runtimes": {"vllm": {"host": "127.0.0.1", "port": 8000, "extra_args": []}},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    local = resolve_runtime_provider(requested="local")
    assert local["provider"] == "local"
    assert local["base_url"] == "http://127.0.0.1:8000/v1"


def test_runtime_provider_prefers_epflemma_scoped_openrouter_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_OPENROUTER_API_KEY", "epflemma-or-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "global-or-key")
    monkeypatch.setenv("OPENAI_API_KEY", "global-openai-key")
    monkeypatch.delenv("EPFLEMMA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("EPFLEMMA_OPENAI_BASE_URL", raising=False)

    resolved = resolve_runtime_provider(requested="openrouter")

    assert resolved["provider"] == "openrouter"
    assert resolved["api_key"] == "epflemma-or-key"


def test_runtime_provider_prefers_epflemma_scoped_custom_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_OPENAI_BASE_URL", "https://rcp.epfl.example/v1")
    monkeypatch.setenv("EPFLEMMA_OPENAI_API_KEY", "epflemma-rcp-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "global-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "global-or-key")

    resolved = resolve_runtime_provider(requested="custom")

    assert resolved["provider"] == "custom"
    assert resolved["base_url"] == "https://rcp.epfl.example/v1"
    assert resolved["api_key"] == "epflemma-rcp-key"


def test_runtime_provider_rejects_placeholder_custom_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_OPENAI_BASE_URL", "https://your-rcp-endpoint/v1")
    monkeypatch.setenv("EPFLEMMA_OPENAI_API_KEY", "epflemma-rcp-key")

    try:
        resolve_runtime_provider(requested="custom")
    except RuntimeProviderError as exc:
        assert "your-rcp-endpoint" in str(exc)
    else:
        raise AssertionError("expected RuntimeProviderError for placeholder base URL")
