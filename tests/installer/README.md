# Installer verification

The Python tests in this directory check command branding and packaged runtime
resources. Run them with the wrapper and Windows bootstrap tests:

```bash
source .venv/bin/activate
python -m pytest tests/installer tests/test_install_wrapper.py tests/test_windows_installer_links.py -q -n 0
```

The `ubuntu_repository_local_install_smoke` scenario runs the direct local
installer in an Ubuntu container. It verifies command wrappers, configuration,
managed MCP setup, bundled skills/specs, and configuration preservation on
reinstall. It needs Docker and network access for dependency downloads.
