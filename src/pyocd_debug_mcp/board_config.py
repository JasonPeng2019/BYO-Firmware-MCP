"""Shared board-config schema and loader for Phase A tools."""

from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pyocd_debug_mcp.probe_families import probe_family_hints, probe_family_label

DEFAULT_BOARD_CONFIG_DIR = (
    Path(__file__).resolve().parents[2] / "boards"
)  # PROJECT-DEFINED (repo layout)
BOARD_CONFIG_SUFFIXES = frozenset({".json", ".yaml", ".yml"})  # PROJECT-DEFINED (supported formats)

# Typed recover-mode selector — board YAML names a backend, never a raw shell command.
# PROJECT-DEFINED (the recover policy vocabulary for board configs).
RECOVER_MODE_BACKEND_MASS_ERASE = "backend_mass_erase"
RECOVER_MODE_MANUAL_ONLY = "manual_only"
SUPPORTED_RECOVER_MODES = {
    RECOVER_MODE_BACKEND_MASS_ERASE,
    RECOVER_MODE_MANUAL_ONLY,
}
_LEGACY_RECOVER_MODE_ALIASES = {"nrf_pyocd_unlock": RECOVER_MODE_BACKEND_MASS_ERASE}


class ConfigError(Exception):
    """Raised when a tracked board-config file is malformed."""


class LegacyPackNameWarning(UserWarning):
    """A legacy profile tried to own package metadata."""


class LegacyRecoverModeWarning(UserWarning):
    """A legacy MCU-named recovery selector was normalized on read."""


@dataclass(frozen=True)
class BoardConfig:
    board_id: str
    display_name: str
    mcu_family: str
    probe_family: str
    pyocd_target: str
    probe_type: str
    probe_hint_terms: tuple[str, ...]
    serial_hint_terms: tuple[str, ...]
    test_addr: int | None
    silicon_id_addr: int | None = None
    silicon_id_expected: int | None = None
    silicon_id_mask: int | None = None
    silicon_id_width_bits: int = 32
    silicon_id_label: str = ""
    default_baudrate: int = 115200
    uart_note: str = ""
    requires_recover_validation: bool = False
    recover_mode: str | None = None
    debug_connect_mode: str | None = None
    debug_clock_hz: int | None = None
    expected_uart_substring: str | None = None
    source_path: Path | None = None


def normalize_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if "," in value:
            return [item.strip() for item in value.split(",") if item.strip()]
        if value.strip():
            return [value.strip()]
        return []
    if isinstance(value, (list, tuple)):
        output = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                output.append(text)
        return output
    raise ConfigError(f"Expected a string or list, got {type(value).__name__}")


def parse_int(value: object, field_name: str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ConfigError(f"Field '{field_name}' must be an int or numeric string")


def validate_width_bits(width_bits: int, field_name: str) -> int:
    if width_bits not in {8, 16, 32}:
        raise ConfigError(f"Field '{field_name}' must be one of: 8, 16, 32")
    return width_bits


def resolve_recover_mode(
    raw_mode: object,
    *,
    requires_recover_validation: bool,
) -> str | None:
    """Resolve the typed recover-mode selector for a board.

    An explicit ``recover_mode`` is honored and validated against
    ``SUPPORTED_RECOVER_MODES``. Legacy MCU-named selectors are accepted only as
    a non-authorizing read compatibility alias. Missing recovery policy never
    infers behavior from a part or family name.
    """
    if raw_mode is not None:
        recover_mode = str(raw_mode).strip().lower()
        if not recover_mode:
            return None
        legacy = _LEGACY_RECOVER_MODE_ALIASES.get(recover_mode)
        if legacy is not None:
            warnings.warn(
                f"Legacy recover_mode '{recover_mode}' is deprecated; use '{legacy}'.",
                LegacyRecoverModeWarning,
                stacklevel=2,
            )
            recover_mode = legacy
        if recover_mode not in SUPPORTED_RECOVER_MODES:
            supported = ", ".join(sorted(SUPPORTED_RECOVER_MODES))
            raise ConfigError(f"Field 'recover_mode' must be one of: {supported}")
        return recover_mode

    if not requires_recover_validation:
        return None
    return RECOVER_MODE_MANUAL_ONLY


def tokenize_hint_text(*values: str) -> set[str]:
    terms: set[str] = set()
    for value in values:
        for token in re.findall(r"[a-z0-9]+", value.lower()):
            if len(token) >= 3:
                terms.add(token)
    return terms


def load_board_config_document(path: Path) -> dict[str, object]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not parse board config {path}: {exc}") from exc
    elif suffix in BOARD_CONFIG_SUFFIXES:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ConfigError(
                f"PyYAML is required to load {path.name}. Run 'uv sync' from the repo root or use JSON."
            ) from exc
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"Could not parse board config {path}: {exc}") from exc
    else:
        raise ConfigError(f"Unsupported board config format for {path}. Use .json, .yaml, or .yml.")

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a single YAML or JSON object")
    return data


