"""Helpers for loading EPFLemma environment files consistently."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from epflemma_cli.config import get_epflemma_home


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding="latin-1")


def load_epflemma_dotenv(
    *,
    epflemma_home: str | Path | None = None,
    project_env: str | Path | None = None,
) -> list[Path]:
    loaded: list[Path] = []

    home_path = Path(epflemma_home) if epflemma_home else get_epflemma_home()
    user_env = home_path / ".env"
    project_env_path = Path(project_env) if project_env else None

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True)
        loaded.append(user_env)

    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=not loaded)
        loaded.append(project_env_path)

    return loaded
