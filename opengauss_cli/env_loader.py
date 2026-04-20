"""Helpers for loading OpenGauss environment files consistently."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from opengauss_cli.config import get_opengauss_home


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding="latin-1")


def load_opengauss_dotenv(
    *,
    opengauss_home: str | Path | None = None,
    gauss_home: str | Path | None = None,
    project_env: str | Path | None = None,
) -> list[Path]:
    loaded: list[Path] = []

    resolved_home = opengauss_home if opengauss_home is not None else gauss_home
    home_path = Path(resolved_home) if resolved_home else get_opengauss_home()
    user_env = home_path / ".env"
    project_env_path = Path(project_env) if project_env else None

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True)
        loaded.append(user_env)

    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=not loaded)
        loaded.append(project_env_path)

    return loaded
