"""Shared flash-artifact guardrail policy for R10."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.reference_artifacts import resolve_reference_artifacts
from pyocd_debug_mcp.services.session_runtime import ActionContext, PolicyRefusal
from pyocd_debug_mcp.target_errors import ReferenceArtifactError

SUPPORTED_FLASH_SUFFIXES = frozenset({".elf", ".hex"})
_URL_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True)
class FlashArtifactIdentity:
    path: Path
    suffix: str
    size_bytes: int
    sha256: str
    source: str

    def as_log_fields(self) -> dict[str, object]:
        return {
            "artifact_path": str(self.path),
            "artifact_suffix": self.suffix,
            "artifact_size_bytes": self.size_bytes,
            "artifact_sha256": self.sha256,
            "artifact_source": self.source,
        }


@dataclass(frozen=True)
class ResolvedFlashRequest:
    artifact_path: Path
    identity: FlashArtifactIdentity


def _refuse(code: str, message: str, context: ActionContext) -> PolicyRefusal:
    return PolicyRefusal(code, message, session_id=context.session_id)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_identity(path: Path, *, source: str) -> FlashArtifactIdentity:
    return FlashArtifactIdentity(
        path=path,
        suffix=path.suffix.lower(),
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        source=source,
    )


def resolve_flash_request(
    handle: TargetSessionHandle | None,
    *,
    explicit_path: Path | str | None,
    action_context: ActionContext,
) -> ResolvedFlashRequest:
    if handle is None:
        raise _refuse(
            "flash/no-session", "Flash requires an active connected session.", action_context
        )

    if explicit_path is None:
        if handle.board is None:
            raise _refuse(
                "flash/no-board-config",
                "Default flash resolution requires a loaded board config.",
                action_context,
            )
        try:
            artifact_path = resolve_reference_artifacts(handle.board).flash_artifact
        except ReferenceArtifactError as exc:
            raise _refuse("flash/missing-default-artifact", str(exc), action_context) from exc
        identity = _build_identity(artifact_path, source="default")
        return ResolvedFlashRequest(artifact_path=artifact_path, identity=identity)

    if isinstance(explicit_path, str) and not explicit_path.strip():
        raise _refuse("flash/empty-path", "Flash path must not be empty.", action_context)

    raw_text = str(explicit_path)
    if _URL_LIKE.match(raw_text):
        raise _refuse(
            "flash/non-local-path", "Flash path must be a local filesystem path.", action_context
        )

    artifact_path = Path(explicit_path).expanduser().resolve()
    if not artifact_path.exists():
        raise _refuse(
            "flash/missing-file",
            f"Flash artifact does not exist: {artifact_path}",
            action_context,
        )
    if artifact_path.is_dir():
        raise _refuse(
            "flash/not-a-file",
            f"Flash artifact must be a file, not a directory: {artifact_path}",
            action_context,
        )

    suffix = artifact_path.suffix.lower()
    if suffix not in SUPPORTED_FLASH_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_FLASH_SUFFIXES))
        raise _refuse(
            "flash/unsupported-suffix",
            f"Unsupported flash artifact type '{suffix or '(none)'}'. Use one of: {supported}.",
            action_context,
        )

    identity = _build_identity(artifact_path, source="explicit")
    return ResolvedFlashRequest(artifact_path=artifact_path, identity=identity)
