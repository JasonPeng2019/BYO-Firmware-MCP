"""Always-visible, non-authorizing firmware artifact collection tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pyocd_debug_mcp.artifact_collector import ArtifactRole, collect_artifacts

ArtifactRoleName = Literal["elf", "hex", "bin", "map"]


def build_artifact_handlers() -> dict[str, Callable[..., str]]:
    """Build the build-system-neutral artifact collection surface."""

    def collect_build_artifacts(
        output_dir: str,
        elf_path: str | None = None,
        hex_path: str | None = None,
        bin_path: str | None = None,
        map_path: str | None = None,
        expected_roles: list[ArtifactRoleName] | None = None,
    ) -> str:
        """Normalize explicit native-build outputs for later safety inspection.

        Call this after a native IDE or CLI build when its ELF, HEX, BIN, or linker-map files
        are scattered or use vendor-specific names. Supply only paths that the build actually
        produced; this tool never searches a build tree, runs a build, downloads dependencies,
        or accesses hardware. Choose a new or empty output_dir. For an application or bootloader
        that will enter the guarded safety/flash flow, normally include both elf_path and map_path
        and set expected_roles to ["elf", "map"]. HEX-only or BIN-only collection is provenance
        only and does not make an image safe to flash. The returned canonical paths must still be
        passed explicitly to board_safety_refresh; collection grants no memory authority or gate.
        """

        supplied = {
            ArtifactRole.ELF: elf_path,
            ArtifactRole.HEX: hex_path,
            ArtifactRole.BIN: bin_path,
            ArtifactRole.MAP: map_path,
        }
        sources = {role: Path(path) for role, path in supplied.items() if path is not None}
        try:
            expected = tuple(ArtifactRole(value) for value in (expected_roles or ()))
            result = collect_artifacts(
                sources,
                output_dir,
                producer="mcp-native-build",
                expected_roles=expected,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return json.dumps(
                {
                    "status": "artifact_collection_refused",
                    "message": str(exc),
                    "remedy": (
                        "Use explicit nonempty build outputs, choose a new or empty output_dir, "
                        "and include every role named in expected_roles. Do not guess paths."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        payload = result.to_payload()
        canonical_paths = {
            record.role.value: str(result.output_dir / record.path) for record in result.artifacts
        }
        has_safety_pair = {"elf", "map"}.issubset(canonical_paths)
        payload.update(
            {
                "canonical_paths": canonical_paths,
                "authority": "provenance_only",
                "safety_handoff": {
                    "status": (
                        "explicit_elf_and_map_available" if has_safety_pair else "provenance_only"
                    ),
                    "next_step": (
                        "Pass the canonical ELF, HEX when present, and MAP paths explicitly to "
                        "board_safety_refresh for the correct application or bootloader role."
                        if has_safety_pair
                        else "Obtain and collect a coherent ELF and linker map before safety refresh."
                    ),
                    "manifest_consumed_automatically": False,
                    "raw_bin_has_trusted_load_address": False,
                    "opens_hardware_gate": False,
                },
                "agent_guidance": (
                    "Collection finished. Keep the returned paths machine-readable; do not describe "
                    "this as validation or flash authorization."
                ),
            }
        )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    return {"collect_build_artifacts": collect_build_artifacts}
