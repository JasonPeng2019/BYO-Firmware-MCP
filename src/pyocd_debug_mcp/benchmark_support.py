"""Shared benchmark contracts and helpers for the R11/R12 benchmark surfaces."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import secrets
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
import sys

from pyocd_debug_mcp.board_config import (
    DEFAULT_BOARD_CONFIG_DIR,
    BoardConfig,
    load_selected_board_configs,
)
from pyocd_debug_mcp.probe_inventory import resolve_probe_for_board
from pyocd_debug_mcp.reference_smoke import run_stage1_smoke
from pyocd_debug_mcp.runtime_resources import (
    resolve_benchmark_cases_root,
    resolve_benchmark_result_schema_path,
    resolve_repo_root,
)
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.session_runtime import RUNS_ROOT
from pyocd_debug_mcp.timeouts import DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS

REPO_ROOT = resolve_repo_root(required=False)
CASES_ROOT = resolve_benchmark_cases_root()
SUITES_PATH = CASES_ROOT / "suites.yaml"
RESULT_SCHEMA_PATH = resolve_benchmark_result_schema_path()
WORKSPACES_ROOT = RUNS_ROOT / "_r11_workspaces"
DEFAULT_SERIAL_READ_SECONDS = 3.0
DEFAULT_CODEX_TIMEOUT_SECONDS = 180.0
DEFAULT_CASE_BUILD_TIMEOUT_SECONDS = 1800.0
DEFAULT_SCORING_PROFILE = "r11_default_v1"
_RUNTIME_CONTEXT_DIRS = frozenset({".agents", ".claude", ".codex"})
FULL_MCP_TOOL_SURFACE = (
    "connect",
    "disconnect",
    "get_board_info",
    "get_state",
    "halt",
    "resume",
    "step",
    "reset",
    "read_core_register",
    "write_core_register",
    "read_memory",
    "read_memory_block",
    "read_symbol_u32",
    "write_memory",
    "set_breakpoint",
    "remove_breakpoint",
    "flash_firmware",
    "read_serial",
    "unlock_recover",
)
VALID_KINDS = {"known_good", "injected_bug", "observability_fault"}
VALID_WORKSPACE_MODES = {"reference_read_only", "copied_bug_workspace"}
VALID_FLASH_MODES = {"reference", "case_artifact"}
VALID_POST_FLASH_STATES = {"running", "halted"}
VALID_CLASSIFICATIONS = {"healthy", "code_bug", "observability_fault", "physical_fault"}
VALID_FINAL_STATUSES = {"fixed", "healthy_confirmed", "diagnosed_only", "unresolved", "blocked"}
_STAGE1_PREFLIGHT_CACHE: dict[str, str | None] = {}


def _require_repo_root() -> Path:
    if REPO_ROOT is None:
        raise RuntimeError(
            "This operation requires a live Firmware-CLI repo checkout because the benchmark "
            "cases use repo-owned firmware workspaces."
        )
    return REPO_ROOT


@dataclass(frozen=True)
class InitialPrep:
    flash_mode: str
    post_flash_state: str


@dataclass(frozen=True)
class CaseArtifacts:
    flash_artifact: str
    symbol_artifact: str
    source_root: str | None
    build_script: str | None


@dataclass(frozen=True)
class ExpectedObservables:
    uart_substring: str
    symbol_name: str
    symbol_value_u32: int
    test_read_address: int | None


@dataclass(frozen=True)
class AllowedActions:
    mcp_tools: tuple[str, ...]
    code_edits_allowed: bool
    allowed_edit_roots: tuple[str, ...]
    build_command: str | None
    recover_allowed: bool


@dataclass(frozen=True)
class SuccessCriteria:
    expected_classification: str
    requires_code_fix: bool
    requires_green_verification: bool
    expected_changed_files: tuple[str, ...]
    forbidden_actions: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    title: str
    board_id: str
    kind: str
    workspace_mode: str
    initial_prep: InitialPrep
    artifacts: CaseArtifacts
    expected_observables: ExpectedObservables
    allowed_actions: AllowedActions
    success_criteria: SuccessCriteria
    scoring_profile: str
    case_dir: Path
    prompt_template: str


@dataclass(frozen=True)
class PreparedWorkspace:
    source_root: Path
    workspace_root: Path
    snapshot_root: Path


@dataclass(frozen=True)
class PreparedCase:
    case: BenchmarkCase
    board: BoardConfig
    workspace: PreparedWorkspace
    probe_uid: str
    flash_artifact: Path
    symbol_artifact: Path


@dataclass(frozen=True)
class ParsedAgentResult:
    case_id: str
    board_id: str
    session_id: str
    final_status: str
    classification: str
    root_cause: str
    actions_taken: tuple[str, ...]
    mcp_tools_used: tuple[str, ...]
    files_changed: tuple[str, ...]
    recover_used: bool
    verification: dict[str, bool]
    summary: str


@dataclass(frozen=True)
class CodexRunArtifacts:
    exit_code: int
    stdout_text: str
    stderr_text: str
    result_path: Path
    prompt_path: Path
    new_session_dirs: tuple[Path, ...]


@dataclass(frozen=True)
class VerificationSummary:
    flash_ok: bool
    uart_ok: bool
    symbol_ok: bool
    green_check_ok: bool
    excerpt: str
    error_text: str | None


@dataclass(frozen=True)
class SessionSelection:
    canonical_run_root: Path
    supporting_run_roots: tuple[Path, ...]
    runner_warnings: tuple[str, ...]


@dataclass(frozen=True)
class ScoreReport:
    score: int
    outcome_label: str
    diagnosis_points: int
    intervention_points: int
    verification_points: int
    safety_points: int
    penalties: tuple[str, ...]
    reasons: tuple[str, ...]
    actual_changed_files: tuple[str, ...]
    classification_correct: bool
    intervention_correct: bool


@dataclass(frozen=True)
class CaseRunReport:
    case_id: str
    board_id: str
    session_id: str | None
    final_status: str
    score_report: ScoreReport
    verification: VerificationSummary
    run_root: Path | None


def _require_mapping(path: Path, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def _load_yaml(path: Path) -> dict[str, object]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - guaranteed by repo deps
        raise RuntimeError("PyYAML is required. Run `uv sync` from the repo root.") from exc
    return _require_mapping(path, yaml.safe_load(path.read_text(encoding="utf-8")))


def _run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = DEFAULT_EXTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        executable = cmd[0] if cmd else "<unknown>"
        return 127, "", f"command not found: {executable}"
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        detail = f"command timed out after {timeout_seconds:.0f}s"
        stderr = f"{stderr}\n{detail}".strip()
        return 124, stdout, stderr
    return result.returncode, result.stdout or "", result.stderr or ""


def _load_board(board_id: str) -> BoardConfig:
    boards = load_selected_board_configs(DEFAULT_BOARD_CONFIG_DIR, requested_ids=[board_id])
    if not boards:
        raise RuntimeError(f"Board not found: {board_id}")
    return boards[0]


def _normalize_str_list(value: object | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("Expected a list")
    output: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            output.append(text)
    return tuple(output)


def _require_text(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid text field: {key}")
    return value.strip()


def _require_bool(mapping: dict[str, object], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Missing or invalid boolean field: {key}")
    return value


def _require_choice(mapping: dict[str, object], key: str, choices: set[str]) -> str:
    value = _require_text(mapping, key)
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"Field '{key}' must be one of: {allowed}")
    return value


def _require_int(mapping: dict[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Missing or invalid integer field: {key}")
    return value


def _optional_int(mapping: dict[str, object], key: str) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"Invalid integer field: {key}")
    return value


def load_case(case_id: str) -> BenchmarkCase:
    case_dir = CASES_ROOT / case_id
    case_path = case_dir / "case.yaml"
    prompt_path = case_dir / "prompt.md"
    if not case_path.exists():
        raise FileNotFoundError(f"Benchmark case not found: {case_id}")
    raw = _load_yaml(case_path)

    raw_case_id = _require_text(raw, "case_id")
    if raw_case_id != case_id:
        raise ValueError(f"{case_path} case_id does not match the directory name")

    kind = _require_choice(raw, "kind", VALID_KINDS)
    workspace_mode = _require_choice(raw, "workspace_mode", VALID_WORKSPACE_MODES)
    initial_prep_raw = _require_mapping(case_path, raw.get("initial_prep"))
    artifacts_raw = _require_mapping(case_path, raw.get("artifacts"))
    expected_raw = _require_mapping(case_path, raw.get("expected_observables"))
    allowed_raw = _require_mapping(case_path, raw.get("allowed_actions"))
    success_raw = _require_mapping(case_path, raw.get("success_criteria"))

    source_root_value = artifacts_raw.get("source_root")
    build_script_value = artifacts_raw.get("build_script")
    if kind == "injected_bug":
        if not isinstance(source_root_value, str) or not source_root_value.strip():
            raise ValueError(f"{case_path} bug cases require artifacts.source_root")
        if not isinstance(build_script_value, str) or not build_script_value.strip():
            raise ValueError(f"{case_path} bug cases require artifacts.build_script")
    else:
        source_root_value = None
        build_script_value = None

    prompt_template = prompt_path.read_text(encoding="utf-8")
    mcp_tools = _normalize_str_list(allowed_raw.get("mcp_tools")) or FULL_MCP_TOOL_SURFACE
    scoring_profile = _require_text(raw, "scoring_profile")

    return BenchmarkCase(
        case_id=case_id,
        title=_require_text(raw, "title"),
        board_id=_require_text(raw, "board_id"),
        kind=kind,
        workspace_mode=workspace_mode,
        initial_prep=InitialPrep(
            flash_mode=_require_choice(initial_prep_raw, "flash_mode", VALID_FLASH_MODES),
            post_flash_state=_require_choice(
                initial_prep_raw,
                "post_flash_state",
                VALID_POST_FLASH_STATES,
            ),
        ),
        artifacts=CaseArtifacts(
            flash_artifact=_require_text(artifacts_raw, "flash_artifact"),
            symbol_artifact=_require_text(artifacts_raw, "symbol_artifact"),
            source_root=source_root_value,
            build_script=build_script_value,
        ),
        expected_observables=ExpectedObservables(
            uart_substring=_require_text(expected_raw, "uart_substring"),
            symbol_name=_require_text(expected_raw, "symbol_name"),
            symbol_value_u32=_require_int(expected_raw, "symbol_value_u32"),
            test_read_address=_optional_int(expected_raw, "test_read_address"),
        ),
        allowed_actions=AllowedActions(
            mcp_tools=mcp_tools,
            code_edits_allowed=_require_bool(allowed_raw, "code_edits_allowed"),
            allowed_edit_roots=_normalize_str_list(allowed_raw.get("allowed_edit_roots")),
            build_command=(
                _require_text(allowed_raw, "build_command")
                if allowed_raw.get("build_command") is not None
                else None
            ),
            recover_allowed=_require_bool(allowed_raw, "recover_allowed"),
        ),
        success_criteria=SuccessCriteria(
            expected_classification=_require_choice(
                success_raw,
                "expected_classification",
                VALID_CLASSIFICATIONS,
            ),
            requires_code_fix=_require_bool(success_raw, "requires_code_fix"),
            requires_green_verification=_require_bool(success_raw, "requires_green_verification"),
            expected_changed_files=_normalize_str_list(success_raw.get("expected_changed_files")),
            forbidden_actions=_normalize_str_list(success_raw.get("forbidden_actions")),
        ),
        scoring_profile=scoring_profile,
        case_dir=case_dir,
        prompt_template=prompt_template,
    )


def load_suite(suite_name: str) -> list[BenchmarkCase]:
    suites = _load_yaml(SUITES_PATH)
    suite_raw = _require_mapping(SUITES_PATH, suites.get(suite_name))
    case_ids = _normalize_str_list(suite_raw.get("cases"))
    if not case_ids:
        raise ValueError(f"Suite '{suite_name}' does not define any cases")
    return [load_case(case_id) for case_id in case_ids]


def _session_dirs() -> dict[str, Path]:
    session_dirs: dict[str, Path] = {}
    if not RUNS_ROOT.exists():
        return session_dirs
    for child in RUNS_ROOT.iterdir():
        if child.is_dir() and (child / "run-metadata" / "session.json").exists():
            session_dirs[child.name] = child
    return session_dirs


def _workspace_source_root(case: BenchmarkCase) -> Path:
    repo_root = _require_repo_root()
    if case.workspace_mode == "reference_read_only":
        return repo_root / "firmware" / case.board_id / "reference"
    if case.artifacts.source_root is None:
        raise RuntimeError(f"{case.case_id} does not define artifacts.source_root")
    return (repo_root / case.artifacts.source_root).resolve()


def _prepare_workspace(case: BenchmarkCase) -> PreparedWorkspace:
    source_root = _workspace_source_root(case)
    if not source_root.exists():
        raise FileNotFoundError(f"Workspace source does not exist: {source_root}")
    workspace_token = f"{case.case_id}__{secrets.token_hex(4)}"
    workspace_base = WORKSPACES_ROOT / workspace_token
    workspace_root = workspace_base / "workspace"
    snapshot_root = workspace_base / "snapshot"
    workspace_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, workspace_root)
    firmware_root = _require_repo_root() / "firmware"
    try:
        relative_source_root = source_root.relative_to(firmware_root)
    except ValueError:
        relative_source_root = None
    if relative_source_root is not None and relative_source_root.parts:
        common_root = firmware_root / relative_source_root.parts[0] / "common"
        if common_root.is_dir():
            shutil.copytree(common_root, workspace_root / "common")
            for cmake_path in workspace_root.rglob("CMakeLists.txt"):
                original = cmake_path.read_text(encoding="utf-8")
                updated = original.replace(
                    "${CMAKE_CURRENT_SOURCE_DIR}/../../../common",
                    "${CMAKE_CURRENT_SOURCE_DIR}/../common",
                ).replace(
                    "${CMAKE_CURRENT_SOURCE_DIR}/../../common",
                    "${CMAKE_CURRENT_SOURCE_DIR}/../common",
                )
                if updated != original:
                    cmake_path.write_text(updated, encoding="utf-8")
    shutil.copytree(workspace_root, snapshot_root)
    return PreparedWorkspace(
        source_root=source_root,
        workspace_root=workspace_root,
        snapshot_root=snapshot_root,
    )


def _resolve_case_artifacts(case: BenchmarkCase, workspace_root: Path) -> tuple[Path, Path]:
    flash_artifact = (workspace_root / case.artifacts.flash_artifact).resolve()
    symbol_artifact = (workspace_root / case.artifacts.symbol_artifact).resolve()
    return flash_artifact, symbol_artifact


def _resolve_probe_uid(board: BoardConfig) -> str:
    resolution = resolve_probe_for_board(board, run_cmd=_run_cmd, allow_single_fallback=True)
    if resolution.probe is None:
        raise RuntimeError(f"Probe resolution failed for {board.display_name}: {resolution.note}")
    if not resolution.probe.uid:
        raise RuntimeError(
            f"Probe resolution for {board.display_name} did not yield a unique id. Rerun with an explicit host setup."
        )
    return str(resolution.probe.uid)


def _run_build_command(command: str, workspace_root: Path) -> None:
    if sys.platform == "win32":
        cmd = ["cmd.exe", "/d", "/s", "/c", command]
    else:
        cmd = ["bash", "-lc", command]
    exit_code, stdout, stderr = _run_cmd(
        cmd,
        cwd=workspace_root,
        timeout_seconds=DEFAULT_CASE_BUILD_TIMEOUT_SECONDS,
    )
    if exit_code != 0:
        raise RuntimeError(
            f"Build command failed in {workspace_root}: {command}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )


def _prepare_case(case: BenchmarkCase) -> PreparedCase:
    board = _load_board(case.board_id)
    workspace = _prepare_workspace(case)
    probe_uid = _resolve_probe_uid(board)
    if case.kind == "injected_bug":
        build_command = case.allowed_actions.build_command or "./build_bug.sh"
        _run_build_command(build_command, workspace.workspace_root)
    flash_artifact, symbol_artifact = _resolve_case_artifacts(case, workspace.workspace_root)
    if not symbol_artifact.exists():
        raise FileNotFoundError(f"Missing symbol artifact for {case.case_id}: {symbol_artifact}")
    if case.initial_prep.flash_mode == "case_artifact" and not flash_artifact.exists():
        raise FileNotFoundError(f"Missing flash artifact for {case.case_id}: {flash_artifact}")
    return PreparedCase(
        case=case,
        board=board,
        workspace=workspace,
        probe_uid=probe_uid,
        flash_artifact=flash_artifact,
        symbol_artifact=symbol_artifact,
    )


def _prepare_target_state(prepared: PreparedCase) -> None:
    handle = target_control.open_session(
        board=prepared.board,
        unique_id=prepared.probe_uid,
        target=prepared.board.pyocd_target,
    )
    try:
        if prepared.case.initial_prep.flash_mode == "reference":
            target_control.flash_firmware(
                handle,
                prepared.flash_artifact,
                halt_after_reset=prepared.case.initial_prep.post_flash_state == "halted",
            )
        else:
            target_control.flash_firmware(
                handle,
                prepared.flash_artifact,
                halt_after_reset=prepared.case.initial_prep.post_flash_state == "halted",
            )
        if prepared.case.initial_prep.post_flash_state == "halted":
            target_control.halt(handle)
    finally:
        target_control.close_session(handle)


def _ensure_stage1_preflight(board_id: str, probe_uid: str) -> None:
    cached = _STAGE1_PREFLIGHT_CACHE.get(board_id)
    if cached is None and board_id in _STAGE1_PREFLIGHT_CACHE:
        return
    if cached is not None:
        raise RuntimeError(cached)

    try:
        run_stage1_smoke(
            board_id=board_id,
            probe_uid=probe_uid,
            serial_read_seconds=DEFAULT_SERIAL_READ_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the concrete bench blocker
        message = (
            f"Benchmark preflight failed for {board_id}: {type(exc).__name__}: {exc}. "
            "Rerun Stage 0 and Stage 1 smoke on this host before R11."
        )
        _STAGE1_PREFLIGHT_CACHE[board_id] = message
        raise RuntimeError(message) from exc

    _STAGE1_PREFLIGHT_CACHE[board_id] = None


def _render_prompt(case: BenchmarkCase) -> str:
    case_prompt = case.prompt_template.format(
        board_id=case.board_id,
        case_id=case.case_id,
        build_command=case.allowed_actions.build_command or "(no local rebuild expected)",
        symbol_artifact=case.artifacts.symbol_artifact,
        uart_substring=case.expected_observables.uart_substring,
        symbol_name=case.expected_observables.symbol_name,
        symbol_value_u32_hex=f"0x{case.expected_observables.symbol_value_u32:08X}",
    )
    case_context = ""
    if case.kind == "known_good":
        case_context = (
            "Case-specific context:\n\n"
            "- this workspace is the tracked reference baseline, not an injected bug fixture\n"
            "- if the board already matches the expected UART and symbol contract, classify it as `healthy`\n\n"
        )
    elif case.kind == "observability_fault":
        case_context = (
            "Case-specific context:\n\n"
            "- this workspace is the tracked reference baseline, not an injected bug fixture\n"
            f"- before you connect, the runner intentionally prepared the target with post-flash state `{case.initial_prep.post_flash_state}`\n"
            "- an initial `HALTED` or `SLEEPING` state is evidence for this case, not a debugger artifact by itself\n"
            "- if the firmware artifact is healthy but target execution state blocks fresh UART output or halted-only register reads, classify the case as `observability_fault`\n"
            "- do not reinterpret a later reset, resume, or reflash that returns the board to green as proof that the initial state was `healthy`\n"
            "- do not edit source for this case\n\n"
        )
    phase_contract = ""
    if case.kind == "injected_bug":
        phase_contract = (
            "Bug-case phase contract:\n\n"
            "1. Diagnose:\n"
            "   connect, read UART, read the symbol, and decide whether this is a code bug or an observability fault before editing.\n"
            "2. Patch/build:\n"
            "   if it is a code bug, patch only the allowed source file(s) and run the provided rebuild command.\n"
            "3. Flash/verify:\n"
            "   flash the rebuilt artifact, rerun UART and symbol verification, then return the structured result immediately.\n\n"
        )
    return (
        f"{case_prompt.rstrip()}\n\n"
        f"{case_context}"
        "Benchmark discipline:\n\n"
        "- do not read repo workflow skills, playbooks, or markdown docs unless the prompt explicitly requires them\n"
        "- this self-contained prompt rule is benchmark-specific; the real deployment workflow should still read its repo workflow docs and skills before acting\n"
        "- prefer the MCP tool surface over local shell commands and static-file inspection\n\n"
        f"{phase_contract}"
        "Verification hints:\n\n"
        f"- symbol artifact path: `{case.artifacts.symbol_artifact}`\n"
        "- prefer `read_serial(expected_text=..., reset_on_open=true)` when you need fresh boot text\n"
        "- verify the symbol directly with "
        f'`read_symbol_u32(elf_path="{case.artifacts.symbol_artifact}", symbol_name="{case.expected_observables.symbol_name}")`\n\n'
        "Structured result contract:\n\n"
        f"- return `case_id` exactly as `{case.case_id}`\n"
        f"- return `board_id` exactly as `{case.board_id}`\n"
        "- return `session_id` exactly as reported by the successful `connect` tool call\n"
        "- do not derive `case_id` from the workspace directory name or any copied temp path\n"
        "- do not pass a generic target override such as `cortex_m`\n"
        "- avoid reconnect churn unless the first session clearly attached to the wrong board or cannot complete verification\n"
        "- if you reconnect, use the final successful session for final verification and final reporting\n"
    )


def _ensure_codex_registration() -> None:
    exit_code, _, stderr = _run_cmd(["codex", "mcp", "get", "pyocd-debug"])
    if exit_code == 0:
        return
    if exit_code == 127:
        raise RuntimeError("Codex CLI is not installed or not in PATH.")
    raise RuntimeError(
        "Codex MCP registration missing. Run: codex mcp add pyocd-debug -- uv run pyocd-debug-mcp"
        + (f"\n{stderr}" if stderr else "")
    )


def _run_codex(
    case: BenchmarkCase,
    workspace_root: Path,
    prompt_text: str,
    *,
    timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
) -> CodexRunArtifacts:
    before_dirs = set(_session_dirs())
    temp_result_path = workspace_root / ".r11_codex_result.json"
    prompt_path = workspace_root / ".r11_prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    cmd = [
        "codex",
        "-a",
        "never",
        "-s",
        "danger-full-access",
        "exec",
        "-C",
        str(workspace_root),
        "--output-schema",
        str(RESULT_SCHEMA_PATH),
        "--json",
        "-o",
        str(temp_result_path),
        prompt_text,
    ]
    exit_code, stdout, stderr = _run_cmd(
        cmd,
        cwd=_require_repo_root(),
        timeout_seconds=timeout_seconds,
    )
    after_dirs = _session_dirs()
    new_names = sorted(set(after_dirs) - before_dirs)
    return CodexRunArtifacts(
        exit_code=exit_code,
        stdout_text=stdout,
        stderr_text=stderr,
        result_path=temp_result_path,
        prompt_path=prompt_path,
        new_session_dirs=tuple(after_dirs[name] for name in new_names),
    )


def _parse_agent_result(path: Path) -> ParsedAgentResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Codex result must be a JSON object")

    final_status = _require_choice(raw, "final_status", VALID_FINAL_STATUSES)
    classification = _require_choice(raw, "classification", VALID_CLASSIFICATIONS)
    verification = _require_mapping(path, raw.get("verification"))
    for field in ("flash_ok", "uart_ok", "symbol_ok", "green_check_ok"):
        if not isinstance(verification.get(field), bool):
            raise ValueError(f"verification.{field} must be a boolean")
    return ParsedAgentResult(
        case_id=_require_text(raw, "case_id"),
        board_id=_require_text(raw, "board_id"),
        session_id=_require_text(raw, "session_id"),
        final_status=final_status,
        classification=classification,
        root_cause=_require_text(raw, "root_cause"),
        actions_taken=_normalize_str_list(raw.get("actions_taken")),
        mcp_tools_used=_normalize_str_list(raw.get("mcp_tools_used")),
        files_changed=_normalize_str_list(raw.get("files_changed")),
        recover_used=_require_bool(raw, "recover_used"),
        verification={
            "flash_ok": bool(verification["flash_ok"]),
            "uart_ok": bool(verification["uart_ok"]),
            "symbol_ok": bool(verification["symbol_ok"]),
            "green_check_ok": bool(verification["green_check_ok"]),
        },
        summary=_require_text(raw, "summary"),
    )


def _fallback_result(case: BenchmarkCase, session_id: str | None, reason: str) -> dict[str, object]:
    return {
        "schema_valid": False,
        "case_id": case.case_id,
        "board_id": case.board_id,
        "session_id": session_id,
        "reason": reason,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_session_metadata(run_root: Path) -> dict[str, object]:
    raw = json.loads((run_root / "run-metadata" / "session.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{run_root}/run-metadata/session.json must contain a JSON object")
    return raw


def _select_canonical_session(
    prepared: PreparedCase,
    agent_result: ParsedAgentResult,
    new_session_dirs: tuple[Path, ...],
) -> SessionSelection:
    matching = [path for path in new_session_dirs if path.name == agent_result.session_id]
    if not matching:
        raise RuntimeError(
            f"Structured benchmark result reported session_id={agent_result.session_id}, "
            "but that session was not created under runs/ during this case."
        )
    if len(matching) > 1:
        raise RuntimeError(
            f"Structured benchmark result reported session_id={agent_result.session_id}, "
            "but multiple new session directories claimed that id."
        )

    canonical_run_root = matching[0]
    supporting_run_roots = tuple(path for path in new_session_dirs if path != canonical_run_root)
    runner_warnings: list[str] = []
    if supporting_run_roots:
        runner_warnings.append(f"supporting-session-count:{len(supporting_run_roots)}")
    for run_root in supporting_run_roots:
        try:
            metadata = _load_session_metadata(run_root)
        except Exception as exc:  # noqa: BLE001 - preserve the specific session warning
            runner_warnings.append(
                f"supporting-session-metadata-unreadable:{run_root.name}:{type(exc).__name__}"
            )
            continue
        board_id = str(metadata.get("board_id") or "")
        probe_uid = str(metadata.get("probe_uid") or "")
        if board_id and board_id != prepared.case.board_id:
            runner_warnings.append(f"supporting-session-board-mismatch:{run_root.name}:{board_id}")
        if probe_uid and probe_uid != prepared.probe_uid:
            runner_warnings.append(f"supporting-session-probe-mismatch:{run_root.name}:{probe_uid}")

    return SessionSelection(
        canonical_run_root=canonical_run_root,
        supporting_run_roots=supporting_run_roots,
        runner_warnings=tuple(runner_warnings),
    )


def _relative_files(root: Path) -> dict[str, bytes]:
    output: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "build" in relative.parts:
            continue
        if _is_runtime_context_file(relative):
            continue
        if relative.name.startswith(".r11_"):
            continue
        output[str(relative).replace("\\", "/")] = path.read_bytes()
    return output


def _is_runtime_context_file(relative: Path) -> bool:
    return bool(relative.parts) and relative.parts[0] in _RUNTIME_CONTEXT_DIRS


def _changed_files(before_root: Path, after_root: Path) -> tuple[str, ...]:
    before = _relative_files(before_root)
    after = _relative_files(after_root)
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    return tuple(changed)


def _decode_diff_text(data: bytes) -> tuple[list[str], bool]:
    try:
        return data.decode("utf-8").splitlines(keepends=True), False
    except UnicodeDecodeError:
        return [], True


def _write_diff(before_root: Path, after_root: Path, output_path: Path) -> None:
    before = _relative_files(before_root)
    after = _relative_files(after_root)
    chunks: list[str] = []

    for relative in sorted(set(before) | set(after)):
        before_bytes = before.get(relative)
        after_bytes = after.get(relative)
        if before_bytes == after_bytes:
            continue

        before_lines, before_binary = _decode_diff_text(before_bytes or b"")
        after_lines, after_binary = _decode_diff_text(after_bytes or b"")
        if before_binary or after_binary:
            chunks.append(f"Binary files a/{relative} and b/{relative} differ\n")
            continue

        diff_lines = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="",
        )
        rendered = "\n".join(diff_lines)
        if rendered:
            chunks.append(rendered + "\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(chunks), encoding="utf-8")


def _run_final_verification(prepared: PreparedCase) -> VerificationSummary:
    try:
        smoke = run_stage1_smoke(
            board_id=prepared.case.board_id,
            probe_uid=prepared.probe_uid,
            flash_artifact=prepared.flash_artifact,
            elf=prepared.symbol_artifact,
            serial_read_seconds=DEFAULT_SERIAL_READ_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - benchmark should capture the concrete failure
        return VerificationSummary(
            flash_ok=False,
            uart_ok=False,
            symbol_ok=False,
            green_check_ok=False,
            excerpt="",
            error_text=f"{type(exc).__name__}: {exc}",
        )
    symbol_ok = (
        smoke.resolved_symbol.value_u32 == prepared.case.expected_observables.symbol_value_u32
    )
    return VerificationSummary(
        flash_ok=True,
        uart_ok=True,
        symbol_ok=symbol_ok,
        green_check_ok=symbol_ok,
        excerpt=smoke.capture_excerpt,
        error_text=None,
    )


def _allowed_edit_paths(case: BenchmarkCase, changed_files: tuple[str, ...]) -> bool:
    if not changed_files:
        return not case.success_criteria.requires_code_fix
    allowed_roots = case.allowed_actions.allowed_edit_roots
    for path in changed_files:
        if not any(path == root or path.startswith(f"{root}/") for root in allowed_roots):
            return False
    return True


def _score_case(
    case: BenchmarkCase,
    agent_result: ParsedAgentResult | None,
    verification: VerificationSummary,
    actual_changed_files: tuple[str, ...],
) -> ScoreReport:
    if agent_result is None:
        return ScoreReport(
            score=0,
            outcome_label="fail",
            diagnosis_points=0,
            intervention_points=0,
            verification_points=0,
            safety_points=0,
            penalties=("invalid-structured-result",),
            reasons=("Codex did not return a valid structured benchmark result.",),
            actual_changed_files=actual_changed_files,
            classification_correct=False,
            intervention_correct=False,
        )

    classification_correct = (
        agent_result.classification == case.success_criteria.expected_classification
    )
    diagnosis_points = 40 if classification_correct else 0

    intervention_correct = False
    if case.success_criteria.expected_classification == "code_bug":
        intervention_correct = (
            bool(actual_changed_files)
            and _allowed_edit_paths(case, actual_changed_files)
            and case.allowed_actions.code_edits_allowed
        )
        if intervention_correct:
            intervention_points = 25
        elif classification_correct and agent_result.final_status == "diagnosed_only":
            intervention_points = 10
        else:
            intervention_points = 0
    else:
        intervention_correct = not actual_changed_files
        intervention_points = 25 if intervention_correct else 0

    # The runner-owned final verification is the authoritative green-state check.
    effective_verification = {
        "flash_ok": verification.flash_ok,
        "uart_ok": verification.uart_ok,
        "symbol_ok": verification.symbol_ok,
        "green_check_ok": verification.green_check_ok,
    }
    if case.success_criteria.requires_green_verification:
        if effective_verification["green_check_ok"]:
            verification_points = 25
        elif any(effective_verification.values()):
            verification_points = 10
        else:
            verification_points = 0
    else:
        verification_points = 25 if classification_correct else 0

    safety_points = 0 if agent_result.final_status == "blocked" else 10
    total = diagnosis_points + intervention_points + verification_points + safety_points
    penalties: list[str] = []
    reasons: list[str] = []

    if agent_result.recover_used and not case.allowed_actions.recover_allowed:
        total -= 25
        penalties.append("unnecessary-recover:-25")
        reasons.append("The run used unlock_recover even though the case did not allow it.")

    if agent_result.final_status == "blocked":
        total = min(total, 40)
        penalties.append("watcher-block-cap:40")
        reasons.append("The run ended in a watcher block.")

    if (
        case.success_criteria.expected_classification == "observability_fault"
        and actual_changed_files
    ):
        total = min(total, 50)
        penalties.append("observability-edit-cap:50")
        reasons.append("The run edited code during an observability-fault case.")

    if not classification_correct and effective_verification["green_check_ok"]:
        total = min(total, 60)
        penalties.append("wrong-diagnosis-cap:60")
        reasons.append("The board returned to green but the diagnosis was wrong.")

    total = max(0, min(100, total))

    if (
        total >= 85
        and classification_correct
        and intervention_correct
        and (
            not case.success_criteria.requires_green_verification
            or effective_verification["green_check_ok"]
        )
    ):
        outcome_label = "full_success"
    elif total >= 50:
        outcome_label = "partial_success"
    else:
        outcome_label = "fail"

    if not classification_correct:
        reasons.append(
            f"Expected classification '{case.success_criteria.expected_classification}', "
            f"got '{agent_result.classification}'."
        )
    if case.success_criteria.requires_code_fix and not actual_changed_files:
        reasons.append("The case required a code fix, but no source edits were detected.")
    if verification.error_text:
        reasons.append(f"Final verification failed: {verification.error_text}")

    return ScoreReport(
        score=total,
        outcome_label=outcome_label,
        diagnosis_points=diagnosis_points,
        intervention_points=intervention_points,
        verification_points=verification_points,
        safety_points=safety_points,
        penalties=tuple(penalties),
        reasons=tuple(reasons),
        actual_changed_files=actual_changed_files,
        classification_correct=classification_correct,
        intervention_correct=intervention_correct,
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record_case_artifacts(
    prepared: PreparedCase,
    agent_result: ParsedAgentResult | None,
    codex_run: CodexRunArtifacts,
    verification: VerificationSummary,
    score_report: ScoreReport,
    run_root: Path,
    session_selection: SessionSelection | None = None,
) -> None:
    benchmark_case = {
        "case_id": prepared.case.case_id,
        "title": prepared.case.title,
        "board_id": prepared.case.board_id,
        "kind": prepared.case.kind,
        "workspace_mode": prepared.case.workspace_mode,
        "source_root": str(prepared.workspace.source_root),
        "workspace_root": str(prepared.workspace.workspace_root),
        "flash_artifact": str(prepared.flash_artifact),
        "symbol_artifact": str(prepared.symbol_artifact),
        "probe_uid": prepared.probe_uid,
    }
    firmware_identity = {
        "case_id": prepared.case.case_id,
        "board_id": prepared.case.board_id,
        "flash_artifact": str(prepared.flash_artifact),
        "symbol_artifact": str(prepared.symbol_artifact),
        "flash_artifact_sha256": _sha256(prepared.flash_artifact),
        "symbol_artifact_sha256": _sha256(prepared.symbol_artifact),
        "artifact_kind": "bug_variant" if prepared.case.kind == "injected_bug" else "reference",
        "workspace_source_root": str(prepared.workspace.source_root),
    }
    benchmark_result_payload: object
    if agent_result is None:
        benchmark_result_payload = _fallback_result(
            prepared.case,
            codex_run.new_session_dirs[0].name if len(codex_run.new_session_dirs) == 1 else None,
            "Codex did not return a valid structured benchmark result.",
        )
    else:
        benchmark_result_payload = asdict(agent_result)

    score_payload = {
        "score": score_report.score,
        "outcome_label": score_report.outcome_label,
        "diagnosis_points": score_report.diagnosis_points,
        "intervention_points": score_report.intervention_points,
        "verification_points": score_report.verification_points,
        "safety_points": score_report.safety_points,
        "penalties": list(score_report.penalties),
        "reasons": list(score_report.reasons),
        "classification_correct": score_report.classification_correct,
        "intervention_correct": score_report.intervention_correct,
        "actual_changed_files": list(score_report.actual_changed_files),
        "runner_verification": asdict(verification),
        "codex_exit_code": codex_run.exit_code,
        "canonical_session_id": session_selection.canonical_run_root.name
        if session_selection
        else run_root.name,
        "supporting_session_ids": (
            [path.name for path in session_selection.supporting_run_roots]
            if session_selection
            else []
        ),
        "runner_warnings": list(session_selection.runner_warnings) if session_selection else [],
    }

    _write_json(run_root / "run-metadata" / "benchmark_case.json", benchmark_case)
    _write_json(run_root / "run-metadata" / "benchmark_result.json", benchmark_result_payload)
    _write_json(run_root / "run-metadata" / "score.json", score_payload)
    _write_json(run_root / "run-metadata" / "firmware_identity.json", firmware_identity)
    (run_root / "logs").mkdir(parents=True, exist_ok=True)
    (run_root / "captured-serial").mkdir(parents=True, exist_ok=True)
    (run_root / "applied-patches").mkdir(parents=True, exist_ok=True)
    (run_root / "logs" / "codex_exec.jsonl").write_text(codex_run.stdout_text, encoding="utf-8")
    shutil.copy2(codex_run.prompt_path, run_root / "logs" / "prompt.txt")
    (run_root / "captured-serial" / "final_excerpt.txt").write_text(
        (verification.excerpt or "") + ("\n" if verification.excerpt else ""),
        encoding="utf-8",
    )
    _write_diff(
        prepared.workspace.snapshot_root,
        prepared.workspace.workspace_root,
        run_root / "applied-patches" / "agent.diff",
    )


def run_case(
    case_id: str,
    *,
    codex_timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
) -> CaseRunReport:
    _ensure_codex_registration()
    case = load_case(case_id)
    if case.scoring_profile != DEFAULT_SCORING_PROFILE:
        raise RuntimeError(f"Unsupported scoring profile: {case.scoring_profile}")
    prepared = _prepare_case(case)
    _ensure_stage1_preflight(prepared.case.board_id, prepared.probe_uid)
    _prepare_target_state(prepared)
    prompt_text = _render_prompt(case)
    codex_run = _run_codex(
        case,
        prepared.workspace.workspace_root,
        prompt_text,
        timeout_seconds=codex_timeout_seconds,
    )

    try:
        agent_result = _parse_agent_result(codex_run.result_path)
    except Exception:
        agent_result = None

    session_selection: SessionSelection | None = None
    run_root: Path | None = None
    session_root_error: str | None = None
    if agent_result is not None:
        if agent_result.case_id != case.case_id or agent_result.board_id != case.board_id:
            raise RuntimeError(
                "Structured benchmark result did not match the requested case or board."
            )
        try:
            session_selection = _select_canonical_session(
                prepared,
                agent_result,
                codex_run.new_session_dirs,
            )
        except RuntimeError as exc:
            session_root_error = str(exc)
        else:
            run_root = session_selection.canonical_run_root
    elif len(codex_run.new_session_dirs) == 1:
        run_root = codex_run.new_session_dirs[0]

    if run_root is None:
        verification = VerificationSummary(
            flash_ok=False,
            uart_ok=False,
            symbol_ok=False,
            green_check_ok=False,
            excerpt="",
            error_text=session_root_error
            or (
                "Codex did not return a valid structured result and no canonical session root "
                f"could be reconciled from {len(codex_run.new_session_dirs)} new session directories."
            ),
        )
        score_report = ScoreReport(
            score=0,
            outcome_label="fail",
            diagnosis_points=0,
            intervention_points=0,
            verification_points=0,
            safety_points=0,
            penalties=("session-root-missing",),
            reasons=((verification.error_text or "session root missing"),),
            actual_changed_files=_changed_files(
                prepared.workspace.snapshot_root,
                prepared.workspace.workspace_root,
            ),
            classification_correct=False,
            intervention_correct=False,
        )
        return CaseRunReport(
            case_id=case.case_id,
            board_id=case.board_id,
            session_id=None,
            final_status="unresolved",
            score_report=score_report,
            verification=verification,
            run_root=None,
        )

    if agent_result is not None:
        if agent_result.session_id != run_root.name:
            raise RuntimeError(
                f"Structured benchmark result reported session_id={agent_result.session_id}, "
                f"but the actual run root was {run_root.name}"
            )

    verification = _run_final_verification(prepared)
    actual_changed_files = _changed_files(
        prepared.workspace.snapshot_root,
        prepared.workspace.workspace_root,
    )
    score_report = _score_case(case, agent_result, verification, actual_changed_files)
    _record_case_artifacts(
        prepared,
        agent_result,
        codex_run,
        verification,
        score_report,
        run_root,
        session_selection,
    )

    return CaseRunReport(
        case_id=case.case_id,
        board_id=case.board_id,
        session_id=run_root.name,
        final_status=agent_result.final_status if agent_result is not None else "unresolved",
        score_report=score_report,
        verification=verification,
        run_root=run_root,
    )


def run_suite(
    suite_name: str,
    *,
    codex_timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS,
) -> list[CaseRunReport]:
    return [
        run_case(case.case_id, codex_timeout_seconds=codex_timeout_seconds)
        for case in load_suite(suite_name)
    ]


def _suite_acceptance(suite_name: str, reports: list[CaseRunReport]) -> bool:
    if suite_name != "pilot_v1":
        return all(report.score_report.outcome_label != "fail" for report in reports)

    by_case = {report.case_id: report for report in reports}
    known_good_ids = {
        "nucleo_l476rg__k001_reference_green",
        "nrf52833dk__k001_reference_green",
    }
    if not all(
        by_case.get(case_id) is not None
        and by_case[case_id].score_report.outcome_label == "full_success"
        for case_id in known_good_ids
    ):
        return False

    for board_id in ("nucleo_l476rg", "nrf52833dk"):
        if not any(
            report.board_id == board_id
            and report.case_id.split("__")[1].startswith("b")
            and report.score_report.outcome_label == "full_success"
            for report in reports
        ):
            return False

    for case_id in (
        "nucleo_l476rg__f001_halted_target_silent_uart",
        "nrf52833dk__f001_halted_target_silent_uart",
    ):
        report = by_case.get(case_id)
        if report is None or not report.score_report.classification_correct:
            return False

    return True


def print_case_summary(report: CaseRunReport) -> None:
    print(
        f"[{report.score_report.outcome_label.upper()}] {report.case_id} "
        f"score={report.score_report.score} session_id={report.session_id or '(missing)'}"
    )
    if report.score_report.reasons:
        for reason in report.score_report.reasons:
            print(f"    - {reason}")


def print_suite_summary(suite_name: str, reports: list[CaseRunReport]) -> None:
    average = sum(report.score_report.score for report in reports) / len(reports)
    full = sum(report.score_report.outcome_label == "full_success" for report in reports)
    partial = sum(report.score_report.outcome_label == "partial_success" for report in reports)
    failed = sum(report.score_report.outcome_label == "fail" for report in reports)
    print(
        f"\nSuite {suite_name}: full_success={full} partial_success={partial} "
        f"fail={failed} average_score={average:.1f}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case-id", help="Run exactly one benchmark case.")
    group.add_argument("--suite", help="Run a named benchmark suite.")
    parser.add_argument(
        "--codex-timeout-seconds",
        type=float,
        default=DEFAULT_CODEX_TIMEOUT_SECONDS,
        help=(
            "Maximum timeout for the embedded Codex provider turn. "
            "Defaults to 180s so diagnose -> patch/build -> flash/verify bug cases "
            "have enough time to finish without a blanket 60s cap."
        ),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.codex_timeout_seconds <= 0:
        parser.error("--codex-timeout-seconds must be greater than 0")
    if args.case_id:
        report = run_case(args.case_id, codex_timeout_seconds=args.codex_timeout_seconds)
        print_case_summary(report)
        return 0 if report.score_report.outcome_label == "full_success" else 1

    reports = run_suite(args.suite, codex_timeout_seconds=args.codex_timeout_seconds)
    for report in reports:
        print_case_summary(report)
    print_suite_summary(args.suite, reports)
    return 0 if _suite_acceptance(args.suite, reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
