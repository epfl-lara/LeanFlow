"""User-facing branding helpers for the LeanFlow shell."""

from __future__ import annotations

import os

PRODUCT_NAME = "LeanFlow"
CLI_NAME = "leanflow"
PRODUCT_TAGLINE = "Lean-first AI automation"
PRODUCT_SUBTITLE = "Verified proving and mathematical formalization for Lean 4"
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


def get_cli_command_name(default: str = "leanflow") -> str:
    return os.getenv("LEANFLOW_CLI_NAME", default or CLI_NAME).strip() or CLI_NAME


def get_product_name() -> str:
    return PRODUCT_NAME


def get_product_tagline() -> str:
    return PRODUCT_TAGLINE


def get_product_subtitle() -> str:
    return PRODUCT_SUBTITLE
