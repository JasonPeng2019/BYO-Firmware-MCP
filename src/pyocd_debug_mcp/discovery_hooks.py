"""Agent-authored discovery hooks: manifest models, snapshots, capped execution, parsing.

Hooks exist for one narrow purpose: a machine where pyOCD or pyserial reports no
hardware, but the operator can still name the device from a vendor tool. Hook output
is *configuration*, never evidence -- it can restore no gate, plan, permission,
assignment, or session. Nothing in this module writes hook files; hooks are authored
by an agent under the project's ``.firm/discovery_hooks`` directory and FirmStore only
names and creates that directory.

Two rules shape the code here and are easy to break by accident:

* ``run_owned`` cannot be used. Its ``communicate()`` buffers without limit, so a hook
  writing gigabytes to stdout would take the server down before any size check could
  run. Execution below uses ``popen_owned`` with capped reader threads that keep
  draining past the cap so the child never blocks on a full pipe.
* Nothing is read from the environment or from disk at import time. A malformed
  agent-written manifest must be recoverable by rewriting it and calling
  ``refresh_discovery_hooks`` again, never by restarting the server.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO, Literal, Mapping, Sequence

from pyocd_debug_mcp.kernel.processes import (
    MAX_OWNED_PROCESS_CLEANUP_SECONDS,
    ProcessMarkerStore,
    popen_owned,
    terminate_process_group,
)

HOOK_SCHEMA_VERSION = 1
SUPPORTED_RUNNERS = frozenset({"server-python", "executable"})
SUPPORTED_PLATFORMS = frozenset({"windows", "macos", "linux"})
SUPPORTED_KINDS = frozenset({"probe", "uart"})
MAX_HOOK_TIMEOUT_SECONDS = 60.0
MAX_HOOK_STDOUT_BYTES = 256 * 1024
MAX_HOOK_STDERR_BYTES = 64 * 1024
MAX_HOOK_ROWS = 64
MAX_FIELD_CHARS = 512
DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0
MAX_HOOKS_PER_MANIFEST = 32
# A hook file is re-hashed before every execution to detect drift since the last
# refresh. Bounding the file is what makes that re-read cheap enough to do per
# inventory call rather than once per refresh.
MAX_HOOK_FILE_BYTES = 1024 * 1024
MAX_HOOK_ID_CHARS = 64
MAX_ARGV_ITEMS = 16
MAX_DIAGNOSTIC_CHARS = 2048
_HASH_CHUNK_BYTES = 65536
_READ_CHUNK_BYTES = 65536

MANIFEST_FILENAME = "hooks.json"
DISCOVERY_HOOK_REGISTRY_ENV = "BYO_MCP_DISCOVERY_HOOK_REGISTRY"

HookKind = Literal["probe", "uart"]
HookRunner = Literal["server-python", "executable"]
HookSource = Literal["project", "operator"]
HookOutcome = Literal["exited", "timeout", "cleanup_failed", "parse_failed", "source_changed"]


class DiscoveryHookError(RuntimeError):
    """A hook manifest, hook file, or hook declaration violates the contract."""


class HookSourceChangedError(DiscoveryHookError):
    """A hook file's bytes changed after the refresh that admitted them."""


def current_platform() -> str:
    """Return the one platform token used by manifests, the contract tool, and docs."""

    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bounded_text(raw: bytes, limit: int = MAX_DIAGNOSTIC_CHARS) -> str:
    """Decode diagnostics leniently -- only diagnostics, never parsed payloads."""

    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveryHookDeclaration:
    """One validated manifest entry, before any filesystem resolution.

    Keeping document validation separate from path resolution is what lets the
    contract tool's example manifest be checked against the real validator without
    the example needing to exist on disk.
    """

    hook_id: str
    kind: HookKind
    platforms: frozenset[str]
    runner: HookRunner
    entrypoint: str
    argv: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DiscoveryHookSpec:
    """One eligible hook, resolved and containment-checked against its root."""

    hook_id: str
    kind: HookKind
    platforms: frozenset[str]
    runner: HookRunner
    entrypoint: Path
    argv: tuple[str, ...]
    timeout_seconds: float
    source: HookSource
    file_sha256: str

    @property
    def provenance(self) -> str:
        """The token that appears in inventory rows for hardware this hook found."""

        return f"hook:{self.hook_id}"

    @property
    def friendly_id(self) -> str:
        return f"{self.source}/{self.hook_id}"

    def command(self) -> tuple[str, ...]:
        if self.runner == "server-python":
            # Matches how configured_probe_cli_commands routes pyOCD through
            # sys.executable, so a hook runs inside the same locked environment.
            return (sys.executable, str(self.entrypoint), *self.argv)
        return (str(self.entrypoint), *self.argv)


