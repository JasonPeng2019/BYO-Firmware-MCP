"""Pluggable, backend-neutral extraction of flash containment evidence."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable, Protocol, cast

from pyocd_debug_mcp.artifact_formats import (
    FirmwareFormat,
    detect_firmware_format,
    matching_elf_companion,
)
from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildEvidence,
    BuildRole,
    LinkerEvidenceError,
    elf_requires_vector_table,
    extract_build_evidence,
)

ENTRY_POINT_GROUP = "pyocd_debug_mcp.artifact_evidence"


class ArtifactEvidenceProvider(Protocol):
    name: str

    def supports(self, path: Path) -> bool: ...

    def inspect(
        self, path: Path, role: BuildRole, *, require_vector_table: bool
    ) -> BuildEvidence: ...

    def dependencies(self, path: Path) -> tuple[Path, ...]: ...

    def requires_vector_table(self, path: Path, role: BuildRole) -> bool: ...


class ElfHexEvidenceProvider:
    name = "elf-intel-hex"

    def supports(self, path: Path) -> bool:
        return detect_firmware_format(path) in {FirmwareFormat.ELF, FirmwareFormat.INTEL_HEX}

    def inspect(
        self, path: Path, role: BuildRole, *, require_vector_table: bool
    ) -> BuildEvidence:
        artifact_format = detect_firmware_format(path)
        if artifact_format is FirmwareFormat.ELF:
            elf_path, hex_path = path, None
        elif artifact_format is FirmwareFormat.INTEL_HEX:
            elf_path, hex_path = matching_elf_companion(path), path
            if elf_path is None:
                raise LinkerEvidenceError(
                    "safety/flash-elf-companion-missing",
                    "Intel HEX requires a matching ELF companion from the same build",
                )
        else:
            raise LinkerEvidenceError(
                "safety/flash-artifact-type", "Artifact is not ELF or Intel HEX"
            )
        return extract_build_evidence(
            BuildArtifactSelection(f"runtime_{role.value}", role, elf_path, None, hex_path),
            require_flash_partition=False,
            require_ram_partition=False,
            require_vector_table=require_vector_table,
        )

    def requires_vector_table(self, path: Path, role: BuildRole) -> bool:
        """Require vector symbols only for architectures whose bundled policy needs them."""

        del role
        artifact_format = detect_firmware_format(path)
        elf_path = path if artifact_format is FirmwareFormat.ELF else matching_elf_companion(path)
        if elf_path is None:
            raise LinkerEvidenceError(
                "safety/flash-elf-companion-missing",
                "Intel HEX requires a matching ELF companion from the same build",
            )
        return elf_requires_vector_table(elf_path)

    def dependencies(self, path: Path) -> tuple[Path, ...]:
        selected = path.expanduser().resolve()
        if detect_firmware_format(selected) is FirmwareFormat.INTEL_HEX:
            companion = matching_elf_companion(selected)
            if companion is None:
                raise LinkerEvidenceError(
                    "safety/flash-elf-companion-missing",
                    "Intel HEX requires a matching ELF companion from the same build",
                )
            return (selected, companion)
        return (selected,)


def artifact_evidence_providers() -> tuple[ArtifactEvidenceProvider, ...]:
    providers: list[ArtifactEvidenceProvider] = [ElfHexEvidenceProvider()]
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        loaded = entry.load()
        provider = loaded() if isinstance(loaded, type) else loaded
        if not callable(getattr(provider, "supports", None)) or not callable(
            getattr(provider, "inspect", None)
        ):
            raise RuntimeError(f"artifact evidence provider {entry.name!r} is invalid")
        providers.append(provider)
    return tuple(providers)


def provider_for_artifact(path: Path) -> ArtifactEvidenceProvider | None:
    selected = path.expanduser().resolve()
    for provider in artifact_evidence_providers():
        if provider.supports(selected):
            return provider
    return None


def artifact_dependency_paths(path: Path) -> tuple[Path, ...]:
    """Return every provider-declared file whose bytes authorize this artifact."""

    selected = path.expanduser().resolve()
    provider = provider_for_artifact(selected)
    if provider is None:
        return ()
    dependencies = getattr(provider, "dependencies", None)
    paths = (
        cast(Callable[[Path], tuple[Path, ...]], dependencies)(selected)
        if callable(dependencies)
        else (selected,)
    )
    resolved = tuple(dict.fromkeys(item.expanduser().resolve() for item in paths))
    if selected not in resolved or any(not item.is_file() for item in resolved):
        raise LinkerEvidenceError(
            "safety/flash-artifact-dependencies",
            "Artifact evidence provider returned missing or incomplete dependency paths",
        )
    return resolved


def extract_artifact_evidence(
    path: Path, role: BuildRole, *, require_vector_table: bool | None = None
) -> BuildEvidence:
    selected = path.expanduser().resolve()
    provider = provider_for_artifact(selected)
    if provider is None:
        raise LinkerEvidenceError(
            "safety/flash-artifact-type",
            "No installed artifact-evidence provider recognizes the selected image",
        )
    required = require_vector_table
    if required is None:
        vector_policy = getattr(provider, "requires_vector_table", None)
        required = bool(vector_policy(selected, role)) if callable(vector_policy) else False
    return provider.inspect(selected, role, require_vector_table=required)
