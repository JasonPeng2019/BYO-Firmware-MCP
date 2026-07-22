"""Configured labels and the pyOCD CLI fallback for debug-probe inventory."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


class ProbeFamilyRegistryError(RuntimeError):
    """The packaged probe-family compatibility registry is invalid."""


@dataclass(frozen=True, slots=True)
class ProbeFamilySpec:
    provider_id: str
    label: str
    text_aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeCliFallback:
    executable: str
    executable_env: str
    inventory_argv: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class ProbeFamilyRegistry:
    families: tuple[ProbeFamilySpec, ...]
    cli_fallback: ProbeCliFallback


_RESOURCE_NAME = "probe_families.json"
_PROVIDER_ID_RE = re.compile(r"[a-z][a-z0-9_-]*")


def _nonempty_string(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProbeFamilyRegistryError(f"Probe registry field '{name}' must be non-empty")
    return value.strip()


def load_probe_family_registry(path: Path | None = None) -> ProbeFamilyRegistry:
    """Load and strictly validate the provider-neutral compatibility data."""

    try:
        text = (
            path.read_text(encoding="utf-8")
            if path is not None
            else resources.files(__package__).joinpath(_RESOURCE_NAME).read_text(encoding="utf-8")
        )
        document = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        source = str(path) if path is not None else _RESOURCE_NAME
        raise ProbeFamilyRegistryError(f"Probe family registry is unreadable: {source}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ProbeFamilyRegistryError("Probe family registry must use schema_version 1")

    raw_cli = document.get("cli_fallback")
    if not isinstance(raw_cli, dict):
        raise ProbeFamilyRegistryError("Probe family registry needs a cli_fallback object")
    executable = _nonempty_string(raw_cli, "executable")
    executable_env = _nonempty_string(raw_cli, "executable_env")
    raw_commands = raw_cli.get("inventory_argv")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ProbeFamilyRegistryError("cli_fallback.inventory_argv must be a non-empty list")
    commands: list[tuple[str, ...]] = []
    for raw_command in raw_commands:
        if (
            not isinstance(raw_command, list)
            or not raw_command
            or not all(
                isinstance(item, str) and item and "\x00" not in item for item in raw_command
            )
        ):
            raise ProbeFamilyRegistryError("Every inventory argv must contain safe strings")
        commands.append(tuple(raw_command))

    raw_families = document.get("families")
    if not isinstance(raw_families, list):
        raise ProbeFamilyRegistryError("Probe family registry families must be a list")
    families: list[ProbeFamilySpec] = []
    seen: set[str] = set()
    for raw_family in raw_families:
        if not isinstance(raw_family, dict):
            raise ProbeFamilyRegistryError("Each probe family must be an object")
        provider_id = _nonempty_string(raw_family, "provider_id").casefold()
        if _PROVIDER_ID_RE.fullmatch(provider_id) is None or provider_id in seen:
            raise ProbeFamilyRegistryError(f"Invalid or duplicate provider_id: {provider_id}")
        raw_aliases = raw_family.get("text_aliases")
        if (
            not isinstance(raw_aliases, list)
            or not raw_aliases
            or not all(isinstance(item, str) and item.strip() for item in raw_aliases)
        ):
            raise ProbeFamilyRegistryError("text_aliases must be a non-empty string list")
        aliases = tuple(dict.fromkeys(item.strip().casefold() for item in raw_aliases))
        families.append(
            ProbeFamilySpec(
                provider_id=provider_id,
                label=_nonempty_string(raw_family, "label"),
                text_aliases=aliases,
            )
        )
        seen.add(provider_id)

    return ProbeFamilyRegistry(
        tuple(families),
        ProbeCliFallback(executable, executable_env, tuple(commands)),
    )


PROBE_FAMILY_REGISTRY = load_probe_family_registry()


def probe_family_label(provider_id: str) -> str:
    normalized = provider_id.strip().casefold()
    for family in PROBE_FAMILY_REGISTRY.families:
        if family.provider_id == normalized:
            return family.label
    return provider_id


def probe_family_hints(provider_id: str) -> tuple[str, ...]:
    normalized = provider_id.strip().casefold()
    for family in PROBE_FAMILY_REGISTRY.families:
        if family.provider_id == normalized:
            return family.text_aliases
    return ()


def match_probe_family_text(text: str) -> str:
    """Return one configured text match, or ``unknown`` if ambiguous or absent."""

    normalized = text.casefold()
    matches = {
        family.provider_id
        for family in PROBE_FAMILY_REGISTRY.families
        if any(alias in normalized for alias in family.text_aliases)
    }
    return next(iter(matches)) if len(matches) == 1 else "unknown"


def provider_qualified_family(unique_id: str) -> str | None:
    """Read pyOCD's optional ``provider:unique-id`` qualifier without guessing."""

    provider, separator, remainder = unique_id.partition(":")
    normalized = provider.casefold()
    if separator and remainder and _PROVIDER_ID_RE.fullmatch(normalized):
        return normalized
    return None


def configured_probe_cli_commands() -> tuple[tuple[str, ...], ...]:
    """Resolve the configured CLI fallback into validated argv, never a shell string."""

    spec = PROBE_FAMILY_REGISTRY.cli_fallback
    override = os.environ.get(spec.executable_env, "").strip()
    executable = override or shutil.which(spec.executable) or spec.executable
    if not executable or "\x00" in executable:
        raise ProbeFamilyRegistryError("Configured probe CLI executable is invalid")
    return tuple((executable, *suffix) for suffix in spec.inventory_argv)