@dataclass(frozen=True, slots=True)
class DiscoveryHookSnapshot:
    """The immutable set of hooks admitted by one refresh."""

    manifest_sha256: str
    hooks: tuple[DiscoveryHookSpec, ...]
    loaded_at: str

    def eligible(self, kind: str, platform: str) -> tuple[DiscoveryHookSpec, ...]:
        return tuple(
            hook for hook in self.hooks if hook.kind == kind and platform in hook.platforms
        )

    def eligible_counts(self, platform: str | None = None) -> dict[str, int]:
        """Per-kind eligible counts, the input to the operation timeout budget."""

        selected = platform or current_platform()
        return {kind: len(self.eligible(kind, selected)) for kind in sorted(SUPPORTED_KINDS)}

    def has_hooks_for(self, kind: str, platform: str | None = None) -> bool:
        return bool(self.eligible(kind, platform or current_platform()))


EMPTY_SNAPSHOT = DiscoveryHookSnapshot(manifest_sha256="", hooks=(), loaded_at="")


class HookSnapshotStore:
    """Run-scoped, memory-only holder for the snapshot the last refresh admitted.

    Hook configuration is not authority, so this deliberately does not live on
    `ServerRun`: `clear_authority()` would wipe it, and sitting beside real gate, plan,
    and permission state would send exactly the wrong signal about what it is.

    Starts empty, which is what makes the no-manifest path cost nothing and the
    operation-timeout provider safe to call during startup.
    """

    __slots__ = ("_guard", "_snapshot")

    def __init__(self, snapshot: DiscoveryHookSnapshot | None = None) -> None:
        self._guard = threading.RLock()
        self._snapshot = snapshot or EMPTY_SNAPSHOT

    def current(self) -> DiscoveryHookSnapshot:
        with self._guard:
            return self._snapshot

    def replace(self, snapshot: DiscoveryHookSnapshot) -> DiscoveryHookSnapshot:
        with self._guard:
            self._snapshot = snapshot
            return self._snapshot

    def clear(self) -> None:
        with self._guard:
            self._snapshot = EMPTY_SNAPSHOT

    def eligible_counts(self) -> dict[str, int]:
        """Per-kind counts for the operation timeout budget. Zero before any refresh."""

        return self.current().eligible_counts()


@dataclass(frozen=True, slots=True)
class HookProbeRow:
    provider: str
    unique_id: str
    description: str


@dataclass(frozen=True, slots=True)
class HookUartRow:
    port_path: str
    description: str
    serial_number: str | None
    vid: int | None
    pid: int | None

    @property
    def has_stable_identity(self) -> bool:
        return bool(self.serial_number) and self.vid is not None and self.pid is not None


@dataclass(frozen=True, slots=True)
class HookOutput:
    kind: HookKind
    probes: tuple[HookProbeRow, ...]
    uarts: tuple[HookUartRow, ...]


@dataclass(frozen=True, slots=True)
class HookExecution:
    """One hook run, or one pre-execution refusal, reported to the agent.

    ``outcome`` is deliberately never collapsed: an agent repairing a hook needs to
    know whether it exited nonzero, blew its deadline, could not be cleaned up,
    produced unparseable output, or was refused because its bytes changed.
    """

    hook_id: str
    kind: HookKind
    source: HookSource
    outcome: HookOutcome
    exit_code: int | None
    timeout_seconds: float
    output: HookOutput | None
    stdout_excerpt: str
    stderr_excerpt: str
    failure_detail: str
    stdout_truncated: bool
    file_sha256: str

    @property
    def ok(self) -> bool:
        return self.outcome == "exited" and self.exit_code == 0 and self.output is not None

    @property
    def failure_code(self) -> str | None:
        """The step-8 code family value for this outcome."""

        if self.ok:
            return None
        if self.outcome == "timeout":
            return "discovery/hook-timeout"
        if self.outcome == "parse_failed":
            return "discovery/hook-output-invalid"
        if self.outcome == "source_changed":
            return "discovery/hook-source-changed"
        return "discovery/hook-failed"

    def diagnostic_row(self) -> dict[str, object]:
        """The bounded, agent-safe description of what this hook did."""

        row: dict[str, object] = {
            "hook_id": self.hook_id,
            "source": self.source,
            "kind": self.kind,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "timeout_seconds": self.timeout_seconds,
            "ok": self.ok,
        }
        if self.failure_code is not None:
            row["code"] = self.failure_code
        if self.failure_detail:
            row["detail"] = self.failure_detail
        if not self.ok:
            row["stdout_excerpt"] = self.stdout_excerpt
            row["stderr_excerpt"] = self.stderr_excerpt
            row["stdout_truncated"] = self.stdout_truncated
        if self.output is not None:
            row["row_count"] = len(self.output.probes) + len(self.output.uarts)
        return row


# --------------------------------------------------------------------------------------
# Manifest document validation (no filesystem)
# --------------------------------------------------------------------------------------

