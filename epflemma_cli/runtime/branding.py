"""User-facing branding helpers for the EPFLemma shell."""

from __future__ import annotations

import os
from pathlib import Path

PRODUCT_NAME = "EPFLemma"
CLI_NAME = "epflemma"
PRODUCT_TAGLINE = "AI for Math"
PRODUCT_SUBTITLE = "EPFL-inspired terminal kernel for Lean proving and formalization"
BRAND_NOTE = "EPFL-inspired visual direction only; not an official EPFL product"

# Swiss-red-first terminal palette inspired by EPFL's public brand guidance.
BRAND_COLORS = {
    "primary": "#FF0000",
    "primary_soft": "#FF5A5F",
    "primary_dim": "#B22222",
    "text": "#F5F5F5",
    "muted": "#D9D9D9",
    "panel": "#8F1D21",
}


def get_cli_command_name(default: str = "epflemma") -> str:
    return os.getenv("EPFLEMMA_CLI_NAME", default or CLI_NAME).strip() or CLI_NAME


def get_product_name() -> str:
    return PRODUCT_NAME


def get_product_tagline() -> str:
    return PRODUCT_TAGLINE


def get_product_subtitle() -> str:
    return PRODUCT_SUBTITLE
