"""Types describing one declared ``LEANFLOW_*`` runtime knob."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: How a knob's raw environment string is interpreted by the module that reads it.
FlagValueType = Literal["bool", "int", "float", "string", "enum", "path", "csv"]

#: What role a knob plays, which decides how a UI should present it.
#:
#: ``feature``  — a behavior switch: the run does something different when it flips.
#: ``tuning``   — a budget, timeout, limit, or capacity that shapes an existing behavior.
#: ``runtime``  — an operational/environment concern (memory mode, redaction, transports).
#: ``internal`` — plumbing the launcher sets on the child process; surfaced read-only.
FlagKind = Literal["feature", "tuning", "runtime", "internal"]

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

#: ``Literal`` is erased at runtime, so the permitted values are restated here
#: and checked on construction. Without this a typo in ``kind`` type-checks and
#: only surfaces later as a rendering or filtering bug.
_KINDS = frozenset({"feature", "tuning", "runtime", "internal"})
_VALUE_TYPES = frozenset({"bool", "int", "float", "string", "enum", "path", "csv"})


@dataclass(frozen=True)
class FlagSpec:
    """One declared runtime knob.

    ``default`` is the literal string the reading module falls back to when the
    variable is absent, so a UI can show the real effective baseline rather than
    an empty field. ``read_in`` records provenance: the modules that consult the
    name, which is what the drift test checks against.
    """

    name: str
    kind: FlagKind
    value_type: FlagValueType
    default: str
    group: str
    summary: str
    read_in: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()
    ablatable: bool = False
    sensitive: bool = False
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if not self.name.startswith("LEANFLOW_"):
            raise ValueError(f"flag name must start with LEANFLOW_: {self.name!r}")
        if self.kind not in _KINDS:
            raise ValueError(
                f"{self.name}: kind must be one of {sorted(_KINDS)}, got {self.kind!r}"
            )
        if self.value_type not in _VALUE_TYPES:
            raise ValueError(
                f"{self.name}: value_type must be one of {sorted(_VALUE_TYPES)}, "
                f"got {self.value_type!r}"
            )
        if self.value_type == "enum" and not self.choices:
            raise ValueError(f"enum flag {self.name} must declare choices")
        if self.ablatable and self.kind == "internal":
            raise ValueError(f"internal flag {self.name} cannot be marked ablatable")

    @property
    def editable(self) -> bool:
        """Whether a user setting this by hand is meaningful.

        Internal plumbing is written by the launcher on every run, so a value a
        user types would be overwritten before the runtime reads it.
        """
        return self.kind != "internal"

    @property
    def extension_editable(self) -> bool:
        """Whether an untrusted editor webview may pass this knob to a run.

        Sensitive knobs can redirect state or raw provider data, weaken a
        credential/research boundary, or bypass command approval. They remain
        available to a trusted terminal, but the extension exposes them as
        read-only so compromised workspace/webview content cannot set them.
        """
        return self.editable and not self.sensitive

    def coerce(self, raw: str | None) -> Any:
        """Interpret one raw environment string the way the reading module does."""
        text = str(raw if raw is not None else self.default).strip()
        if self.value_type == "bool":
            return text.lower() in _TRUE_VALUES
        if self.value_type == "int":
            try:
                return int(float(text))
            except ValueError:
                return None
        if self.value_type == "float":
            try:
                return float(text)
            except ValueError:
                return None
        if self.value_type == "csv":
            return tuple(part.strip() for part in text.split(",") if part.strip())
        return text

    def is_default(self, raw: str | None) -> bool:
        """Whether a raw value means the same thing as the declared default."""
        if raw is None:
            return True
        if self.value_type == "bool":
            declared = self.default.strip().lower() in _TRUE_VALUES
            given = raw.strip().lower()
            if given not in _TRUE_VALUES and given not in _FALSE_VALUES:
                return False
            return (given in _TRUE_VALUES) == declared
        return str(raw).strip() == self.default.strip()

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON shape consumed by the CLI and the VS Code extension."""
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "value_type": self.value_type,
            "default": self.default,
            "group": self.group,
            "summary": self.summary,
            "editable": self.editable,
            "extension_editable": self.extension_editable,
            "sensitive": self.sensitive,
            "ablatable": self.ablatable,
            "read_in": list(self.read_in),
        }
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        return payload


@dataclass(frozen=True)
class FlagProfile:
    """A named set of knob overrides that can be applied to one run."""

    name: str
    summary: str
    overrides: dict[str, str] = field(default_factory=dict)
    builtin: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "builtin": self.builtin,
            "overrides": dict(sorted(self.overrides.items())),
        }
