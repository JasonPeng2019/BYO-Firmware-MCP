"""Always-visible, non-authorizing firmware artifact collection tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from firmware_mcp.artifact_collector import ArtifactRole, collect_artifacts

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
        """**What** Normalize explicit build outputs into one artifact bundle.

        **When** Use after an IDE or external build when ELF, HEX, BIN, or MAP files need a stable
        handoff; for example `elf_path="out/firmware.elf"`.

        **Parameters** `output_dir` is a new or empty bundle directory; optional `elf_path`,
        `hex_path`, `bin_path`, and `map_path` are produced files; optional `expected_roles`
        lists expected artifact roles such as `["elf", "map"]`.

        **Returns** Canonical paths, artifact hashes, and provenance-only evidence. Collection is
        not target validation or flash success.

        **Failures and recovery** Missing, empty, malformed, or colliding outputs are refused;
        correct the build output or choose an unambiguous bundle directory, then call
        `collect_build_artifacts` again.
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
        has_flash_artifact = "elf" in canonical_paths
        payload.update(
            {
                "canonical_paths": canonical_paths,
                "authority": "provenance_only",
                "flash_handoff": {
                    "status": (
                        "firmware_artifact_available" if has_flash_artifact else "provenance_only"
                    ),
                    "next_step": (
                        "Submit the matching canonical ELF or HEX directly to flash."
                        if has_flash_artifact
                        else "Obtain and collect a coherent ELF (and matching ELF for any HEX) "
                        "before flashing."
                    ),
                    "manifest_consumed_automatically": False,
                    "raw_bin_has_trusted_load_address": False,
                    "starts_hardware_operation": False,
                },
                "agent_guidance": (
                    "Collection finished. Returned paths and provenance identify artifacts; collection "
                    "does not validate or flash a target."
                ),
            }
        )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    return {"collect_build_artifacts": collect_build_artifacts}
