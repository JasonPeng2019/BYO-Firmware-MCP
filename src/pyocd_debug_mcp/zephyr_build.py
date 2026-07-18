"""Cross-platform Zephyr workspace/bootstrap/build helper."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pyocd_debug_mcp.kernel.processes import run_owned
from urllib.error import URLError
from urllib.request import urlopen

from .local_env import load_local_env

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANAGED_ZEPHYR_REPO = "https://github.com/zephyrproject-rtos/zephyr.git"
DEFAULT_MANAGED_ZEPHYR_REF = "v4.3.0"
DEFAULT_TOOLCHAIN = "arm-zephyr-eabi"
DEFAULT_STEP_TIMEOUT_SECONDS = 1800
MANAGED_WORKSPACE_MARKER = "." + "firmware-cli-managed-workspace.json"
MANAGED_WORKSPACE_COMPLETE = "." + "firmware-cli-managed-workspace-complete.json"
MANAGED_WORKSPACE_OWNER = "pyocd-debug-mcp-zephyr-build"
MANAGED_SDK_MARKER = "." + "firmware-cli-managed-sdk.json"
MANAGED_SDK_OWNER = "pyocd-debug-mcp-zephyr-build"
CACHE_LOCK_SUFFIX = "." + "firmware-cli.lock"
REQUIREMENTS_MARKER = "." + "zephyr-workspace-requirements.json"
BUILD_OUTPUT_MARKER = "." + "firmware-cli-build-output.json"
BUILD_OUTPUT_OWNER = "pyocd-debug-mcp-zephyr-build"
CACHE_LOCK_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class CandidatePath:
    path: Path
    source: str


@dataclass(frozen=True)
class ZephyrRuntime:
    workspace_dir: Path
    workspace_source: str
    sdk_dir: Path
    sdk_source: str
    west_python: Path
    managed_workspace_dir: Path


@contextmanager
def _cache_lock(resource: Path, *, timeout_seconds: float = CACHE_LOCK_TIMEOUT_SECONDS):
    """Serialize mutation of a shared cache using an OS-released file lock."""

    resource = resource.expanduser().resolve()
    resource.parent.mkdir(parents=True, exist_ok=True)
    lock_path = resource.parent / f".{resource.name}{CACHE_LOCK_SUFFIX}"
    deadline = time.monotonic() + timeout_seconds
    try:
        with lock_path.open("xb") as created:
            created.write(b" ")
    except FileExistsError:
        pass
    handle = lock_path.open("r+b")
    acquired = False
    try:
        while not acquired:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Timed out waiting for managed cache lock: {resource}"
                    ) from exc
                time.sleep(0.05)
        owner = {
            "schema_version": 1,
            "pid": os.getpid(),
            "resource": str(resource),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write((json.dumps(owner, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        handle.seek(0)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _default_cache_root() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "firmware-cli"
        return Path.home() / "AppData" / "Local" / "firmware-cli"
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / "firmware-cli"
    return Path.home() / ".cache" / "firmware-cli"


def _managed_ref_slug(ref: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in ref).strip("-") or "default"


def _default_managed_workspace_dir(ref: str) -> Path:
    return _default_cache_root() / "zephyr" / _managed_ref_slug(ref) / "workspace"


def _default_west_venv_dir() -> Path:
    return _default_cache_root() / "zephyr" / "west-venv"


def _default_managed_sdk_dir() -> Path:
    return _default_cache_root() / "zephyr" / "sdk"


def _venv_python_path(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _path_has_spaces(path: Path) -> bool:
    return " " in str(path)


def _path_is_long_for_windows_build(path: Path) -> bool:
    return sys.platform == "win32" and len(str(path.resolve())) >= 100


def _should_use_scratch_build(app_dir: Path, build_dir: Path) -> bool:
    return (
        _path_has_spaces(app_dir)
        or _path_has_spaces(build_dir)
        or _path_is_long_for_windows_build(app_dir)
        or _path_is_long_for_windows_build(build_dir)
    )


def _copy_adjacent_common_for_scratch(app_dir: Path, scratch_root: Path) -> None:
    common_dir = app_dir.parent / "common"
    if common_dir.is_dir():
        shutil.copytree(common_dir, scratch_root / "common")


def _validate_build_path_relationship(app_dir: Path, build_dir: Path) -> None:
    """Reject any output target whose cleanup could delete application source."""

    root = Path(build_dir.anchor).resolve()
    if build_dir == root or build_dir == Path.home().resolve():
        raise RuntimeError("Build dir must be a dedicated non-root, non-home output directory.")
    if app_dir == build_dir or app_dir.is_relative_to(build_dir):
        raise RuntimeError(
            "Build dir must not equal or contain the app dir; choose a dedicated child "
            "or sibling output directory."
        )


def _build_output_marker_payload(app_dir: Path, build_dir: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "owner": BUILD_OUTPUT_OWNER,
        "app_dir": str(app_dir.resolve()),
        "build_dir": str(build_dir.resolve()),
    }


def _build_dir_is_owned(app_dir: Path, build_dir: Path) -> bool:
    marker_path = build_dir / BUILD_OUTPUT_MARKER
    if (
        _path_is_link_or_junction(build_dir)
        or not marker_path.is_file()
        or _path_is_link_or_junction(marker_path)
    ):
        return False
    try:
        if not marker_path.resolve().is_relative_to(build_dir.resolve()):
            return False
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return payload == _build_output_marker_payload(app_dir, build_dir)


def _allowed_unowned_build_entries(build_dir: Path) -> set[str]:
    marker_path = build_dir / BUILD_OUTPUT_MARKER
    return {
        ".gitkeep",
        ".gitignore",
        marker_path.with_suffix(".tmp").name,
    }


def _preflight_build_request(app_value: str | Path, build_value: str | Path) -> tuple[Path, Path]:
    """Reject invalid or foreign output before dependency provisioning."""

    app_dir = Path(app_value).expanduser().resolve()
    requested_build_dir = Path(build_value).expanduser()
    if not app_dir.is_dir():
        raise RuntimeError(f"App dir does not exist: {app_dir}")
    if requested_build_dir.exists() and _path_is_link_or_junction(requested_build_dir):
        raise RuntimeError(f"Refusing linked or junction build directory: {requested_build_dir}")
    build_dir = requested_build_dir.resolve()
    _validate_build_path_relationship(app_dir, build_dir)
    if not build_dir.exists():
        return app_dir, build_dir
    marker_path = build_dir / BUILD_OUTPUT_MARKER
    if marker_path.exists():
        if not _build_dir_is_owned(app_dir, build_dir):
            raise RuntimeError(
                "Build directory ownership does not match this app and output path; "
                f"choose a dedicated empty directory: {build_dir}"
            )
        return app_dir, build_dir
    allowed = _allowed_unowned_build_entries(build_dir)
    if any(child.name not in allowed for child in build_dir.iterdir()):
        raise RuntimeError(
            "Refusing to adopt nonempty unowned build directory; preserve its contents and "
            f"choose a dedicated empty directory: {build_dir}"
        )
    return app_dir, build_dir


def _claim_build_dir(app_dir: Path, build_dir: Path) -> None:
    _preflight_build_request(app_dir, build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    marker_path = build_dir / BUILD_OUTPUT_MARKER
    if marker_path.exists():
        if not _build_dir_is_owned(app_dir, build_dir):
            raise RuntimeError(
                "Build directory ownership does not match this app and output path; "
                f"choose a dedicated empty directory: {build_dir}"
            )
        return
    allowed_initial_entries = _allowed_unowned_build_entries(build_dir)
    foreign_entries = [
        child.name for child in build_dir.iterdir() if child.name not in allowed_initial_entries
    ]
    if foreign_entries:
        raise RuntimeError(
            "Refusing to adopt nonempty unowned build directory; preserve its contents and "
            f"choose a dedicated empty directory: {build_dir}"
        )
    temporary = marker_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            _build_output_marker_payload(app_dir, build_dir),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker_path)


def _copy_app_for_scratch(app_dir: Path, build_dir: Path, destination: Path) -> None:
    # The FirmStore layout remains the only owner of the project-state directory
    # spelling; identify hidden state roots generically when making a disposable
    # source-only scratch copy.
    generated_root_names = frozenset(
        {
            "." + "firm",
            ".git",
            ".west",
            ".pytest_cache",
            "__pycache__",
            "acceptance",
            "dist",
        }
    )

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory).resolve()
        ignored: set[str] = set()
        for name in names:
            child = (current / name).resolve()
            if child == build_dir:
                ignored.add(name)
            elif current == app_dir and name in generated_root_names:
                ignored.add(name)
        return ignored

    shutil.copytree(app_dir, destination, ignore=ignore)


def _is_zephyr_workspace(path: Path) -> bool:
    zephyr_dir = path / "zephyr"
    sdk_version_path = zephyr_dir / "SDK_VERSION"
    return (
        path.is_dir()
        and zephyr_dir.is_dir()
        and (zephyr_dir / "CMakeLists.txt").is_file()
        and sdk_version_path.is_file()
        and bool(sdk_version_path.read_text(encoding="utf-8", errors="strict").strip())
    )


def _installed_sdk_version(path: Path) -> str | None:
    version_path = path / "sdk_version"
    if not path.is_dir() or not version_path.is_file():
        return None
    lines = version_path.read_text(encoding="utf-8", errors="strict").splitlines()
    version = lines[0].strip() if lines else ""
    return version or None


def _is_zephyr_sdk(path: Path, *, expected_version: str | None = None) -> bool:
    version = _installed_sdk_version(path)
    return version is not None and (expected_version is None or version == expected_version)


def _sdk_has_toolchain(path: Path, toolchain: str) -> bool:
    """Return whether an SDK contains the requested compiler component."""

    toolchain_dir = path / toolchain
    if not toolchain_dir.is_dir():
        return False
    compiler_name = f"{toolchain}-gcc.exe" if sys.platform == "win32" else f"{toolchain}-gcc"
    return (toolchain_dir / "bin" / compiler_name).is_file()


def _sdk_toolchain_runs(path: Path, toolchain: str) -> bool:
    compiler_name = f"{toolchain}-gcc.exe" if sys.platform == "win32" else f"{toolchain}-gcc"
    compiler = path / toolchain / "bin" / compiler_name
    try:
        result = run_owned(
            [str(compiler), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _sdk_versions_are_patch_compatible(installed: str, requested: str) -> bool:
    """Allow an installed SDK from the same major/minor release line.

    Zephyr's SDK_VERSION is a recommended release, while vendor NCS bundles can
    intentionally carry another patch from the same SDK line. Reusing that
    complete local toolchain is preferable to an unnecessary network install.
    """

    try:
        installed_parts = tuple(int(part) for part in installed.split("."))
        requested_parts = tuple(int(part) for part in requested.split("."))
    except ValueError:
        return False
    return len(installed_parts) >= 2 and installed_parts[:2] == requested_parts[:2]


def _iter_zephyr_workspace_candidates(
    *,
    explicit_workspace_dir: Path | None,
    managed_workspace_dir: Path,
) -> list[CandidatePath]:
    candidates: list[CandidatePath] = []
    seen: set[Path] = set()

    def add(path: Path | None, source: str) -> None:
        if path is None:
            return
        resolved = path.expanduser().resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(CandidatePath(path=resolved, source=source))

    add(explicit_workspace_dir, "--workspace-dir")

    env_workspace = os.environ.get("ZEPHYR_WORKSPACE_DIR")
    if env_workspace:
        add(Path(env_workspace), "ZEPHYR_WORKSPACE_DIR")

    env_zephyr_base = os.environ.get("ZEPHYR_BASE")
    if env_zephyr_base:
        zephyr_base = Path(env_zephyr_base).expanduser().resolve()
        if zephyr_base.name == "zephyr":
            add(zephyr_base.parent, "ZEPHYR_BASE")

    current = Path.cwd().resolve()
    for ancestor in (current, *current.parents):
        if (ancestor / ".west").exists() and (ancestor / "zephyr").is_dir():
            add(ancestor, "cwd-ancestor")
            break

    add(Path.home() / "zephyrproject", "~/zephyrproject")

    if sys.platform == "win32":
        for root in (Path("C:/ncs"), Path.home() / "ncs"):
            if not root.exists():
                continue
            for child in sorted(root.glob("v*"), reverse=True):
                add(child, "detected-ncs")
    else:
        for root in (Path.home() / "ncs", Path.home() / "work" / "ncs"):
            if not root.exists():
                continue
            for child in sorted(root.glob("v*"), reverse=True):
                add(child, "detected-ncs")

    add(managed_workspace_dir, "managed-cache")
    return candidates


def _workspace_supports_board(workspace_dir: Path, board: str | None) -> bool:
    if board is None:
        return True
    if not _workspace_has_exact_board_target(workspace_dir, board):
        return False
    for _project_name, relative_path in _required_workspace_projects(board):
        if not (workspace_dir / relative_path).exists():
            return False
    return True


def _workspace_has_exact_board_target(workspace_dir: Path, board: str) -> bool:
    if not board or any(part in {"", ".", ".."} for part in board.split("/")):
        return False
    if any(not all(char.isalnum() or char in "_-" for char in part) for part in board.split("/")):
        return False
    base, *qualifiers = board.split("/")
    boards_root = workspace_dir / "zephyr" / "boards"
    if not boards_root.is_dir():
        return False
    board_dirs = [path for path in boards_root.glob(f"*/{base}") if path.is_dir()]
    for board_dir in board_dirs:
        suffix = "_".join((base, *qualifiers))
        if (board_dir / f"{suffix}_defconfig").is_file():
            return True
    return False


def _find_existing_workspace(
    *,
    workspace_dir: Path | None,
    managed_workspace_dir: Path,
    board: str | None,
    managed_zephyr_repo: str | None = None,
    managed_zephyr_ref: str | None = None,
) -> CandidatePath | None:
    for candidate in _iter_zephyr_workspace_candidates(
        explicit_workspace_dir=workspace_dir,
        managed_workspace_dir=managed_workspace_dir,
    ):
        if candidate.path == managed_workspace_dir.resolve():
            if (
                managed_zephyr_repo is None
                or managed_zephyr_ref is None
                or not _managed_workspace_is_owned(
                    managed_workspace_dir,
                    zephyr_repo=managed_zephyr_repo,
                    zephyr_ref=managed_zephyr_ref,
                )
                or _west_manifest_dir(managed_workspace_dir)
                != (managed_workspace_dir / "manifest").resolve()
                or not _managed_workspace_is_complete(
                    managed_workspace_dir,
                    zephyr_repo=managed_zephyr_repo,
                    zephyr_ref=managed_zephyr_ref,
                    board=board,
                )
            ):
                continue
        if _is_zephyr_workspace(candidate.path) and _workspace_supports_board(
            candidate.path, board
        ):
            return candidate
    return None


def _iter_sdk_candidates(
    *,
    explicit_sdk_dir: Path | None,
    managed_sdk_dir: Path,
    workspace_dir: Path,
) -> list[CandidatePath]:
    candidates: list[CandidatePath] = []
    seen: set[Path] = set()

    def add(path: Path | None, source: str) -> None:
        if path is None:
            return
        resolved = path.expanduser().resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(CandidatePath(path=resolved, source=source))

    add(explicit_sdk_dir, "--sdk-dir")

    env_sdk = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
    if env_sdk:
        add(Path(env_sdk), "ZEPHYR_SDK_INSTALL_DIR")

    toolchains_root = workspace_dir.parent / "toolchains"
    if toolchains_root.exists():
        for candidate in sorted(toolchains_root.glob("*/opt/zephyr-sdk"), reverse=True):
            add(candidate, "workspace-adjacent-toolchain")

    if sys.platform == "win32":
        ncs_roots = [Path("C:/ncs"), Path.home() / "ncs"]
    else:
        ncs_roots = [Path.home() / "ncs", Path.home() / "work" / "ncs"]
    for root in ncs_roots:
        if not root.exists():
            continue
        for pattern in ("toolchains/*/opt/zephyr-sdk", "v*/toolchains/*/opt/zephyr-sdk"):
            for candidate in sorted(root.glob(pattern), reverse=True):
                add(candidate, "detected-ncs-toolchain")

    # nRF Connect for Desktop has also used these per-user locations. Keep the
    # search bounded to known layouts; never recursively scan the user's disk.
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        nrf_connect_roots = [Path.home() / ".nrfconnect"]
        if local_app_data:
            nrf_connect_roots.append(Path(local_app_data) / "nrfconnect")
        for root in nrf_connect_roots:
            if not root.exists():
                continue
            for pattern in (
                "toolchains/*/opt/zephyr-sdk",
                "toolchains/v*/opt/zephyr-sdk",
                "sdk/*/opt/zephyr-sdk",
            ):
                for candidate in sorted(root.glob(pattern), reverse=True):
                    add(candidate, "detected-nrf-connect-toolchain")

    standard_candidates = [
        Path.home() / "zephyr-sdk-1.0.1",
        Path.home() / "zephyr-sdk-1.0.0",
        Path.home() / "zephyr-sdk-0.17.4",
        Path.home() / ".local" / "opt" / "zephyr-sdk-1.0.1",
        Path.home() / ".local" / "opt" / "zephyr-sdk-1.0.0",
        Path.home() / ".local" / "opt" / "zephyr-sdk-0.17.4",
        Path("/usr/local/zephyr-sdk-1.0.1"),
        Path("/usr/local/zephyr-sdk-1.0.0"),
        Path("/usr/local/zephyr-sdk-0.17.4"),
    ]
    if sys.platform == "win32":
        standard_candidates.extend(
            [
                Path("C:/zephyr-sdk-1.0.1"),
                Path("C:/zephyr-sdk-1.0.0"),
                Path("C:/zephyr-sdk-0.17.4"),
            ]
        )

    for candidate in standard_candidates:
        add(candidate, "standard-location")

    # A server-managed SDK is a cache/fallback, not a reason to ignore a user's
    # already-installed NCS or Zephyr SDK.
    add(managed_sdk_dir, "managed-cache")

    return candidates


def _find_existing_sdk(
    *,
    workspace_dir: Path,
    sdk_dir: Path | None,
    managed_sdk_dir: Path,
    toolchain: str,
) -> CandidatePath | None:
    required_version = _sdk_version(workspace_dir)
    versioned_managed_sdk_dir = managed_sdk_dir / required_version
    for candidate in _iter_sdk_candidates(
        explicit_sdk_dir=sdk_dir,
        managed_sdk_dir=versioned_managed_sdk_dir,
        workspace_dir=workspace_dir,
    ):
        if candidate.path == versioned_managed_sdk_dir.resolve() and not _managed_sdk_is_owned(
            candidate.path, version=required_version, toolchain=toolchain
        ):
            continue
        installed_version = _installed_sdk_version(candidate.path)
        if (
            installed_version is None
            or not _sdk_has_toolchain(candidate.path, toolchain)
            or not _sdk_toolchain_runs(candidate.path, toolchain)
        ):
            continue
        if installed_version == required_version or _sdk_versions_are_patch_compatible(
            installed_version, required_version
        ):
            return candidate
    return None


def _print_step(message: str) -> None:
    print(f"[zephyr-build] {message}")


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int = DEFAULT_STEP_TIMEOUT_SECONDS,
) -> None:
    rendered = " ".join(str(part) for part in cmd)
    location = f" (cwd={cwd})" if cwd is not None else ""
    _print_step(f"run: {rendered}{location}")
    try:
        result = run_owned(
            cmd,
            cwd=cwd,
            env=env,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout_seconds}s: {' '.join(cmd)}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(cmd)}")


def _ensure_west_python_unlocked(west_venv_dir: Path) -> Path:
    west_python = _venv_python_path(west_venv_dir)
    west_bin_dir = west_python.parent
    executable_suffix = ".exe" if sys.platform == "win32" else ""
    required_tools = [
        west_bin_dir / f"cmake{executable_suffix}",
        west_bin_dir / f"ninja{executable_suffix}",
    ]
    if (
        west_python.exists()
        and all(tool.exists() for tool in required_tools)
        and _python_has_module(west_python, "patoolib")
        and _python_has_module(west_python, "py7zr")
        and _python_has_module(west_python, "elftools")
        and _python_has_module(west_python, "yaml")
        and _can_run_west(west_python)
    ):
        return west_python

    west_venv_dir.parent.mkdir(parents=True, exist_ok=True)
    if not west_python.exists():
        _print_step(f"bootstrap west venv: {west_venv_dir}")
        _run([sys.executable, "-m", "venv", str(west_venv_dir)])
    _run([str(west_python), "-m", "pip", "install", "--upgrade", "pip"])
    _run(
        [
            str(west_python),
            "-m",
            "pip",
            "install",
            "west",
            "cmake",
            "ninja",
            "patool",
            "py7zr",
            "pyelftools",
        ]
    )
    return west_python


def _ensure_west_python(west_venv_dir: Path) -> Path:
    with _cache_lock(west_venv_dir):
        return _ensure_west_python_unlocked(west_venv_dir)


def _west_cmd(west_python: Path, *args: str) -> list[str]:
    return [str(west_python), "-m", "west", *args]


def _west_env(west_python: Path, sdk_dir: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    path_entries = [str(west_python.parent)]
    if sdk_dir is not None:
        sdk_bin_dir = sdk_dir.parent / "bin"
        if sdk_bin_dir.is_dir():
            path_entries.append(str(sdk_bin_dir))
    existing_path = env.get("PATH", "")
    if existing_path:
        path_entries.append(existing_path)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


def _can_run_west(python_path: Path) -> bool:
    try:
        result = run_owned(
            [str(python_path), "-m", "west", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _is_complete_build_python(python_path: Path) -> bool:
    executable_suffix = ".exe" if sys.platform == "win32" else ""
    tools_dir = python_path.parent
    return (
        python_path.is_file()
        and (tools_dir / f"cmake{executable_suffix}").is_file()
        and (tools_dir / f"ninja{executable_suffix}").is_file()
        and _can_run_west(python_path)
        and _python_has_module(python_path, "yaml")
        and _python_has_module(python_path, "elftools")
    )


def _python_has_module(python_path: Path, module_name: str) -> bool:
    try:
        result = run_owned(
            [str(python_path), "-c", f"import {module_name}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _resolve_toolchain_python(sdk_dir: Path, fallback_python: Path) -> Path:
    candidate_names = ["python.exe", "python3", "python"]
    for name in candidate_names:
        candidate = sdk_dir.parent / "bin" / name
        if _is_complete_build_python(candidate):
            return candidate
    return fallback_python


def _managed_manifest_text(*, zephyr_repo: str, zephyr_ref: str) -> str:
    return (
        "manifest:\n"
        '  version: "0.13"\n'
        "  projects:\n"
        "    - name: zephyr\n"
        f"      url: {zephyr_repo}\n"
        "      path: zephyr\n"
        f"      revision: {zephyr_ref}\n"
        "      import:\n"
        "        name-allowlist:\n"
        "          - cmsis\n"
        "          - cmsis_6\n"
        "          - hal_nordic\n"
        "          - hal_st\n"
        "          - hal_stm32\n"
        "          - picolibc\n"
        "          - segger\n"
        "  self:\n"
        "    path: manifest\n"
    )


def _install_zephyr_python_requirements(west_python: Path, workspace_dir: Path) -> None:
    requirements_path = workspace_dir / "zephyr" / "scripts" / "requirements-base.txt"
    if not requirements_path.is_file():
        raise RuntimeError(f"Missing Zephyr Python requirements file: {requirements_path}")
    _run([str(west_python), "-m", "pip", "install", "-r", str(requirements_path)])


def _workspace_requirements_fingerprint(workspace_dir: Path) -> str:
    scripts_dir = workspace_dir / "zephyr" / "scripts"
    requirements_path = scripts_dir / "requirements-base.txt"
    if not requirements_path.is_file():
        raise RuntimeError(f"Missing Zephyr Python requirements file: {requirements_path}")
    digest = hashlib.sha256()
    requirement_files = sorted(scripts_dir.rglob("requirements*.txt"))
    for path in requirement_files:
        relative = path.relative_to(scripts_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _workspace_requirements_satisfied(west_python: Path, workspace_dir: Path) -> bool:
    requirements_path = workspace_dir / "zephyr" / "scripts" / "requirements-base.txt"
    if not requirements_path.is_file():
        return False
    commands = (
        [
            str(west_python),
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--no-index",
            "-r",
            str(requirements_path),
        ],
        [str(west_python), "-m", "pip", "check"],
    )
    for command in commands:
        try:
            result = run_owned(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
    return True


def _workspace_requirements_venv_dir(base_venv_dir: Path, workspace_dir: Path) -> Path:
    fingerprint = _workspace_requirements_fingerprint(workspace_dir)
    return base_venv_dir.parent / f"{base_venv_dir.name}-req-{fingerprint[:20]}"


def _ensure_workspace_python_requirements(
    *, west_python: Path, west_venv_dir: Path, workspace_dir: Path
) -> None:
    try:
        west_python.resolve().relative_to(west_venv_dir.resolve())
    except ValueError:
        # Vendor-owned Python environments are validated but never mutated.
        return
    fingerprint = _workspace_requirements_fingerprint(workspace_dir)
    marker_path = west_venv_dir / REQUIREMENTS_MARKER
    expected = {
        "schema_version": 1,
        "requirements_sha256": fingerprint,
    }
    with _cache_lock(west_venv_dir):
        try:
            current = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            current = None
        if current == expected and _workspace_requirements_satisfied(west_python, workspace_dir):
            return
        _install_zephyr_python_requirements(west_python, workspace_dir)
        if not _workspace_requirements_satisfied(west_python, workspace_dir):
            raise RuntimeError(
                f"Zephyr Python requirements remain unsatisfied after install: {workspace_dir}"
            )
        temporary = marker_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, marker_path)


def _sdk_version(workspace_dir: Path) -> str:
    sdk_version_path = workspace_dir / "zephyr" / "SDK_VERSION"
    if not sdk_version_path.is_file():
        raise RuntimeError(f"Missing Zephyr SDK_VERSION file: {sdk_version_path}")
    version = sdk_version_path.read_text(encoding="utf-8").splitlines()[0].strip()
    if not version:
        raise RuntimeError(f"Zephyr SDK_VERSION file was empty: {sdk_version_path}")
    return version


def _sdk_host_archive_parts() -> tuple[str, str, str]:
    system = platform.system()
    machine = platform.machine()
    if system == "Linux":
        os_name = "linux"
    elif system == "Darwin":
        os_name = "macos"
    elif system == "Windows":
        os_name = "windows"
    else:
        raise RuntimeError(f"Unsupported system for managed Zephyr SDK install: {system}")

    if machine in {"aarch64", "arm64"}:
        arch = "aarch64"
    elif machine in {"x86_64", "AMD64"}:
        arch = "x86_64"
    else:
        raise RuntimeError(f"Unsupported machine for managed Zephyr SDK install: {machine}")

    ext = ".7z" if os_name == "windows" else ".tar.xz"
    return os_name, arch, ext


def _sdk_minimal_archive_filename(version: str) -> str:
    os_name, arch, ext = _sdk_host_archive_parts()
    return f"zephyr-sdk-{version}_{os_name}-{arch}_minimal{ext}"


def _sdk_toolchain_archive_filename(toolchain: str) -> str:
    os_name, arch, ext = _sdk_host_archive_parts()
    return f"toolchain_{os_name}-{arch}_{toolchain}{ext}"


def _download_bytes(url: str) -> bytes:
    _print_step(f"download: {url}")
    try:
        with urlopen(url, timeout=120) as response:
            return bytes(response.read())
    except URLError as exc:
        raise RuntimeError(f"Unable to download {url}: {exc}") from exc


def _download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_download_bytes(url))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_sdk_sha256(version: str, filename: str) -> str:
    sha_url = (
        f"https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v{version}/sha256.sum"
    )
    sha_text = _download_bytes(sha_url).decode("utf-8")
    for line in sha_text.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        candidate_name = parts[-1].lstrip("*")
        if candidate_name == filename:
            return parts[0]
    raise RuntimeError(f"Could not find {filename} in {sha_url}")


def _extract_7z_archive(west_python: Path, archive_path: Path, destination: Path) -> None:
    script = (
        "from pathlib import Path\n"
        "import py7zr\n"
        f"archive = Path({str(archive_path)!r})\n"
        f"destination = Path({str(destination)!r})\n"
        "with py7zr.SevenZipFile(archive, 'r') as archive_handle:\n"
        "    archive_handle.extractall(path=destination)\n"
    )
    _run([str(west_python), "-c", script])


def _extract_sdk_archive(west_python: Path, archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if archive_path.name.endswith(".tar.xz"):
        with tarfile.open(archive_path, mode="r:xz") as archive:
            archive.extractall(destination)
        return
    if archive_path.suffix == ".7z":
        _extract_7z_archive(west_python, archive_path, destination)
        return
    raise RuntimeError(f"Unsupported Zephyr SDK archive format: {archive_path.name}")


def _install_managed_toolchain(
    *, west_python: Path, sdk_dir: Path, version: str, toolchain: str
) -> None:
    """Install one verified SDK toolchain without relying on wget or host 7-Zip."""

    if _sdk_has_toolchain(sdk_dir, toolchain):
        return
    listed_toolchains = sdk_dir / "sdk_toolchains"
    if listed_toolchains.is_file():
        available = {
            line.strip() for line in listed_toolchains.read_text(encoding="utf-8").splitlines()
        }
        if toolchain not in available:
            raise RuntimeError(
                f"Zephyr SDK {version} does not publish requested toolchain {toolchain}"
            )

    filename = _sdk_toolchain_archive_filename(toolchain)
    archive_url = (
        f"https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v{version}/{filename}"
    )
    expected_sha = _expected_sdk_sha256(version, filename)
    with tempfile.TemporaryDirectory(prefix="firmcli-zephyr-toolchain-") as temp_dir_text:
        archive_path = Path(temp_dir_text) / filename
        _download_file(archive_url, archive_path)
        actual_sha = _sha256_file(archive_path)
        if actual_sha.lower() != expected_sha.lower():
            raise RuntimeError(
                f"Managed Zephyr SDK toolchain archive sha256 mismatch for {filename}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        _extract_sdk_archive(west_python, archive_path, sdk_dir)

    if not _sdk_has_toolchain(sdk_dir, toolchain):
        raise RuntimeError(
            f"Managed Zephyr SDK toolchain archive did not provide {toolchain}: {sdk_dir}"
        )


def _managed_workspace_marker_payload(*, zephyr_repo: str, zephyr_ref: str) -> dict[str, object]:
    manifest_text = _managed_manifest_text(zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref)
    return {
        "schema_version": 1,
        "owner": MANAGED_WORKSPACE_OWNER,
        "zephyr_repo": zephyr_repo,
        "zephyr_ref": zephyr_ref,
        "manifest_sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
    }


def _path_is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction is not None and is_junction())


def _managed_workspace_is_owned(workspace_dir: Path, *, zephyr_repo: str, zephyr_ref: str) -> bool:
    if _path_is_link_or_junction(workspace_dir):
        return False
    workspace_root = workspace_dir.resolve()
    marker_path = workspace_dir / MANAGED_WORKSPACE_MARKER
    manifest_dir = workspace_dir / "manifest"
    manifest_path = workspace_dir / "manifest" / "west.yml"
    if not marker_path.is_file() or not manifest_path.is_file():
        return False
    for owned_path in (marker_path, manifest_dir, manifest_path):
        try:
            if _path_is_link_or_junction(owned_path) or not owned_path.resolve().is_relative_to(
                workspace_root
            ):
                return False
        except OSError:
            return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    expected = _managed_workspace_marker_payload(zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref)
    if marker != expected:
        return False
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return manifest_sha == expected["manifest_sha256"]


def _write_managed_workspace_identity(
    workspace_dir: Path, *, zephyr_repo: str, zephyr_ref: str
) -> None:
    manifest_dir = workspace_dir / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "west.yml").write_bytes(
        _managed_manifest_text(zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref).encode("utf-8")
    )
    marker = _managed_workspace_marker_payload(zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref)
    marker_path = workspace_dir / MANAGED_WORKSPACE_MARKER
    temporary = marker_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker_path)


def _managed_workspace_identity_can_resume(
    workspace_dir: Path, *, zephyr_repo: str, zephyr_ref: str
) -> bool:
    """Recognize only the exact partial identity this helper may safely finish."""

    if _path_is_link_or_junction(workspace_dir) or not workspace_dir.is_dir():
        return False
    allowed = {
        "manifest",
        MANAGED_WORKSPACE_MARKER,
        Path(MANAGED_WORKSPACE_MARKER).with_suffix(".tmp").name,
    }
    if any(child.name not in allowed for child in workspace_dir.iterdir()):
        return False
    manifest_dir = workspace_dir / "manifest"
    manifest_path = manifest_dir / "west.yml"
    if (
        not manifest_path.is_file()
        or _path_is_link_or_junction(manifest_dir)
        or _path_is_link_or_junction(manifest_path)
    ):
        return False
    expected = _managed_manifest_text(zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref).encode(
        "utf-8"
    )
    try:
        return (
            manifest_dir.resolve().is_relative_to(workspace_dir.resolve())
            and manifest_path.resolve().is_relative_to(workspace_dir.resolve())
            and manifest_path.read_bytes() == expected
        )
    except OSError:
        return False


def _managed_workspace_completion(
    workspace_dir: Path, *, zephyr_repo: str, zephyr_ref: str
) -> dict[str, object] | None:
    completion_path = workspace_dir / MANAGED_WORKSPACE_COMPLETE
    if not completion_path.is_file() or _path_is_link_or_junction(completion_path):
        return None
    try:
        if not completion_path.resolve().is_relative_to(workspace_dir.resolve()):
            return None
        payload = json.loads(completion_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    expected_identity = _managed_workspace_marker_payload(
        zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref
    )
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != 1 or payload.get("owner") != MANAGED_WORKSPACE_OWNER:
        return None
    if payload.get("identity") != expected_identity:
        return None
    targets = payload.get("validated_board_targets")
    if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
        return None
    return payload


def _managed_workspace_is_complete(
    workspace_dir: Path, *, zephyr_repo: str, zephyr_ref: str, board: str | None
) -> bool:
    payload = _managed_workspace_completion(
        workspace_dir, zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref
    )
    if (
        payload is None
        or payload.get("global_update_complete") is not True
        or not _managed_workspace_is_owned(
            workspace_dir, zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref
        )
    ):
        return False
    if _west_manifest_dir(workspace_dir) != (workspace_dir / "manifest").resolve():
        return False
    targets = payload["validated_board_targets"]
    return board is None or (
        isinstance(targets, list)
        and board in targets
        and _workspace_supports_board(workspace_dir, board)
    )


def _write_managed_workspace_completion(
    workspace_dir: Path, *, zephyr_repo: str, zephyr_ref: str, board: str | None
) -> None:
    previous = _managed_workspace_completion(
        workspace_dir, zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref
    )
    previous_targets = previous.get("validated_board_targets", []) if previous is not None else []
    targets = (
        {item for item in previous_targets if isinstance(item, str)}
        if isinstance(previous_targets, list)
        else set()
    )
    if board is not None:
        targets.add(board)
    payload = {
        "schema_version": 1,
        "owner": MANAGED_WORKSPACE_OWNER,
        "global_update_complete": True,
        "identity": _managed_workspace_marker_payload(
            zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref
        ),
        "validated_board_targets": sorted(targets),
    }
    completion_path = workspace_dir / MANAGED_WORKSPACE_COMPLETE
    temporary = completion_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, completion_path)


def _west_manifest_dir(workspace_dir: Path) -> Path | None:
    config_path = workspace_dir / ".west" / "config"
    if not config_path.is_file():
        return None
    config = configparser.ConfigParser()
    try:
        config.read(config_path, encoding="utf-8")
        configured = config.get("manifest", "path")
    except (configparser.Error, KeyError, ValueError):
        return None
    configured_path = Path(configured)
    if not configured_path.is_absolute():
        configured_path = workspace_dir / configured_path
    return configured_path.resolve()


def _managed_sdk_marker_payload(*, version: str, toolchain: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "owner": MANAGED_SDK_OWNER,
        "version": version,
        "toolchain": toolchain,
    }


def _managed_sdk_is_owned(sdk_dir: Path, *, version: str, toolchain: str) -> bool:
    if _path_is_link_or_junction(sdk_dir):
        return False
    marker_path = sdk_dir / MANAGED_SDK_MARKER
    if not marker_path.is_file():
        return False
    try:
        if _path_is_link_or_junction(marker_path) or not marker_path.resolve().is_relative_to(
            sdk_dir.resolve()
        ):
            return False
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return marker == _managed_sdk_marker_payload(version=version, toolchain=toolchain)


def _write_managed_sdk_identity(sdk_dir: Path, *, version: str, toolchain: str) -> None:
    marker = _managed_sdk_marker_payload(version=version, toolchain=toolchain)
    (sdk_dir / MANAGED_SDK_MARKER).write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _install_managed_sdk(
    *,
    west_python: Path,
    workspace_dir: Path,
    managed_sdk_dir: Path,
    toolchain: str,
) -> None:
    version = _sdk_version(workspace_dir)
    if managed_sdk_dir.exists() and not _managed_sdk_is_owned(
        managed_sdk_dir, version=version, toolchain=toolchain
    ):
        raise RuntimeError(
            "Refusing to replace an unowned managed SDK directory; choose a different "
            f"--managed-sdk-dir: {managed_sdk_dir}"
        )
    filename = _sdk_minimal_archive_filename(version)
    archive_url = (
        f"https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v{version}/{filename}"
    )
    expected_sha = _expected_sdk_sha256(version, filename)

    managed_sdk_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{managed_sdk_dir.name}.staging-", dir=managed_sdk_dir.parent
    ) as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        archive_path = temp_dir / filename
        extract_root = temp_dir / "extract"
        _download_file(archive_url, archive_path)
        actual_sha = _sha256_file(archive_path)
        if actual_sha.lower() != expected_sha.lower():
            raise RuntimeError(
                f"Managed Zephyr SDK archive sha256 mismatch for {filename}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        _extract_sdk_archive(west_python, archive_path, extract_root)
        extracted_dirs = [path for path in extract_root.iterdir() if path.is_dir()]
        matching_dir = next(
            (path for path in extracted_dirs if path.name.startswith(f"zephyr-sdk-{version}")),
            None,
        )
        sdk_root = matching_dir or (extracted_dirs[0] if len(extracted_dirs) == 1 else None)
        if sdk_root is None:
            raise RuntimeError(
                f"Managed Zephyr SDK archive had unexpected layout for {filename}: {extracted_dirs}"
            )
        # Complete and validate the entire candidate before it becomes visible
        # at the shared managed-cache path.
        _install_managed_toolchain(
            west_python=west_python,
            sdk_dir=sdk_root,
            version=version,
            toolchain=toolchain,
        )
        if (
            _installed_sdk_version(sdk_root) != version
            or not _sdk_has_toolchain(sdk_root, toolchain)
            or not _sdk_toolchain_runs(sdk_root, toolchain)
        ):
            raise RuntimeError(f"Staged managed SDK failed executable validation: {sdk_root}")
        _write_managed_sdk_identity(sdk_root, version=version, toolchain=toolchain)

        previous = temp_dir / "previous-owned-sdk"
        if managed_sdk_dir.exists():
            shutil.move(str(managed_sdk_dir), str(previous))
        try:
            shutil.move(str(sdk_root), str(managed_sdk_dir))
        except BaseException:
            if previous.exists() and not managed_sdk_dir.exists():
                shutil.move(str(previous), str(managed_sdk_dir))
            raise


def _resolve_workspace_dir_unlocked(
    *,
    west_python: Path,
    workspace_dir: Path | None,
    managed_workspace_dir: Path,
    zephyr_repo: str,
    zephyr_ref: str,
    board: str | None,
    skip_workspace_bootstrap: bool,
) -> tuple[Path, str]:
    if workspace_dir is not None and (
        not _is_zephyr_workspace(workspace_dir)
        or not _workspace_supports_board(workspace_dir, board)
    ):
        raise RuntimeError(
            f"Explicit Zephyr workspace is incomplete or does not support board {board}: "
            f"{workspace_dir}"
        )

    existing = _find_existing_workspace(
        workspace_dir=workspace_dir,
        managed_workspace_dir=managed_workspace_dir,
        board=board,
        managed_zephyr_repo=zephyr_repo,
        managed_zephyr_ref=zephyr_ref,
    )
    if existing is not None:
        _print_step(f"using workspace: {existing.path} ({existing.source})")
        return existing.path, existing.source

    # A successful West update populates every project in the pinned manifest.
    # If a later board target is already present and supported, record that
    # validation without needlessly rerunning a network-capable global update.
    completion = _managed_workspace_completion(
        managed_workspace_dir,
        zephyr_repo=zephyr_repo,
        zephyr_ref=zephyr_ref,
    )
    if (
        completion is not None
        and completion.get("global_update_complete") is True
        and _managed_workspace_is_owned(
            managed_workspace_dir,
            zephyr_repo=zephyr_repo,
            zephyr_ref=zephyr_ref,
        )
        and _west_manifest_dir(managed_workspace_dir)
        == (managed_workspace_dir / "manifest").resolve()
        and _is_zephyr_workspace(managed_workspace_dir)
        and _workspace_supports_board(managed_workspace_dir, board)
    ):
        _write_managed_workspace_completion(
            managed_workspace_dir,
            zephyr_repo=zephyr_repo,
            zephyr_ref=zephyr_ref,
            board=board,
        )
        _print_step(
            f"using managed workspace after local board validation: {managed_workspace_dir}"
        )
        return managed_workspace_dir, "managed-cache"

    if skip_workspace_bootstrap:
        raise RuntimeError(
            "No usable Zephyr workspace found. Set ZEPHYR_WORKSPACE_DIR/--workspace-dir or allow managed bootstrap."
        )

    managed_workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    manifest_dir = managed_workspace_dir / "manifest"

    if _is_zephyr_workspace(managed_workspace_dir):
        if (
            not _managed_workspace_is_owned(
                managed_workspace_dir,
                zephyr_repo=zephyr_repo,
                zephyr_ref=zephyr_ref,
            )
            or _west_manifest_dir(managed_workspace_dir) != manifest_dir.resolve()
        ):
            raise RuntimeError(
                "Refusing unowned or misdirected managed workspace cache; choose a different "
                f"--managed-workspace-dir: {managed_workspace_dir}"
            )

    if not _is_zephyr_workspace(managed_workspace_dir) or not _managed_workspace_is_complete(
        managed_workspace_dir,
        zephyr_repo=zephyr_repo,
        zephyr_ref=zephyr_ref,
        board=board,
    ):
        managed_workspace_dir.mkdir(parents=True, exist_ok=True)
        west_config_path = managed_workspace_dir / ".west" / "config"
        if west_config_path.is_file():
            if (
                not _managed_workspace_is_owned(
                    managed_workspace_dir,
                    zephyr_repo=zephyr_repo,
                    zephyr_ref=zephyr_ref,
                )
                or _west_manifest_dir(managed_workspace_dir) != manifest_dir.resolve()
            ):
                raise RuntimeError(
                    "Managed workspace is not owned by this helper or its manifest identity "
                    f"does not match {zephyr_repo}@{zephyr_ref}: {managed_workspace_dir}. "
                    "Choose a different --managed-workspace-dir."
                )
            _print_step(f"resume managed workspace at {managed_workspace_dir}")
        else:
            if (managed_workspace_dir / ".west").exists():
                raise RuntimeError(
                    f"Managed workspace has an incomplete foreign .west directory: {managed_workspace_dir}"
                )
            existing_entries = list(managed_workspace_dir.iterdir())
            if existing_entries and not _managed_workspace_is_owned(
                managed_workspace_dir,
                zephyr_repo=zephyr_repo,
                zephyr_ref=zephyr_ref,
            ):
                if not _managed_workspace_identity_can_resume(
                    managed_workspace_dir,
                    zephyr_repo=zephyr_repo,
                    zephyr_ref=zephyr_ref,
                ):
                    raise RuntimeError(
                        "Managed workspace directory contains unowned data; choose a different "
                        f"--managed-workspace-dir: {managed_workspace_dir}"
                    )
                _write_managed_workspace_identity(
                    managed_workspace_dir,
                    zephyr_repo=zephyr_repo,
                    zephyr_ref=zephyr_ref,
                )
            elif not existing_entries:
                _write_managed_workspace_identity(
                    managed_workspace_dir,
                    zephyr_repo=zephyr_repo,
                    zephyr_ref=zephyr_ref,
                )
            _print_step(
                f"bootstrap managed workspace at {managed_workspace_dir} from "
                f"{zephyr_repo}@{zephyr_ref}"
            )
            _run(
                _west_cmd(west_python, "init", "-l", str(manifest_dir)),
                cwd=managed_workspace_dir,
                env=_west_env(west_python),
            )
        _run(
            _west_cmd(
                west_python,
                "update",
                "--narrow",
                "-o=--depth=1",
            ),
            cwd=managed_workspace_dir,
            env=_west_env(west_python),
        )
        _run(
            _west_cmd(west_python, "zephyr-export"),
            cwd=managed_workspace_dir,
            env=_west_env(west_python),
        )
    elif not _workspace_supports_board(managed_workspace_dir, board):
        _print_step(
            f"managed workspace exists but is missing modules for board {board}; continuing update"
        )
        _run(
            _west_cmd(
                west_python,
                "update",
                "--narrow",
                "-o=--depth=1",
            ),
            cwd=managed_workspace_dir,
            env=_west_env(west_python),
        )
        _run(
            _west_cmd(west_python, "zephyr-export"),
            cwd=managed_workspace_dir,
            env=_west_env(west_python),
        )
    if not _is_zephyr_workspace(managed_workspace_dir) or not _workspace_supports_board(
        managed_workspace_dir, board
    ):
        raise RuntimeError(
            f"Managed Zephyr workspace does not provide exact board target {board}: "
            f"{managed_workspace_dir}"
        )
    _write_managed_workspace_completion(
        managed_workspace_dir,
        zephyr_repo=zephyr_repo,
        zephyr_ref=zephyr_ref,
        board=board,
    )
    return managed_workspace_dir, "managed-bootstrap"


def _resolve_workspace_dir(
    *,
    west_python: Path,
    workspace_dir: Path | None,
    managed_workspace_dir: Path,
    zephyr_repo: str,
    zephyr_ref: str,
    board: str | None,
    skip_workspace_bootstrap: bool,
) -> tuple[Path, str]:
    # Explicit and already-installed local workspaces are read-only and need no
    # shared-cache lock. The unlocked resolver repeats these checks after the
    # lock so another process's successful bootstrap is reused.
    if workspace_dir is not None and (
        not _is_zephyr_workspace(workspace_dir)
        or not _workspace_supports_board(workspace_dir, board)
    ):
        raise RuntimeError(
            f"Explicit Zephyr workspace is incomplete or does not support board {board}: "
            f"{workspace_dir}"
        )
    existing = _find_existing_workspace(
        workspace_dir=workspace_dir,
        managed_workspace_dir=managed_workspace_dir,
        board=board,
        managed_zephyr_repo=zephyr_repo,
        managed_zephyr_ref=zephyr_ref,
    )
    if existing is not None and existing.path != managed_workspace_dir.resolve():
        _print_step(f"using workspace: {existing.path} ({existing.source})")
        return existing.path, existing.source
    if skip_workspace_bootstrap:
        raise RuntimeError(
            "No usable Zephyr workspace found. Set ZEPHYR_WORKSPACE_DIR/--workspace-dir "
            "or allow managed bootstrap."
        )
    with _cache_lock(managed_workspace_dir):
        return _resolve_workspace_dir_unlocked(
            west_python=west_python,
            workspace_dir=workspace_dir,
            managed_workspace_dir=managed_workspace_dir,
            zephyr_repo=zephyr_repo,
            zephyr_ref=zephyr_ref,
            board=board,
            skip_workspace_bootstrap=skip_workspace_bootstrap,
        )


def _resolve_sdk_dir_unlocked(
    *,
    west_python: Path,
    workspace_dir: Path,
    sdk_dir: Path | None,
    managed_sdk_dir: Path,
    toolchain: str,
    skip_sdk_install: bool,
) -> tuple[Path, str]:
    required_version = _sdk_version(workspace_dir)
    versioned_managed_sdk_dir = managed_sdk_dir / required_version
    if sdk_dir is not None:
        installed_version = _installed_sdk_version(sdk_dir)
        if (
            installed_version is None
            or not _sdk_has_toolchain(sdk_dir, toolchain)
            or not _sdk_toolchain_runs(sdk_dir, toolchain)
            or not (
                installed_version == required_version
                or _sdk_versions_are_patch_compatible(installed_version, required_version)
            )
        ):
            raise RuntimeError(
                f"Explicit Zephyr SDK is incomplete, cannot execute {toolchain}, or is "
                f"incompatible with requested SDK {required_version}: {sdk_dir}"
            )
    for candidate in _iter_sdk_candidates(
        explicit_sdk_dir=sdk_dir,
        managed_sdk_dir=versioned_managed_sdk_dir,
        workspace_dir=workspace_dir,
    ):
        if candidate.path == versioned_managed_sdk_dir.resolve() and not _managed_sdk_is_owned(
            candidate.path, version=required_version, toolchain=toolchain
        ):
            continue
        installed_version = _installed_sdk_version(candidate.path)
        if (
            installed_version is None
            or not _sdk_has_toolchain(candidate.path, toolchain)
            or not _sdk_toolchain_runs(candidate.path, toolchain)
        ):
            continue
        if installed_version == required_version:
            _print_step(f"using sdk: {candidate.path} ({candidate.source})")
            return candidate.path, candidate.source
        if _sdk_versions_are_patch_compatible(installed_version, required_version):
            source = f"{candidate.source}; compatible-sdk-{installed_version}"
            _print_step(
                f"using local sdk: {candidate.path} ({source}; "
                f"workspace recommends {required_version})"
            )
            return candidate.path, source

    if skip_sdk_install:
        raise RuntimeError(
            "No usable Zephyr SDK found. Set ZEPHYR_SDK_INSTALL_DIR/--sdk-dir or allow managed SDK install."
        )

    versioned_managed_sdk_dir.parent.mkdir(parents=True, exist_ok=True)
    _print_step(f"install managed sdk at {versioned_managed_sdk_dir}")
    _install_managed_sdk(
        west_python=west_python,
        workspace_dir=workspace_dir,
        managed_sdk_dir=versioned_managed_sdk_dir,
        toolchain=toolchain,
    )
    if (
        not _is_zephyr_sdk(versioned_managed_sdk_dir, expected_version=required_version)
        or not _sdk_has_toolchain(versioned_managed_sdk_dir, toolchain)
        or not _sdk_toolchain_runs(versioned_managed_sdk_dir, toolchain)
        or not _managed_sdk_is_owned(
            versioned_managed_sdk_dir, version=required_version, toolchain=toolchain
        )
    ):
        raise RuntimeError(
            "Managed SDK install completed but its sdk_version does not match "
            f"{required_version}: {versioned_managed_sdk_dir}"
        )
    return versioned_managed_sdk_dir, "managed-install"


def _resolve_sdk_dir(
    *,
    west_python: Path,
    workspace_dir: Path,
    sdk_dir: Path | None,
    managed_sdk_dir: Path,
    toolchain: str,
    skip_sdk_install: bool,
) -> tuple[Path, str]:
    required_version = _sdk_version(workspace_dir)
    with _cache_lock(managed_sdk_dir / required_version):
        return _resolve_sdk_dir_unlocked(
            west_python=west_python,
            workspace_dir=workspace_dir,
            sdk_dir=sdk_dir,
            managed_sdk_dir=managed_sdk_dir,
            toolchain=toolchain,
            skip_sdk_install=skip_sdk_install,
        )


def ensure_runtime(args: argparse.Namespace) -> ZephyrRuntime:
    west_venv_dir = Path(args.west_venv_dir).expanduser().resolve()
    managed_workspace_dir = Path(args.managed_workspace_dir).expanduser().resolve()
    managed_sdk_dir = Path(args.managed_sdk_dir).expanduser().resolve()
    workspace_dir = Path(args.workspace_dir).expanduser().resolve() if args.workspace_dir else None
    sdk_dir = Path(args.sdk_dir).expanduser().resolve() if args.sdk_dir else None

    if workspace_dir is not None and (
        not _is_zephyr_workspace(workspace_dir)
        or not _workspace_supports_board(workspace_dir, args.board)
    ):
        raise RuntimeError(
            f"Explicit Zephyr workspace is incomplete or does not support board {args.board}: "
            f"{workspace_dir}"
        )
    if sdk_dir is not None and (
        _installed_sdk_version(sdk_dir) is None
        or not _sdk_has_toolchain(sdk_dir, args.toolchain)
        or not _sdk_toolchain_runs(sdk_dir, args.toolchain)
    ):
        raise RuntimeError(
            f"Explicit Zephyr SDK is incomplete or cannot execute {args.toolchain}: {sdk_dir}"
        )

    # A complete local NCS install is usable offline. Discover it before any
    # private-venv pip bootstrap or managed network fallback.
    west_python: Path | None = None
    local_workspace = _find_existing_workspace(
        workspace_dir=workspace_dir,
        managed_workspace_dir=managed_workspace_dir,
        board=args.board,
        managed_zephyr_repo=args.zephyr_repo,
        managed_zephyr_ref=args.zephyr_ref,
    )
    if local_workspace is not None and local_workspace.path == managed_workspace_dir.resolve():
        # Managed state is inspected only while its interprocess lock is held.
        local_workspace = None
    if local_workspace is not None and sdk_dir is not None:
        installed_version = _installed_sdk_version(sdk_dir)
        required_version = _sdk_version(local_workspace.path)
        if installed_version is None or not (
            installed_version == required_version
            or _sdk_versions_are_patch_compatible(installed_version, required_version)
        ):
            raise RuntimeError(
                f"Explicit Zephyr SDK {installed_version} is incompatible with workspace "
                f"SDK {required_version}: {sdk_dir}"
            )
    if local_workspace is not None:
        local_sdk = _find_existing_sdk(
            workspace_dir=local_workspace.path,
            sdk_dir=sdk_dir,
            managed_sdk_dir=managed_sdk_dir,
            toolchain=args.toolchain,
        )
        if local_sdk is not None:
            candidate_python = _resolve_toolchain_python(
                local_sdk.path, west_venv_dir / "missing-python"
            )
            if candidate_python != west_venv_dir / "missing-python":
                west_python = candidate_python
                _print_step(f"using local build Python: {west_python}")

    if west_python is None:
        west_python = _ensure_west_python(west_venv_dir)
    resolved_workspace_dir, workspace_source = _resolve_workspace_dir(
        west_python=west_python,
        workspace_dir=workspace_dir,
        managed_workspace_dir=managed_workspace_dir,
        zephyr_repo=args.zephyr_repo,
        zephyr_ref=args.zephyr_ref,
        board=args.board,
        skip_workspace_bootstrap=args.skip_workspace_bootstrap,
    )
    resolved_sdk_dir, sdk_source = _resolve_sdk_dir(
        west_python=west_python,
        workspace_dir=resolved_workspace_dir,
        sdk_dir=sdk_dir,
        managed_sdk_dir=managed_sdk_dir,
        toolchain=args.toolchain,
        skip_sdk_install=args.skip_sdk_install,
    )
    candidate_python = _resolve_toolchain_python(resolved_sdk_dir, west_python)
    try:
        candidate_is_base_helper = candidate_python.resolve().is_relative_to(
            west_venv_dir.resolve()
        )
    except OSError:
        candidate_is_base_helper = True
    if not candidate_is_base_helper and _workspace_requirements_satisfied(
        candidate_python, resolved_workspace_dir
    ):
        west_python = candidate_python
    else:
        requirements_venv_dir = _workspace_requirements_venv_dir(
            west_venv_dir, resolved_workspace_dir
        )
        west_python = _ensure_west_python(requirements_venv_dir)
        _ensure_workspace_python_requirements(
            west_python=west_python,
            west_venv_dir=requirements_venv_dir,
            workspace_dir=resolved_workspace_dir,
        )
    return ZephyrRuntime(
        workspace_dir=resolved_workspace_dir,
        workspace_source=workspace_source,
        sdk_dir=resolved_sdk_dir,
        sdk_source=sdk_source,
        west_python=west_python,
        managed_workspace_dir=managed_workspace_dir,
    )


def _clean_build_dir(build_dir: Path, *, app_dir: Path) -> None:
    if not _build_dir_is_owned(app_dir, build_dir):
        raise RuntimeError(f"Refusing to clean unowned build directory: {build_dir}")
    for child in build_dir.iterdir():
        if child.name in {".gitkeep", ".gitignore", BUILD_OUTPUT_MARKER}:
            continue
        if _path_is_link_or_junction(child):
            raise RuntimeError(f"Refusing to clean linked or junction build output: {child}")
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _cmake_cache_source_dir(build_dir: Path) -> Path | None:
    cache_path = build_dir / "CMakeCache.txt"
    if not cache_path.is_file():
        return None
    candidates: dict[str, Path] = {}
    for line in cache_path.read_text(encoding="utf-8", errors="replace").splitlines():
        for key in ("APP_DIR", "CMAKE_HOME_DIRECTORY"):
            if not line.startswith(f"{key}:"):
                continue
            _key, _sep, value = line.partition("=")
            value = value.strip()
            if value:
                candidates[key] = Path(value).expanduser().resolve()
    # Zephyr sysbuild owns CMAKE_HOME_DIRECTORY itself and records the actual
    # main application in APP_DIR. Plain Zephyr builds use CMAKE_HOME_DIRECTORY.
    return candidates.get("APP_DIR") or candidates.get("CMAKE_HOME_DIRECTORY")


def _build_cache_matches_app(build_dir: Path, app_dir: Path) -> bool:
    cache_source_dir = _cmake_cache_source_dir(build_dir)
    if cache_source_dir is None:
        return True
    return cache_source_dir == app_dir.resolve()


def _resolve_artifact_paths(work_build_dir: Path, *, app_dir_name: str) -> tuple[Path, Path | None]:
    preferred_dirs = [
        work_build_dir / "zephyr",
        work_build_dir / app_dir_name / "zephyr",
    ]
    seen: set[Path] = set()
    for candidate_dir in preferred_dirs:
        candidate_dir = candidate_dir.resolve()
        if candidate_dir in seen:
            continue
        seen.add(candidate_dir)
        elf_path = candidate_dir / "zephyr.elf"
        hex_path = candidate_dir / "zephyr.hex"
        if elf_path.is_file():
            return elf_path, hex_path if hex_path.is_file() else None

    candidates = sorted(
        work_build_dir.rglob("zephyr.elf"),
        key=lambda path: (len(path.parts), str(path)),
    )
    if not candidates:
        return work_build_dir / "zephyr" / "zephyr.elf", None

    elf_path = candidates[0]
    hex_candidate = elf_path.with_suffix(".hex")
    return elf_path, hex_candidate if hex_candidate.is_file() else None


def _copy_artifacts(
    work_build_dir: Path,
    canonical_build_dir: Path,
    *,
    app_dir: Path,
    app_dir_name: str,
) -> None:
    elf_path, hex_path = _resolve_artifact_paths(work_build_dir, app_dir_name=app_dir_name)
    if not elf_path.is_file():
        raise RuntimeError(f"Build succeeded but artifact is missing: {elf_path}")

    same_build_dir = work_build_dir.resolve() == canonical_build_dir.resolve()
    if not same_build_dir:
        _clean_build_dir(canonical_build_dir, app_dir=app_dir)
    else:
        canonical_build_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(elf_path, canonical_build_dir / "firmware.elf")
    _print_step(f"built {canonical_build_dir / 'firmware.elf'}")
    if hex_path is not None and hex_path.is_file():
        shutil.copy2(hex_path, canonical_build_dir / "firmware.hex")
        _print_step(f"built {canonical_build_dir / 'firmware.hex'}")


def _required_workspace_projects(board: str) -> list[tuple[str, Path]]:
    normalized = board.lower()
    requirements: list[tuple[str, Path]] = []
    if normalized.startswith("nucleo_") or "stm32" in normalized:
        requirements.append(("hal_stm32", Path("modules/hal/stm32")))
    if normalized.startswith("nrf"):
        requirements.append(("hal_nordic", Path("modules/hal/nordic")))
    return requirements


def _ensure_workspace_projects(runtime: ZephyrRuntime, board: str) -> None:
    for project_name, relative_path in _required_workspace_projects(board):
        project_path = runtime.workspace_dir / relative_path
        if project_path.exists():
            continue
        _print_step(
            f"workspace missing {relative_path}; attempting targeted west update for {project_name}"
        )
        _run(
            _west_cmd(runtime.west_python, "update", project_name),
            cwd=runtime.workspace_dir,
            env=_west_env(runtime.west_python, runtime.sdk_dir),
        )
        if not project_path.exists():
            raise RuntimeError(
                f"Workspace still missing required project after `west update {project_name}`: {project_path}"
            )


def run_build(args: argparse.Namespace, runtime: ZephyrRuntime) -> None:
    app_dir, build_dir = _preflight_build_request(args.app_dir, args.build_dir)
    with _cache_lock(build_dir):
        _claim_build_dir(app_dir, build_dir)
        _ensure_workspace_projects(runtime, args.board)

        env = os.environ.copy()
        env["ZEPHYR_BASE"] = str(runtime.workspace_dir / "zephyr")
        env["ZEPHYR_TOOLCHAIN_VARIANT"] = "zephyr"
        env["ZEPHYR_SDK_INSTALL_DIR"] = str(runtime.sdk_dir)
        env["PATH"] = _west_env(runtime.west_python, runtime.sdk_dir)["PATH"]

        work_app_dir = app_dir
        work_build_dir = build_dir
        scratch_root: Path | None = None
        if _should_use_scratch_build(app_dir, build_dir):
            scratch_root = Path(tempfile.mkdtemp(prefix="firmware-cli-zephyr-")).resolve()
            work_app_dir = scratch_root / "app"
            work_build_dir = scratch_root / "build"
            _copy_app_for_scratch(app_dir, build_dir, work_app_dir)
            _copy_adjacent_common_for_scratch(app_dir, scratch_root)
        elif args.pristine != "never" and not _build_cache_matches_app(
            work_build_dir, work_app_dir
        ):
            _print_step(
                f"clean stale build cache at {work_build_dir} because it points at a different app source"
            )
            _clean_build_dir(work_build_dir, app_dir=app_dir)

        try:
            _run(
                _west_cmd(
                    runtime.west_python,
                    "build",
                    "-p",
                    args.pristine,
                    "-b",
                    args.board,
                    str(work_app_dir),
                    "-d",
                    str(work_build_dir),
                ),
                cwd=runtime.workspace_dir,
                env=env,
            )
            _copy_artifacts(
                work_build_dir,
                build_dir,
                app_dir=app_dir,
                app_dir_name=work_app_dir.name,
            )
        finally:
            if scratch_root is not None:
                shutil.rmtree(scratch_root, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", help="Zephyr application source directory.")
    parser.add_argument(
        "--build-dir", help="Canonical output directory for firmware.elf / firmware.hex."
    )
    parser.add_argument("--board", help="Zephyr board target string, e.g. nucleo_l476rg.")
    parser.add_argument("--workspace-dir", help="Existing Zephyr workspace root to reuse.")
    parser.add_argument("--sdk-dir", help="Existing Zephyr SDK install dir to reuse.")
    parser.add_argument(
        "--managed-workspace-dir",
        default=str(_default_managed_workspace_dir(DEFAULT_MANAGED_ZEPHYR_REF)),
        help="Workspace path used for managed bootstrap when no existing workspace is found.",
    )
    parser.add_argument(
        "--managed-sdk-dir",
        default=str(_default_managed_sdk_dir()),
        help="SDK path used for managed install when no existing SDK is found.",
    )
    parser.add_argument(
        "--west-venv-dir",
        default=str(_default_west_venv_dir()),
        help="Private venv used to run west without relying on global installs.",
    )
    parser.add_argument(
        "--zephyr-repo",
        default=DEFAULT_MANAGED_ZEPHYR_REPO,
        help="Git URL used when bootstrapping a managed Zephyr workspace.",
    )
    parser.add_argument(
        "--zephyr-ref",
        default=DEFAULT_MANAGED_ZEPHYR_REF,
        help="Git ref used when bootstrapping a managed Zephyr workspace.",
    )
    parser.add_argument(
        "--toolchain",
        default=DEFAULT_TOOLCHAIN,
        help="Zephyr SDK toolchain component to install when a managed SDK is needed.",
    )
    parser.add_argument(
        "--pristine",
        choices=("auto", "always", "never"),
        default="auto",
        help="Pristine policy passed through to `west build -p`. Defaults to incremental-friendly `auto`.",
    )
    parser.add_argument(
        "--ensure-only",
        action="store_true",
        help="Provision or resolve west/workspace/SDK, then exit without building.",
    )
    parser.add_argument(
        "--skip-workspace-bootstrap",
        action="store_true",
        help="Refuse to create a managed workspace when no existing workspace is found.",
    )
    parser.add_argument(
        "--skip-sdk-install",
        action="store_true",
        help="Refuse to install a managed SDK when no existing SDK is found.",
    )
    return parser


def main() -> int:
    load_local_env()
    args = build_parser().parse_args()
    if not args.ensure_only and (not args.app_dir or not args.build_dir or not args.board):
        raise SystemExit(
            "--app-dir, --build-dir, and --board are required unless --ensure-only is set."
        )
    if not args.ensure_only:
        _preflight_build_request(args.app_dir, args.build_dir)

    runtime = ensure_runtime(args)
    _print_step(
        f"runtime ready: workspace={runtime.workspace_dir} ({runtime.workspace_source}), "
        f"sdk={runtime.sdk_dir} ({runtime.sdk_source})"
    )
    if args.ensure_only:
        return 0

    run_build(args, runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
