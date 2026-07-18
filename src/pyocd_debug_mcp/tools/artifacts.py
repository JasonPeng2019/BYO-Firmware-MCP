"""Always-visible, non-authorizing firmware artifact collection tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from pyocd_debug_mcp.artifact_collector import ArtifactRole, collect_artifacts


def build_artifact_handlers() -> dict[str, Callable[..., str]]:
    """Build the build-system-neutral artifact collection surface."""

    def collect_build_artifacts(
        output_dir: str,
        elf_path: str | None = None,
        hex_path: str | None = None,
        bin_path: str | None = None,
        map_path: str | None = None,
        native_artifacts: dict[str, str] | None = None,
        expected_roles: list[str] | None = None,
    ) -> str:
        """Normalize explicit native-build outputs for execution-time flash inspection.

        Call this after any native IDE or CLI build when outputs are scattered or vendor-named.
        The common ELF, HEX, BIN, and linker-map paths have convenient fields. Put any other
        toolchain-native output in native_artifacts as a bounded role-to-path mapping; an installed
        artifact-evidence provider decides later whether it is safe and flashable. Supply only paths
        the build actually produced. This tool never searches, builds, downloads, or accesses
        hardware. Choose a new or empty output_dir. Collection grants no memory authority or gate.
        """

        supplied = {
            ArtifactRole.ELF: elf_path,
            ArtifactRole.HEX: hex_path,
            ArtifactRole.BIN: bin_path,
            ArtifactRole.MAP: map_path,
        }
        sources: dict[ArtifactRole | str, Path] = {
            role: Path(path) for role, path in supplied.items() if path is not None
        }
        for role, path in (native_artifacts or {}).items():
            if role.casefold() in {
                item.value for item in ArtifactRole if supplied[item] is not None
            }:
                return json.dumps(
                    {
                        "status": "artifact_collection_refused",
                        "message": f"Artifact role was supplied more than once: {role}",
                        "remedy": "Use either the convenient field or native_artifacts for a role.",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            sources[role] = Path(path)
        try:
            result = collect_artifacts(
                sources,
                output_dir,
                producer="mcp-native-build",
                expected_roles=tuple(expected_roles or ()),
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
            record.role: str(result.output_dir / record.path) for record in result.artifacts
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
                        "Pass the canonical ELF or HEX path to flash_application-plan or "
                        "flash_bootloader-plan for the intended role."
                        if has_safety_pair
                        else "Obtain and collect a coherent ELF and linker map before flashing."
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
