"""Regex-based secret redaction for logs and tool output.

Applies pattern matching to mask API keys, tokens, and credentials
before they reach log files, verbose output, or gateway logs.

All matched credential values are fully masked. Prefixes and suffixes remain
credential material and therefore never survive redaction.
"""

import logging
import os
import re
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Known API key prefixes -- match the prefix + contiguous token chars
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",  # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",  # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",  # GitHub PAT (fine-grained)
    r"xox[baprs]-[A-Za-z0-9-]{10,}",  # Slack tokens
    r"AIza[A-Za-z0-9_-]{30,}",  # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",  # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",  # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",  # Firecrawl
    r"bb_live_[A-Za-z0-9_-]{10,}",  # BrowserBase
    r"gAAAA[A-Za-z0-9_=-]{20,}",  # Codex encrypted tokens
    r"AKIA[A-Z0-9]{16}",  # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",  # Stripe secret key (live)
    r"sk_test_[A-Za-z0-9]{10,}",  # Stripe secret key (test)
    r"rk_live_[A-Za-z0-9]{10,}",  # Stripe restricted key
    r"SG\.[A-Za-z0-9_-]{10,}",  # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",  # HuggingFace token
    r"r8_[A-Za-z0-9]{10,}",  # Replicate API token
    r"npm_[A-Za-z0-9]{10,}",  # npm access token
    r"pypi-[A-Za-z0-9_-]{10,}",  # PyPI API token
    r"dop_v1_[A-Za-z0-9]{10,}",  # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",  # DigitalOcean OAuth
    r"am_[A-Za-z0-9_-]{10,}",  # AgentMail API key
]

# ENV assignment patterns: KEY=value where KEY contains a secret-like name
_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z_]*{_SECRET_ENV_NAMES}[A-Z_]*)\s*=\s*(['\"]?)(\S+)\2",
    re.IGNORECASE,
)

# JSON field patterns: "apiKey": "value", "token": "value", etc.
_JSON_KEY_NAMES = r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer|secret_value|raw_secret|secret_input|key_material)"
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)

# Authorization headers
_AUTH_HEADER_RE = re.compile(
    r"(Authorization:\s*Bearer\s+)(\S+)",
    re.IGNORECASE,
)

# Telegram bot tokens: bot<digits>:<token> or <digits>:<token>,
# where token part is restricted to [-A-Za-z0-9_] and length >= 30
_TELEGRAM_RE = re.compile(
    r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})",
)

# Private key blocks: -----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# Database connection strings: protocol://user:PASSWORD@host
# Catches postgres, mysql, mongodb, redis, amqp URLs and redacts the password
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)

# E.164 phone numbers: +<country><number>, 7-15 digits
# Negative lookahead prevents matching hex strings or identifiers
_SIGNAL_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")

# Compile known prefix patterns into one alternation
_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])")
_JWT_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9_-]\.)"
    r"eyJ[A-Za-z0-9_-]{13,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])"
)


def _mask_token(token: str) -> str:
    """Mask a token without retaining any credential-derived text."""
    del token
    return "***"


def redact_sensitive_text(text: str) -> str:
    """Apply all redaction patterns to a block of text.

    Safe to call on any string -- non-matching text passes through unchanged.
    Disabled when security.redact_secrets is false in config.yaml.
    """
    if not text:
        return text
    if os.getenv("LEANFLOW_REDACT_SECRETS", "").lower() in ("0", "false", "no", "off"):
        return text

    # Raw JWTs may arrive without an Authorization or named-field label.
    text = _JWT_CREDENTIAL_RE.sub("***", text)

    # Known prefixes (sk-, ghp_, etc.)
    text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)

    # ENV assignments: OPENAI_API_KEY=sk-abc...
    def _redact_env(m):
        name, quote, value = m.group(1), m.group(2), m.group(3)
        return f"{name}={quote}{_mask_token(value)}{quote}"

    text = _ENV_ASSIGN_RE.sub(_redact_env, text)

    # JSON fields: "apiKey": "value"
    def _redact_json(m):
        key, value = m.group(1), m.group(2)
        return f'{key}: "{_mask_token(value)}"'

    text = _JSON_FIELD_RE.sub(_redact_json, text)

    # Authorization headers
    text = _AUTH_HEADER_RE.sub(
        lambda m: m.group(1) + _mask_token(m.group(2)),
        text,
    )

    # Telegram bot tokens are single credentials; retain no bot-id prefix.
    text = _TELEGRAM_RE.sub("[REDACTED]", text)

    # Private key blocks
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)

    # Database connection string passwords
    text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)

    # E.164 phone numbers (Signal, WhatsApp)
    def _redact_phone(m):
        phone = m.group(1)
        if len(phone) <= 8:
            return phone[:2] + "****" + phone[-2:]
        return phone[:4] + "****" + phone[-4:]

    text = _SIGNAL_PHONE_RE.sub(_redact_phone, text)

    return text


def redact_sensitive_value(
    value: Any,
    *,
    exact_secrets: Sequence[str] = (),
) -> Any:
    """Recursively redact structured log data and exact caller-owned secrets.

    Exact-secret replacement is unconditional so a credential explicitly owned
    by the caller cannot leak even when optional pattern redaction is disabled.
    """
    secrets = tuple(secret for secret in exact_secrets if isinstance(secret, str) and secret)
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {
            key: redact_sensitive_value(item, exact_secrets=secrets) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_value(item, exact_secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(item, exact_secrets=secrets) for item in value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive_value(str(value), exact_secrets=secrets)


class RedactingFormatter(logging.Formatter):
    """Log formatter that redacts secrets from all log messages."""

    def __init__(self, fmt=None, datefmt=None, style="%", **kwargs):
        super().__init__(fmt, datefmt, style, **kwargs)

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_sensitive_text(original)