def make_board_config(raw: dict[str, object], source_path: Path | None) -> BoardConfig:
    forbidden_session_fields = {
        "project_path",
        "user_project_path",
        "build_command",
        "user_build_command",
        "artifact_path",
        "output_artifact_path",
        "user_output_artifact",
        "reference_firmware_path",
        "recovery_image_path",
    }
    present_forbidden = sorted(field for field in forbidden_session_fields if field in raw)
    if present_forbidden:
        raise ConfigError(
            "Board config contains project/session-scoped fields that do not belong in boards/<board>.yaml: "
            f"{', '.join(present_forbidden)}. Supply these later as runtime/session inputs instead."
        )

    required_fields = ["board_id", "display_name", "mcu_family", "probe_family", "pyocd_target"]
    missing = [field for field in required_fields if not raw.get(field)]
    if missing:
        raise ConfigError(f"Missing required board config fields: {', '.join(missing)}")

    board_id = str(raw["board_id"]).strip()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", board_id):
        raise ConfigError("board_id must be 1-64 lowercase letters, numbers, or underscores")

    display_name = str(raw["display_name"]).strip()
    if not display_name or len(display_name) > 100:
        raise ConfigError("display_name must be a non-empty string of at most 100 characters")
    mcu_family = str(raw["mcu_family"]).strip().lower()
    probe_family = str(raw["probe_family"]).strip().lower()
    pyocd_target = str(raw["pyocd_target"]).strip()
    if "pack_name" in raw:
        source = f" in {source_path}" if source_path is not None else ""
        warnings.warn(
            "Legacy field 'pack_name'"
            f"{source} is deprecated and ignored; packs/manifest.yaml is the "
            "authoritative owner of device-support package metadata.",
            LegacyPackNameWarning,
            stacklevel=2,
        )
    probe_type = str(
        raw.get("probe_type") or probe_family_label(probe_family)
    ).strip()

    test_addr = None
    if raw.get("test_read_address") is not None:
        test_addr = parse_int(raw["test_read_address"], "test_read_address")

    default_baudrate = parse_int(raw.get("serial_baudrate", 115200), "serial_baudrate")
    uart_note = str(raw.get("uart_note", "")).strip()

    requires_recover_validation = bool(raw.get("requires_recover_validation", False))

    recover_mode = resolve_recover_mode(
        raw.get("recover_mode"),
        requires_recover_validation=requires_recover_validation,
    )

    debug_connect_mode = None
    if raw.get("debug_connect_mode") is not None:
        debug_connect_mode = str(raw["debug_connect_mode"]).strip().lower()
        if debug_connect_mode not in {"attach", "halt", "pre-reset", "under-reset"}:
            raise ConfigError(
                "Field 'debug_connect_mode' must be one of: attach, halt, pre-reset, under-reset"
            )
    debug_clock_hz = None
    if raw.get("debug_clock_hz") is not None:
        debug_clock_hz = parse_int(raw["debug_clock_hz"], "debug_clock_hz")
        if debug_clock_hz <= 0:
            raise ConfigError("Field 'debug_clock_hz' must be a positive integer")

    probe_terms = set(normalize_list(raw.get("probe_hint_terms")))
    serial_terms = set(normalize_list(raw.get("serial_hint_terms")))
    default_terms = tokenize_hint_text(board_id, display_name, mcu_family, pyocd_target)
    probe_terms.update(default_terms)
    serial_terms.update(default_terms)
    probe_terms.update(probe_family_hints(probe_family))
    serial_terms.update(probe_family_hints(probe_family))
    serial_terms.add("virtual com")

    expected_uart_substring = None
    if raw.get("expected_uart_substring"):
        expected_uart_substring = str(raw["expected_uart_substring"]).strip()
    else:
        patterns = normalize_list(raw.get("reference_uart_patterns"))
        if patterns:
            expected_uart_substring = patterns[0]

    silicon_fields_present = any(
        field in raw
        for field in (
            "silicon_id_address",
            "silicon_id_expected",
            "silicon_id_mask",
            "silicon_id_width_bits",
            "silicon_id_label",
        )
    )
    silicon_id_addr = None
    silicon_id_expected = None
    silicon_id_mask = None
    silicon_id_width_bits = 32
    silicon_id_label = ""

    if silicon_fields_present:
        if raw.get("silicon_id_address") is None or raw.get("silicon_id_expected") is None:
            raise ConfigError(
                "Board config silicon identity requires both 'silicon_id_address' and "
                "'silicon_id_expected' when any silicon_id_* field is present"
            )
        silicon_id_addr = parse_int(raw["silicon_id_address"], "silicon_id_address")
        silicon_id_expected = parse_int(raw["silicon_id_expected"], "silicon_id_expected")
        silicon_id_width_bits = validate_width_bits(
            parse_int(raw.get("silicon_id_width_bits", 32), "silicon_id_width_bits"),
            "silicon_id_width_bits",
        )
        full_mask = (1 << silicon_id_width_bits) - 1
        silicon_id_mask = (
            parse_int(raw.get("silicon_id_mask", full_mask), "silicon_id_mask") & full_mask
        )
        silicon_id_expected &= full_mask
        silicon_id_label = str(raw.get("silicon_id_label") or "silicon identity").strip()

    return BoardConfig(
        board_id=board_id,
        display_name=display_name,
        mcu_family=mcu_family,
        probe_family=probe_family,
        pyocd_target=pyocd_target,
        probe_type=probe_type,
        probe_hint_terms=tuple(sorted(term.lower() for term in probe_terms if term)),
        serial_hint_terms=tuple(sorted(term.lower() for term in serial_terms if term)),
        test_addr=test_addr,
        silicon_id_addr=silicon_id_addr,
        silicon_id_expected=silicon_id_expected,
        silicon_id_mask=silicon_id_mask,
        silicon_id_width_bits=silicon_id_width_bits,
        silicon_id_label=silicon_id_label,
        default_baudrate=default_baudrate,
        uart_note=uart_note,
        requires_recover_validation=requires_recover_validation,
        recover_mode=recover_mode,
        debug_connect_mode=debug_connect_mode,
        debug_clock_hz=debug_clock_hz,
        expected_uart_substring=expected_uart_substring,
        source_path=source_path,
    )


