"""Strict project-owned analysis settings and selected build identity.

Command strings remain opaque. This module never invokes a compiler or changes
the compilation database, source files, or clangd configuration.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import yaml

SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".c++"})
HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".h++", ".inc", ".inl", ".tpp"})


@dataclass(frozen=True)
class AnalysisProblem:
    code: str
    message: str
    remedy: str
    field: str | None = None
    backend: str | None = None

    def to_payload(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message, "remedy": self.remedy}
        if self.field is not None:
            result["field"] = self.field
        return result


class AnalysisConfigError(ValueError):
    def __init__(self, problem: AnalysisProblem) -> None:
        super().__init__(problem.message)
        self.problem = problem


def _fail(code: str, message: str, remedy: str, field: str | None = None) -> NoReturn:
    raise AnalysisConfigError(AnalysisProblem("analysis/" + code, message, remedy, field))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            _fail(
                "config-invalid",
                f"Duplicate JSON field {name}.",
                "Remove the duplicate field.",
                name,
            )
        result[name] = value
    return result


def _finite_json(value: Any, field: str = "") -> None:
    if type(value) is float and not math.isfinite(value):
        _fail(
            "config-invalid",
            f"Non-finite JSON number at {field or 'byo-analysis.json'}.",
            "Use a finite JSON number.",
            field or "byo-analysis.json",
        )
    if isinstance(value, dict):
        for name, item in value.items():
            _finite_json(item, f"{field}.{name}" if field else name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _finite_json(item, f"{field}[{index}]")


def strict_json(data: bytes) -> Any:
    # Decode first to retain the offending field for NaN/Infinity and numeric
    # overflow, then reject them before any consumer receives the parsed value.
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs)
    _finite_json(value)
    return value


def _object(value: Any, label: str, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("config-invalid", f"{label} must be an object.", "Use a JSON object.", label)
    for name in value:
        if name not in names:
            full = f"{label}.{name}" if label != "byo-analysis.json" else name
            _fail(
                "config-invalid", f"Unknown field {full}.", "Remove this unsupported field.", full
            )
    return value


def resolve_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        _fail(
            "config-invalid",
            f"{label} must be a nonempty local path string.",
            "Supply a local filesystem path.",
            label,
        )
    if "://" in value or value.startswith("\\\\") or value.startswith("//"):
        _fail(
            "config-invalid",
            f"{label} must be a local path, not a URI/network path.",
            "Supply a local filesystem path.",
            label,
        )
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


@dataclass(frozen=True)
class CompilationEntry:
    source: Path
    directory: Path
    arguments: tuple[str, ...] | None
    command: str | None
    raw: dict[str, Any]


@dataclass
class AnalysisConfigSnapshot:
    project_root: Path
    config_path: Path
    config_present: bool = False
    config_sha256: str | None = None
    database_path: Path | None = None
    database_sha256: str | None = None
    database_bytes: bytes | None = None
    database_identity: tuple[Any, ...] = ()
    entries: tuple[CompilationEntry, ...] = ()
    cppcheck: dict[str, Any] = field(
        default_factory=lambda: {"enable_style": False, "timeout_seconds": 120.0}
    )
    query_driver_paths: tuple[Path, ...] = ()
    problems: list[AnalysisProblem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    clangd_files: dict[Path, str | None] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            str(self.project_root),
            self.config_present,
            self.config_sha256,
            str(self.database_path),
            self.database_sha256,
            self.database_identity,
            tuple(map(str, self.query_driver_paths)),
            tuple(sorted((str(p), h) for p, h in self.clangd_files.items())),
        )

    @property
    def clangd_problems(self) -> list[AnalysisProblem]:
        return [p for p in self.problems if p.backend != "cppcheck"]


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return first == second


def _database(snapshot: AnalysisConfigSnapshot, settings: dict[str, Any]) -> None:
    root = snapshot.project_root
    if "compilation_database" in settings:
        chosen = resolve_path(root, settings["compilation_database"], "compilation_database")
    else:
        candidates: list[Path] = []
        for path in (root / "compile_commands.json", root / "build/compile_commands.json"):
            if path.exists() and not any(_same_file(path, p) for p in candidates):
                candidates.append(path.resolve())
        if not candidates:
            _fail(
                "database-missing",
                "No compile_commands.json in project root or build/.",
                "Export the selected native build database, then set compilation_database in byo-analysis.json.",
            )
        if len(candidates) > 1:
            _fail(
                "database-ambiguous",
                "Multiple distinct compilation databases exist.",
                "Set compilation_database explicitly in byo-analysis.json.",
                "compilation_database",
            )
        chosen = candidates[0]
    snapshot.database_path = chosen
    if chosen.name != "compile_commands.json":
        _fail(
            "database-invalid",
            "Selected database must be named compile_commands.json.",
            "Re-export with the native filename.",
            "compilation_database",
        )
    try:
        before = chosen.stat()
        data = chosen.read_bytes()
        after = chosen.stat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_size,
        ):
            _fail(
                "database-invalid",
                "Selected database changed while reading its snapshot.",
                "Finish exporting the database and retry.",
            )
        snapshot.database_identity = (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
        snapshot.database_bytes = data
        snapshot.database_sha256 = sha256_bytes(data)
        records = strict_json(data)
    except (OSError, UnicodeError, ValueError) as exc:
        _fail(
            "database-invalid",
            f"Cannot read selected database: {exc}",
            "Export a readable valid compilation database.",
            "compilation_database",
        )
    if not isinstance(records, list) or not records:
        _fail(
            "database-invalid",
            "Database must contain a nonempty array of commands.",
            "Export commands for the selected build.",
            "compilation_database",
        )
    entries: list[CompilationEntry] = []
    for index, record in enumerate(records):
        label = f"compilation_database[{index}]"
        if not isinstance(record, dict):
            _fail(
                "database-invalid",
                f"{label} must be a command object.",
                "Re-export the database.",
                label,
            )
        try:
            directory = resolve_path(root, record.get("directory"), label + ".directory")
            source = resolve_path(directory, record.get("file"), label + ".file")
        except AnalysisConfigError as exc:
            _fail(
                "database-invalid",
                exc.problem.message,
                "Re-export valid directory/file paths.",
                exc.problem.field,
            )
        if not directory.is_dir() or not source.is_file():
            _fail(
                "database-invalid",
                f"{label} directory or source does not exist: {source}",
                "Build generated inputs or re-export existing paths.",
                label,
            )
        arguments = record.get("arguments")
        command = record.get("command")
        if "arguments" in record and (
            not isinstance(arguments, list)
            or not arguments
            or not isinstance(arguments[0], str)
            or not arguments[0].strip()
            or not all(isinstance(arg, str) and "\0" not in arg for arg in arguments)
        ):
            _fail(
                "database-invalid",
                f"{label}.arguments must be a nonempty string array.",
                "Re-export argv without shell execution.",
                label + ".arguments",
            )
        if "command" in record and (
            not isinstance(command, str) or not command.strip() or "\0" in command
        ):
            _fail(
                "database-invalid",
                f"{label}.command must be a nonempty string.",
                "Re-export the command.",
                label + ".command",
            )
        if arguments is None and command is None:
            _fail(
                "database-invalid",
                f"{label} has neither arguments nor command.",
                "Export a compiler command.",
                label,
            )
        entry = CompilationEntry(
            source, directory, tuple(arguments) if arguments else None, command, dict(record)
        )
        previous = next((e for e in entries if _same_file(source, e.source)), None)
        if previous:
            representation = entry.arguments if entry.arguments is not None else entry.command
            old = previous.arguments if previous.arguments is not None else previous.command
            if directory != previous.directory or representation != old:
                _fail(
                    "database-conflict",
                    f"Conflicting commands for {source}.",
                    "Export/select one build configuration.",
                    label,
                )
        else:
            entries.append(entry)
    snapshot.entries = tuple(entries)


def clangd_user_config_path() -> Path:
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            / "clangd/config.yaml"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library/Preferences/clangd/config.yaml"
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "clangd/config.yaml"
    )


def _conditions(fragment: dict[str, Any], path: Path, config: Path, user: bool) -> bool:
    conditions = fragment.get("If", {})
    if not isinstance(conditions, dict) or set(conditions) - {"PathMatch", "PathExclude"}:
        raise ValueError("unsupported/invalid If conditions")
    subject = path.as_posix() if user else path.relative_to(config.parent).as_posix()
    for key, value in conditions.items():
        patterns = value if isinstance(value, list) else [value]
        if not all(isinstance(p, str) for p in patterns):
            raise ValueError("path conditions must be strings")
        matches = any(re.fullmatch(p, subject) is not None for p in patterns)
        if (key == "PathMatch" and not matches) or (key == "PathExclude" and matches):
            return False
    return True


def validate_clangd_configuration(snapshot: AnalysisConfigSnapshot, source: Path) -> None:
    """Evaluate database selection in actual applicable fragments, low to high precedence."""
    user_path = clangd_user_config_path()
    files = [directory / ".clangd" for directory in reversed(source.parent.parents)]
    files += [source.parent / ".clangd", user_path]
    selected: tuple[Any, Path] | None = None
    for config in files:
        if config in snapshot.clangd_files:
            # Still evaluate it for each source: fragment conditions are file-specific.
            pass
        if not config.is_file():
            snapshot.clangd_files[config] = None
            continue
        try:
            data = config.read_bytes()
            snapshot.clangd_files[config] = sha256_bytes(data)
            for fragment in yaml.safe_load_all(data.decode("utf-8")):
                if fragment is None:
                    continue
                if not isinstance(fragment, dict):
                    raise ValueError("fragment must be a mapping")
                if not _conditions(fragment, source, config, config == user_path):
                    continue
                flags = fragment.get("CompileFlags", {})
                if not isinstance(flags, dict):
                    raise ValueError("CompileFlags must be a mapping")
                if "CompilationDatabase" in flags:
                    selected = flags["CompilationDatabase"], config
                if (
                    set(flags) - {"CompilationDatabase"}
                    or fragment.get("Index")
                    or fragment.get("Diagnostics")
                ):
                    warning = f"Retained compile/index/diagnostic settings in {config}; results reflect these settings."
                    if warning not in snapshot.warnings:
                        snapshot.warnings.append(warning)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            problem = AnalysisProblem(
                "analysis/clangd-config-conflict",
                f"Cannot establish effective clangd configuration for {source}: {config}: {exc}",
                "Fix the applicable .clangd/user fragment so database selection is explicit and valid.",
            )
            if problem not in snapshot.problems:
                snapshot.problems.append(problem)
            return
    if selected is None or snapshot.database_path is None:
        return
    value, config = selected
    valid = False
    if isinstance(value, str) and value == "Ancestors":
        for directory in (source.parent, *source.parent.parents):
            found = next(
                (
                    p
                    for p in (
                        directory / "compile_commands.json",
                        directory / "build/compile_commands.json",
                    )
                    if p.is_file()
                ),
                None,
            )
            if found is not None:
                valid = _same_file(found, snapshot.database_path)
                break
    elif isinstance(value, str) and value != "None":
        path = Path(value)
        candidate = (path if path.is_absolute() else config.parent / path) / "compile_commands.json"
        valid = _same_file(candidate.resolve(), snapshot.database_path)
    if not valid:
        snapshot.problems.append(
            AnalysisProblem(
                "analysis/clangd-config-conflict",
                f"Effective CompilationDatabase in {config} conflicts with selected database for {source}.",
                "Align that fragment with the selected compile_commands.json; do not disable target configuration.",
            )
        )


def load_analysis_config(project_root: str | Path) -> AnalysisConfigSnapshot:
    root = Path(project_root).resolve()
    snapshot = AnalysisConfigSnapshot(root, root / "byo-analysis.json")
    settings: dict[str, Any] = {}
    try:
        if snapshot.config_path.exists():
            snapshot.config_present = True
            data = snapshot.config_path.read_bytes()
            snapshot.config_sha256 = sha256_bytes(data)
            settings = _object(
                strict_json(data),
                "byo-analysis.json",
                {"schema_version", "compilation_database", "cppcheck", "clangd"},
            )
            if type(settings.get("schema_version")) is not int or settings["schema_version"] != 1:
                _fail(
                    "config-invalid",
                    "schema_version must be integer 1.",
                    "Set schema_version to 1.",
                    "schema_version",
                )
        cpp = _object(
            settings.get("cppcheck", {}),
            "cppcheck",
            {"platform_file", "suppressions_file", "enable_style", "timeout_seconds"},
        )
        clang = _object(settings.get("clangd", {}), "clangd", {"query_driver_paths"})
        if "enable_style" in cpp and type(cpp["enable_style"]) is not bool:
            _fail(
                "config-invalid",
                "cppcheck.enable_style must be a boolean.",
                "Use true or false.",
                "cppcheck.enable_style",
            )
        timeout = cpp.get("timeout_seconds", 120)
        try:
            valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            _fail(
                "config-invalid",
                "cppcheck.timeout_seconds must be positive and finite.",
                "Use a finite positive number.",
                "cppcheck.timeout_seconds",
            )
        snapshot.cppcheck.update(cpp)
        for name in ("platform_file", "suppressions_file"):
            if name in cpp:
                path = resolve_path(root, cpp[name], "cppcheck." + name)
                snapshot.cppcheck[name] = path
                try:
                    path.read_bytes()
                except OSError:
                    snapshot.problems.append(
                        AnalysisProblem(
                            "analysis/config-invalid",
                            f"cppcheck.{name} is not readable: {path}",
                            "Supply an existing readable Cppcheck resource.",
                            "cppcheck." + name,
                            "cppcheck",
                        )
                    )
        drivers = clang.get("query_driver_paths", [])
        if not isinstance(drivers, list):
            _fail(
                "config-invalid",
                "clangd.query_driver_paths must be an array.",
                "Supply an array of exact executables.",
                "clangd.query_driver_paths",
            )
        resolved: list[Path] = []
        for index, raw in enumerate(drivers):
            label = f"clangd.query_driver_paths[{index}]"
            path = resolve_path(root, raw, label)
            # clangd uses comma-separated globs; these cannot represent literal paths.
            if (
                any(c in str(path) for c in "*?[],{}")
                or (os.name == "nt" and path.suffix.lower() in {".bat", ".cmd"})
                or not path.is_file()
                or not os.access(path, os.X_OK)
            ):
                snapshot.problems.append(
                    AnalysisProblem(
                        "analysis/driver-invalid",
                        f"{label} must name a representable exact compiler executable: {path}",
                        "Use an existing executable path without glob characters or comma separators.",
                        label,
                    )
                )
            else:
                resolved.append(path)
        snapshot.query_driver_paths = tuple(resolved)
        _database(snapshot, settings)
    except AnalysisConfigError as exc:
        snapshot.problems.append(exc.problem)
    except (OSError, UnicodeError, ValueError) as exc:
        snapshot.problems.append(
            AnalysisProblem(
                "analysis/config-invalid",
                f"Cannot read byo-analysis.json: {exc}",
                "Fix the project JSON using schema_version 1.",
            )
        )
    for source in {entry.source for entry in snapshot.entries}:
        validate_clangd_configuration(snapshot, source)
    return snapshot