_DECLARATION_FIELDS = frozenset(
    {"hook_id", "kind", "platforms", "runner", "entrypoint", "argv", "timeout_seconds"}
)
_MANIFEST_FIELDS = frozenset({"schema_version", "hooks"})
_HOOK_ID_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def _reject_unknown(raw: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise DiscoveryHookError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _require_text(raw: Mapping[str, Any], field: str, label: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryHookError(f"{label} field '{field}' must be non-empty text")
    if "\x00" in value:
        raise DiscoveryHookError(f"{label} field '{field}' must not contain NUL bytes")
    if len(value) > MAX_FIELD_CHARS:
        raise DiscoveryHookError(f"{label} field '{field}' exceeds {MAX_FIELD_CHARS} characters")
    return value.strip()


def _validated_hook_id(raw: Mapping[str, Any], label: str) -> str:
    value = _require_text(raw, "hook_id", label).casefold()
    if len(value) > MAX_HOOK_ID_CHARS:
        raise DiscoveryHookError(f"{label} hook_id exceeds {MAX_HOOK_ID_CHARS} characters")
    if any(character not in _HOOK_ID_ALLOWED for character in value):
        raise DiscoveryHookError(
            f"{label} hook_id must use only lowercase letters, digits, '.', '_', or '-'"
        )
    if value[0] not in _HOOK_ID_ALLOWED - frozenset("._-"):
        raise DiscoveryHookError(f"{label} hook_id must start with a letter or digit")
    return value


def _validated_timeout(raw: Mapping[str, Any], label: str) -> float:
    if "timeout_seconds" not in raw:
        return DEFAULT_HOOK_TIMEOUT_SECONDS
    value = raw["timeout_seconds"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiscoveryHookError(f"{label} timeout_seconds must be a number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise DiscoveryHookError(f"{label} timeout_seconds must be positive and finite")
    if seconds > MAX_HOOK_TIMEOUT_SECONDS:
        raise DiscoveryHookError(
            f"{label} timeout_seconds must not exceed {MAX_HOOK_TIMEOUT_SECONDS:g}"
        )
    return seconds


def _validated_platforms(raw: Mapping[str, Any], label: str) -> frozenset[str]:
    value = raw.get("platforms")
    if not isinstance(value, list) or not value:
        raise DiscoveryHookError(f"{label} platforms must be a non-empty list")
    platforms: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise DiscoveryHookError(f"{label} platforms entries must be text")
        token = item.strip().casefold()
        if token not in SUPPORTED_PLATFORMS:
            raise DiscoveryHookError(
                f"{label} platform '{item}' is not one of "
                f"{', '.join(sorted(SUPPORTED_PLATFORMS))}"
            )
        platforms.add(token)
    return frozenset(platforms)


def _validated_argv(raw: Mapping[str, Any], label: str) -> tuple[str, ...]:
    if "argv" not in raw:
        return ()
    value = raw["argv"]
    if not isinstance(value, list):
        raise DiscoveryHookError(f"{label} argv must be a list")
    if len(value) > MAX_ARGV_ITEMS:
        raise DiscoveryHookError(f"{label} argv must not exceed {MAX_ARGV_ITEMS} entries")
    argv: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise DiscoveryHookError(f"{label} argv entries must be non-empty text")
        if "\x00" in item:
            raise DiscoveryHookError(f"{label} argv entries must not contain NUL bytes")
        if len(item) > MAX_FIELD_CHARS:
            raise DiscoveryHookError(f"{label} argv entry exceeds {MAX_FIELD_CHARS} characters")
        argv.append(item)
    return tuple(argv)


def parse_hook_declaration(raw: object, *, label: str = "hook") -> DiscoveryHookDeclaration:
    """Validate one manifest entry strictly, without touching the filesystem."""

    if not isinstance(raw, dict):
        raise DiscoveryHookError(f"{label} must be a JSON object")
    _reject_unknown(raw, _DECLARATION_FIELDS, label)
    hook_id = _validated_hook_id(raw, label)
    kind = _require_text(raw, "kind", label).casefold()
    if kind not in SUPPORTED_KINDS:
        raise DiscoveryHookError(
            f"{label} kind must be one of {', '.join(sorted(SUPPORTED_KINDS))}"
        )
    runner = _require_text(raw, "runner", label)
    if runner not in SUPPORTED_RUNNERS:
        raise DiscoveryHookError(
            f"{label} runner must be one of {', '.join(sorted(SUPPORTED_RUNNERS))}"
        )
    entrypoint = _require_text(raw, "entrypoint", label)
    return DiscoveryHookDeclaration(
        hook_id=hook_id,
        kind=kind,  # type: ignore[arg-type]
        platforms=_validated_platforms(raw, label),
        runner=runner,  # type: ignore[arg-type]
        entrypoint=entrypoint,
        argv=_validated_argv(raw, label),
        timeout_seconds=_validated_timeout(raw, label),
    )


def parse_manifest_document(
    document: object,
    *,
    label: str = "discovery hook manifest",
) -> tuple[DiscoveryHookDeclaration, ...]:
    """Validate a whole manifest document strictly, without touching the filesystem."""

    if not isinstance(document, dict):
        raise DiscoveryHookError(f"{label} must be a JSON object")
    _reject_unknown(document, _MANIFEST_FIELDS, label)
    if document.get("schema_version") != HOOK_SCHEMA_VERSION:
        raise DiscoveryHookError(f"{label} must use schema_version {HOOK_SCHEMA_VERSION}")
    rows = document.get("hooks")
    if not isinstance(rows, list):
        raise DiscoveryHookError(f"{label} hooks must be a list")
    if len(rows) > MAX_HOOKS_PER_MANIFEST:
        raise DiscoveryHookError(f"{label} must not declare more than {MAX_HOOKS_PER_MANIFEST} hooks")
    declarations: list[DiscoveryHookDeclaration] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        declaration = parse_hook_declaration(row, label=f"{label} hooks[{index}]")
        if declaration.hook_id in seen:
            raise DiscoveryHookError(f"{label} declares duplicate hook_id '{declaration.hook_id}'")
        seen.add(declaration.hook_id)
        declarations.append(declaration)
    return tuple(declarations)


# --------------------------------------------------------------------------------------
# Filesystem resolution: containment, hashing
# --------------------------------------------------------------------------------------


def _hash_hook_file(path: Path, *, label: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DiscoveryHookError(f"{label} entrypoint is unreadable: {exc}") from exc
    if size > MAX_HOOK_FILE_BYTES:
        raise DiscoveryHookError(
            f"{label} entrypoint exceeds {MAX_HOOK_FILE_BYTES} bytes and cannot be verified"
        )
    digest = hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                read += len(chunk)
                if read > MAX_HOOK_FILE_BYTES:
                    raise DiscoveryHookError(
                        f"{label} entrypoint grew past {MAX_HOOK_FILE_BYTES} bytes while hashing"
                    )
                digest.update(chunk)
    except OSError as exc:
        raise DiscoveryHookError(f"{label} entrypoint is unreadable: {exc}") from exc
    return digest.hexdigest()


def _contained_entrypoint(root: Path, entrypoint: str, *, label: str) -> Path:
    """Resolve a project hook entrypoint and prove it cannot escape the hook root."""

    if "\x00" in entrypoint:
        raise DiscoveryHookError(f"{label} entrypoint must not contain NUL bytes")
    candidate = Path(entrypoint)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise DiscoveryHookError(
            f"{label} entrypoint must be relative to the server's hook directory"
        )
    resolved_root = Path(root).resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise DiscoveryHookError(f"{label} entrypoint must stay below {resolved_root}")
    if not resolved.is_file():
        raise DiscoveryHookError(f"{label} entrypoint is not a file: {resolved}")
    # `is_relative_to` compares text after resolve() and does not, on its own, prove a
    # symlink inside the root does not point outside it. Compare realpaths as well.
    real_root = os.path.realpath(resolved_root)
    real_file = os.path.realpath(resolved)
    if not Path(real_file).is_relative_to(Path(real_root)):
        raise DiscoveryHookError(
            f"{label} entrypoint resolves outside the hook directory through a link"
        )
    return resolved


def _operator_entrypoint(entrypoint: str, *, label: str) -> Path:
    """Resolve an operator-installed executable: absolute by definition, still checked."""

    if "\x00" in entrypoint:
        raise DiscoveryHookError(f"{label} entrypoint must not contain NUL bytes")
    candidate = Path(entrypoint).expanduser()
    if not candidate.is_absolute():
        raise DiscoveryHookError(f"{label} executable entrypoint must be an absolute path")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise DiscoveryHookError(f"{label} entrypoint is not a file: {resolved}")
    return resolved


def resolve_declaration(
    declaration: DiscoveryHookDeclaration,
    *,
    root: Path,
    source: HookSource,
) -> DiscoveryHookSpec:
    """Turn a validated declaration into an executable spec, or refuse it."""

    label = f"{source} hook '{declaration.hook_id}'"
    if declaration.runner == "executable":
        # Operator-installed by definition, so root containment does not apply.
        entrypoint = _operator_entrypoint(declaration.entrypoint, label=label)
    else:
        entrypoint = _contained_entrypoint(root, declaration.entrypoint, label=label)
    return DiscoveryHookSpec(
        hook_id=declaration.hook_id,
        kind=declaration.kind,
        platforms=declaration.platforms,
        runner=declaration.runner,
        entrypoint=entrypoint,
        argv=declaration.argv,
        timeout_seconds=declaration.timeout_seconds,
        source=source,
        file_sha256=_hash_hook_file(entrypoint, label=label),
    )


def hook_source_digest(spec: DiscoveryHookSpec) -> str:
    """Re-hash a hook's bytes for drift detection before execution."""

    return _hash_hook_file(spec.entrypoint, label=f"{spec.source} hook '{spec.hook_id}'")


# --------------------------------------------------------------------------------------
# Registry loading -- never at import time
# --------------------------------------------------------------------------------------


def _read_manifest_bytes(path: Path, *, label: str) -> bytes | None:
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError as exc:
        raise DiscoveryHookError(f"{label} is unreadable: {path}") from exc


def _decode_manifest(payload: bytes, *, label: str) -> object:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiscoveryHookError(f"{label} must be UTF-8 text") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiscoveryHookError(f"{label} is not valid JSON: {exc}") from exc


def load_hook_snapshot(
    hook_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> DiscoveryHookSnapshot:
    """Load the project manifest and the operator registry into one immutable snapshot.

    The environment variable is read *here*, on every refresh, and never at import
    time. Reading it at import would make a malformed agent-written manifest
    unrecoverable without a server restart -- the exact failure hooks exist to avoid.
    """

    env = os.environ if environ is None else environ
    root = Path(hook_root)
    specs: list[DiscoveryHookSpec] = []
    digest = hashlib.sha256()

    project_manifest = root / MANIFEST_FILENAME
    project_bytes = _read_manifest_bytes(project_manifest, label="project hook manifest")
    if project_bytes is not None:
        digest.update(b"project\x00")
        digest.update(hashlib.sha256(project_bytes).digest())
        declarations = parse_manifest_document(
            _decode_manifest(project_bytes, label="project hook manifest"),
            label="project hook manifest",
        )
        for declaration in declarations:
            specs.append(resolve_declaration(declaration, root=root, source="project"))

    configured = str(env.get(DISCOVERY_HOOK_REGISTRY_ENV, "")).strip()
    if configured:
        operator_manifest = Path(configured).expanduser()
        operator_bytes = _read_manifest_bytes(operator_manifest, label="operator hook registry")
        if operator_bytes is None:
            raise DiscoveryHookError(
                f"operator hook registry does not exist: {operator_manifest}"
            )
        digest.update(b"operator\x00")
        digest.update(hashlib.sha256(operator_bytes).digest())
        declarations = parse_manifest_document(
            _decode_manifest(operator_bytes, label="operator hook registry"),
            label="operator hook registry",
        )
        operator_root = operator_manifest.parent
        for declaration in declarations:
            specs.append(resolve_declaration(declaration, root=operator_root, source="operator"))

    # Deterministic execution order across repeated snapshots. A project hook and an
    # operator hook may share a hook_id; source keeps them distinguishable.
    specs.sort(key=lambda spec: (spec.source, spec.kind, spec.hook_id))
    return DiscoveryHookSnapshot(
        manifest_sha256=digest.hexdigest() if specs or project_bytes is not None else "",
        hooks=tuple(specs),
        loaded_at=_utc_now_text(),
    )


# --------------------------------------------------------------------------------------
# Hook output parsing
# --------------------------------------------------------------------------------------

_OUTPUT_FIELDS = frozenset({"schema_version", "kind", "probes", "uarts"})
_PROBE_ROW_FIELDS = frozenset({"provider", "unique_id", "description"})
_UART_ROW_FIELDS = frozenset({"port_path", "description", "serial_number", "vid", "pid"})


def _row_text(raw: Mapping[str, Any], field: str, label: str, *, required: bool) -> str | None:
    value = raw.get(field)
    if value is None:
        if required:
            raise DiscoveryHookError(f"{label} field '{field}' is required")
        return None
    if not isinstance(value, str):
        raise DiscoveryHookError(f"{label} field '{field}' must be text")
    text = value.strip()
    if not text:
        if required:
            raise DiscoveryHookError(f"{label} field '{field}' must not be empty")
        return None
    if "\x00" in text:
        raise DiscoveryHookError(f"{label} field '{field}' must not contain NUL bytes")
    if len(text) > MAX_FIELD_CHARS:
        raise DiscoveryHookError(f"{label} field '{field}' exceeds {MAX_FIELD_CHARS} characters")
    return text


def _row_usb_id(raw: Mapping[str, Any], field: str, label: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoveryHookError(f"{label} field '{field}' must be an integer or null")
    if not 0 <= value <= 0xFFFF:
        raise DiscoveryHookError(f"{label} field '{field}' must be a 16-bit USB identifier")
    return value


def parse_hook_output(document: object, *, expected_kind: str) -> HookOutput:
    """Validate a hook's stdout document strictly against the published output schema."""

    label = "hook output"
    if not isinstance(document, dict):
        raise DiscoveryHookError(f"{label} must be a JSON object")
    _reject_unknown(document, _OUTPUT_FIELDS, label)
    if document.get("schema_version") != HOOK_SCHEMA_VERSION:
        raise DiscoveryHookError(f"{label} must use schema_version {HOOK_SCHEMA_VERSION}")
    kind = document.get("kind")
    if not isinstance(kind, str) or kind.strip().casefold() != expected_kind:
        raise DiscoveryHookError(f"{label} kind must be '{expected_kind}'")

    probe_rows = document.get("probes")
    uart_rows = document.get("uarts")
    if expected_kind == "probe":
        if uart_rows is not None:
            raise DiscoveryHookError(f"{label} for a probe hook must not carry 'uarts'")
        rows = probe_rows if probe_rows is not None else []
    else:
        if probe_rows is not None:
            raise DiscoveryHookError(f"{label} for a uart hook must not carry 'probes'")
        rows = uart_rows if uart_rows is not None else []
    if not isinstance(rows, list):
        raise DiscoveryHookError(f"{label} rows must be a list")
    if len(rows) > MAX_HOOK_ROWS:
        raise DiscoveryHookError(f"{label} must not exceed {MAX_HOOK_ROWS} rows")

    probes: list[HookProbeRow] = []
    uarts: list[HookUartRow] = []
    for index, row in enumerate(rows):
        row_label = f"{label} row[{index}]"
        if not isinstance(row, dict):
            raise DiscoveryHookError(f"{row_label} must be a JSON object")
        if expected_kind == "probe":
            _reject_unknown(row, _PROBE_ROW_FIELDS, row_label)
            provider = _row_text(row, "provider", row_label, required=True)
            unique_id = _row_text(row, "unique_id", row_label, required=True)
            description = _row_text(row, "description", row_label, required=True)
            assert provider is not None and unique_id is not None and description is not None
            probes.append(
                HookProbeRow(
                    provider=provider.casefold(),
                    unique_id=unique_id,
                    description=description,
                )
            )
        else:
            _reject_unknown(row, _UART_ROW_FIELDS, row_label)
            port_path = _row_text(row, "port_path", row_label, required=True)
            description = _row_text(row, "description", row_label, required=True)
            assert port_path is not None and description is not None
            uarts.append(
                HookUartRow(
                    port_path=port_path,
                    description=description,
                    serial_number=_row_text(row, "serial_number", row_label, required=False),
                    vid=_row_usb_id(row, "vid", row_label),
                    pid=_row_usb_id(row, "pid", row_label),
                )
            )
    return HookOutput(
        kind=expected_kind,  # type: ignore[arg-type]
        probes=tuple(probes),
        uarts=tuple(uarts),
    )


def decode_hook_stdout(payload: bytes, *, expected_kind: str) -> HookOutput:
    """Decode stdout strictly as UTF-8, then validate. Never decode with `replace`."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiscoveryHookError(f"hook stdout is not valid UTF-8: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiscoveryHookError(f"hook stdout is not valid JSON: {exc}") from exc
    return parse_hook_output(document, expected_kind=expected_kind)


# --------------------------------------------------------------------------------------
# Capped execution
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _CappedReader:
    """Read at most `limit` bytes, then keep draining and discarding."""

    stream: IO[bytes]
    limit: int
    chunks: list[bytes]
    truncated: bool = False
    error: BaseException | None = None

    def run(self) -> None:
        remaining = self.limit
        try:
            while chunk := self.stream.read(_READ_CHUNK_BYTES):
                if remaining > 0:
                    kept = chunk[:remaining]
                    self.chunks.append(kept)
                    remaining -= len(kept)
                    if len(kept) < len(chunk):
                        self.truncated = True
                else:
                    self.truncated = True
                # Draining past the cap is what stops the child blocking on a full
                # pipe. Peak memory stays bounded by `limit`, not by output volume.
        except BaseException as exc:  # noqa: BLE001 - reported, never raised in a thread
            self.error = exc

    def text(self) -> str:
        return _bounded_text(b"".join(self.chunks))

    def payload(self) -> bytes:
        return b"".join(self.chunks)


def _hook_env() -> dict[str, str]:
    # A Windows legacy code page can abort a child's stdout midway through an
    # enumeration, silently hiding rows. `_run_cmd` sets this for the same reason.
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    return child_env


@dataclass(frozen=True, slots=True)
class _RawExecution:
    outcome: Literal["exited", "timeout", "cleanup_failed"]
    exit_code: int | None
    stdout: bytes
    stderr_text: str
    stdout_truncated: bool
    detail: str


def _execute(
    spec: DiscoveryHookSpec,
    argv: Sequence[str],
    *,
    marker_store: ProcessMarkerStore | None = None,
) -> _RawExecution:
    """Run one hook under full process-group ownership with bounded capture."""

    process, marker = popen_owned(
        argv,
        marker_store=marker_store,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_hook_env(),
        text=False,
    )
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    readers: list[tuple[_CappedReader, threading.Thread, IO[bytes]]] = []
    if stdout_stream is not None:
        reader = _CappedReader(stdout_stream, MAX_HOOK_STDOUT_BYTES, [])
        readers.append(
            (reader, threading.Thread(target=reader.run, daemon=True), stdout_stream)
        )
    if stderr_stream is not None:
        reader = _CappedReader(stderr_stream, MAX_HOOK_STDERR_BYTES, [])
        readers.append(
            (reader, threading.Thread(target=reader.run, daemon=True), stderr_stream)
        )
    remove_marker = False
    joined: list[IO[bytes]] = []

    def join_readers() -> None:
        """Wait out the reader threads, recording which streams became safe to close.

        Idempotent: the normal path calls this to collect output, and the `finally`
        calls it again so a cancelled run also closes its descriptors instead of
        leaving them to the garbage collector.
        """

        for _reader, thread, stream in readers:
            if thread.is_alive():
                thread.join(timeout=MAX_OWNED_PROCESS_CLEANUP_SECONDS)
            if not thread.is_alive() and stream not in joined:
                joined.append(stream)

    try:
        for _reader, thread, _stream in readers:
            thread.start()
        detail = ""
        try:
            process.wait(timeout=spec.timeout_seconds)
            outcome: Literal["exited", "timeout", "cleanup_failed"] = "exited"
        except subprocess.TimeoutExpired:
            outcome = "timeout"
            detail = f"hook exceeded its {spec.timeout_seconds:g}s deadline"
        except BaseException:
            # Cancellation must not outlive the ownership marker. This includes
            # KeyboardInterrupt and SystemExit, which bypass Exception.
            remove_marker = terminate_process_group(process)
            raise
        remove_marker = terminate_process_group(process)
        join_readers()
        if not remove_marker:
            outcome = "cleanup_failed"
            detail = "hook process group cleanup could not be confirmed; marker retained"
        elif len(joined) != len(readers):
            outcome = "cleanup_failed"
            detail = "a hook output reader did not finish; its descriptor was not closed"
        stdout_reader = readers[0][0] if stdout_stream is not None else None
        stderr_reader = readers[-1][0] if stderr_stream is not None else None
        return _RawExecution(
            outcome=outcome,
            exit_code=process.returncode,
            stdout=stdout_reader.payload() if stdout_reader is not None else b"",
            stderr_text=stderr_reader.text() if stderr_reader is not None else "",
            stdout_truncated=bool(stdout_reader is not None and stdout_reader.truncated),
            detail=detail,
        )
    finally:
        # `communicate()` closed these for us; a hand-rolled reader must do it here.
        # Close only after joining, and never underneath a reader still blocked on the
        # stream -- leaking a descriptor is strictly better than raising in that thread.
        join_readers()
        for stream in joined:
            try:
                stream.close()
            except OSError:
                pass
        if remove_marker:
            (marker_store or ProcessMarkerStore()).remove(marker)


def execute_hook(
    spec: DiscoveryHookSpec,
    *,
    marker_store: ProcessMarkerStore | None = None,
) -> HookExecution:
    """Verify the hook's bytes, run it under a deadline, and parse its output."""

    try:
        current_digest = hook_source_digest(spec)
    except DiscoveryHookError as exc:
        return _refusal(spec, "source_changed", f"hook source is unverifiable: {exc}")
    if current_digest != spec.file_sha256:
        return _refusal(
            spec,
            "source_changed",
            "hook file changed since the last refresh_discovery_hooks; refresh again",
        )

    raw = _execute(spec, spec.command(), marker_store=marker_store)
    stdout_excerpt = _bounded_text(raw.stdout)
    if raw.outcome != "exited" or raw.exit_code != 0:
        detail = raw.detail or f"hook exited with code {raw.exit_code}"
        return HookExecution(
            hook_id=spec.hook_id,
            kind=spec.kind,
            source=spec.source,
            outcome=raw.outcome,
            exit_code=raw.exit_code,
            timeout_seconds=spec.timeout_seconds,
            output=None,
            stdout_excerpt=stdout_excerpt,
            stderr_excerpt=raw.stderr_text,
            failure_detail=detail,
            stdout_truncated=raw.stdout_truncated,
            file_sha256=spec.file_sha256,
        )
    if raw.stdout_truncated:
        return HookExecution(
            hook_id=spec.hook_id,
            kind=spec.kind,
            source=spec.source,
            outcome="parse_failed",
            exit_code=raw.exit_code,
            timeout_seconds=spec.timeout_seconds,
            output=None,
            stdout_excerpt=stdout_excerpt,
            stderr_excerpt=raw.stderr_text,
            failure_detail=(
                f"hook wrote more than {MAX_HOOK_STDOUT_BYTES} bytes to stdout; "
                "output was truncated and cannot be parsed"
            ),
            stdout_truncated=True,
            file_sha256=spec.file_sha256,
        )
    try:
        output = decode_hook_stdout(raw.stdout, expected_kind=spec.kind)
    except DiscoveryHookError as exc:
        return HookExecution(
            hook_id=spec.hook_id,
            kind=spec.kind,
            source=spec.source,
            outcome="parse_failed",
            exit_code=raw.exit_code,
            timeout_seconds=spec.timeout_seconds,
            output=None,
            stdout_excerpt=stdout_excerpt,
            stderr_excerpt=raw.stderr_text,
            failure_detail=str(exc),
            stdout_truncated=raw.stdout_truncated,
            file_sha256=spec.file_sha256,
        )
    return HookExecution(
        hook_id=spec.hook_id,
        kind=spec.kind,
        source=spec.source,
        outcome="exited",
        exit_code=raw.exit_code,
        timeout_seconds=spec.timeout_seconds,
        output=output,
        stdout_excerpt="",
        stderr_excerpt=raw.stderr_text,
        failure_detail="",
        stdout_truncated=False,
        file_sha256=spec.file_sha256,
    )


def _refusal(spec: DiscoveryHookSpec, outcome: HookOutcome, detail: str) -> HookExecution:
    """Report a hook that was refused *without* being executed."""

    return HookExecution(
        hook_id=spec.hook_id,
        kind=spec.kind,
        source=spec.source,
        outcome=outcome,
        exit_code=None,
        timeout_seconds=spec.timeout_seconds,
        output=None,
        stdout_excerpt="",
        stderr_excerpt="",
        failure_detail=detail,
        stdout_truncated=False,
        file_sha256=spec.file_sha256,
    )


def execute_eligible_hooks(
    snapshot: DiscoveryHookSnapshot,
    kind: str,
    *,
    platform: str | None = None,
    marker_store: ProcessMarkerStore | None = None,
) -> tuple[HookExecution, ...]:
    """Run every eligible hook of one kind, once, in the snapshot's fixed order."""

    selected = platform or current_platform()
    return tuple(
        execute_hook(spec, marker_store=marker_store)
        for spec in snapshot.eligible(kind, selected)
    )


# --------------------------------------------------------------------------------------
# Published examples -- the single source the contract tool hands out
# --------------------------------------------------------------------------------------

MANIFEST_SCHEMA_EXAMPLE: dict[str, object] = {
    "schema_version": HOOK_SCHEMA_VERSION,
    "hooks": [
        {
            "hook_id": "local-probe-fallback",
            "kind": "probe",
            "platforms": ["windows", "macos", "linux"],
            "runner": "server-python",
            "entrypoint": "local_probe_fallback.py",
            "argv": [],
            "timeout_seconds": DEFAULT_HOOK_TIMEOUT_SECONDS,
        },
        {
            "hook_id": "local-uart-fallback",
            "kind": "uart",
            "platforms": ["windows", "macos", "linux"],
            "runner": "server-python",
            "entrypoint": "local_uart_fallback.py",
            "argv": [],
            "timeout_seconds": DEFAULT_HOOK_TIMEOUT_SECONDS,
        },
    ],
}

PROBE_OUTPUT_SCHEMA_EXAMPLE: dict[str, object] = {
    "schema_version": HOOK_SCHEMA_VERSION,
    "kind": "probe",
    "probes": [
        {
            "provider": "cmsisdap",
            "unique_id": "066EFF505057717867163251",
            "description": "ST-LINK/V2-1 on the target board",
        }
    ],
}

UART_OUTPUT_SCHEMA_EXAMPLE: dict[str, object] = {
    "schema_version": HOOK_SCHEMA_VERSION,
    "kind": "uart",
    "uarts": [
        {
            "port_path": "COM7",
            "description": "USB Serial Device",
            "serial_number": "066EFF505057717867163251",
            "vid": 1155,
            "pid": 14155,
        }
    ],
}

EXAMPLE_PROBE_HOOK_SOURCE = '''"""Skeleton probe discovery hook. Print one JSON document to stdout and exit 0."""

import json
import sys

# Replace this with a bounded call to the vendor tool that can see the debugger,
# then map its output onto the rows below. Print nothing else to stdout.
PROBES = [
    {
        "provider": "cmsisdap",
        "unique_id": "066EFF505057717867163251",
        "description": "ST-LINK/V2-1 on the target board",
    }
]


def main() -> int:
    json.dump({"schema_version": 1, "kind": "probe", "probes": PROBES}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

EXAMPLE_UART_HOOK_SOURCE = '''"""Skeleton UART discovery hook. Print one JSON document to stdout and exit 0."""

import json
import sys

# `serial_number`, `vid`, and `pid` together make an endpoint stable across
# reconnects and port-path changes. Omit any of them and the endpoint is treated as
# session-local: usable for this run, never cached.
UARTS = [
    {
        "port_path": "COM7",
        "description": "USB Serial Device",
        "serial_number": "066EFF505057717867163251",
        "vid": 1155,
        "pid": 14155,
    }
]


def main() -> int:
    json.dump({"schema_version": 1, "kind": "uart", "uarts": UARTS}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

PLATFORM_GUIDANCE: dict[str, str] = {
    "windows": (
        "Prefer a vendor CLI that is already installed (for example STM32_Programmer_CLI "
        "or nrfjprog) and give its full path. Quote nothing yourself: argv entries are "
        "passed to the process directly, so a path containing spaces needs no escaping. "
        "Do not launch a GUI installer or anything that opens a console window."
    ),
    "macos": (
        "The hook must be executable (`chmod +x`) when runner is 'executable'. Prefer a "
        "'server-python' hook so the server's own interpreter runs it. Avoid tools that "
        "prompt for a keychain or an administrator password: stdin is closed, so a "
        "prompt becomes an immediate EOF, not a hang."
    ),
    "linux": (
        "The hook must be executable (`chmod +x`) when runner is 'executable'. Serial and "
        "debug devices usually require group membership (commonly `dialout` or `plugdev`) "
        "or a udev rule; if the vendor tool sees nothing, check that before writing a "
        "hook. stdin is closed, so nothing may prompt."
    ),
}