def load_board_configs_from_paths(paths: Iterable[Path]) -> list[BoardConfig]:
    boards: list[BoardConfig] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise ConfigError(f"Board config not found: {path}")
        document = load_board_config_document(path)
        boards.append(make_board_config(document, path))
    return boards


def iter_board_config_paths(board_config_dir: Path) -> list[Path]:
    if not board_config_dir.exists():
        raise ConfigError(f"Board config directory not found: {board_config_dir}")
    if not board_config_dir.is_dir():
        raise ConfigError(f"Board config path is not a directory: {board_config_dir}")

    paths = [
        path.resolve()
        for path in sorted(board_config_dir.iterdir())
        if path.is_file() and path.suffix.lower() in BOARD_CONFIG_SUFFIXES
    ]
    if not paths:
        raise ConfigError(f"No board config files found in: {board_config_dir}")
    return paths


def preview_board_config_paths(board_config_dir: Path) -> list[Path]:
    if not board_config_dir.exists() or not board_config_dir.is_dir():
        return []
    return [
        path.resolve()
        for path in sorted(board_config_dir.iterdir())
        if path.is_file() and path.suffix.lower() in BOARD_CONFIG_SUFFIXES
    ]


def load_default_board_configs(board_config_dir: Path) -> list[BoardConfig]:
    default_paths = [
        path
        for path in iter_board_config_paths(board_config_dir)
        if not path.stem.startswith("example_")
    ]
    if not default_paths:
        raise ConfigError(
            f"No non-example board config files found in: {board_config_dir}. "
            "Add board files or pass --board-config."
        )
    return load_board_configs_from_paths(default_paths)


def merge_board_lists(builtins: list[BoardConfig], customs: list[BoardConfig]) -> list[BoardConfig]:
    merged: list[BoardConfig] = []
    seen: set[str] = set()
    for board in builtins + customs:
        if board.board_id in seen:
            raise ConfigError(f"Duplicate board_id detected: {board.board_id}")
        seen.add(board.board_id)
        merged.append(board)
    return merged


def select_boards_by_id(
    boards: list[BoardConfig], requested_ids: Iterable[str]
) -> list[BoardConfig]:
    requested = [board_id.strip().lower() for board_id in requested_ids if board_id.strip()]
    if not requested:
        return boards

    selected = [board for board in boards if board.board_id in requested]
    missing = sorted(set(requested) - {board.board_id for board in selected})
    if missing:
        raise ConfigError(f"Requested board_id values not found: {', '.join(missing)}")
    return selected


def load_selected_board_configs(
    board_config_dir: Path,
    extra_paths: Iterable[Path] = (),
    requested_ids: Iterable[str] = (),
) -> list[BoardConfig]:
    default_boards = load_default_board_configs(board_config_dir)
    custom_boards = load_board_configs_from_paths(extra_paths)
    boards = merge_board_lists(default_boards, custom_boards)
    return select_boards_by_id(boards, requested_ids)
