"""Cross-platform Zephyr workspace/bootstrap/build helper."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .local_env import load_local_env

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANAGED_ZEPHYR_REPO = "https://github.com/zephyrproject-rtos/zephyr.git"
DEFAULT_MANAGED_ZEPHYR_REF = "v4.3.0"
DEFAULT_TOOLCHAIN = "arm-zephyr-eabi"
DEFAULT_STEP_TIMEOUT_SECONDS = 1800


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


def _is_zephyr_workspace(path: Path) -> bool:
    return path.is_dir() and (path / "zephyr").is_dir()


def _is_zephyr_sdk(path: Path) -> bool:
    return path.is_dir() and (path / "sdk_version").is_file()


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
    for _project_name, relative_path in _required_workspace_projects(board):
        if not (workspace_dir / relative_path).exists():
            return False
    return True


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

    add(managed_sdk_dir, "managed-cache")

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

    return candidates


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
        result = subprocess.run(
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


def _ensure_west_python(west_venv_dir: Path) -> Path:
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
        and _python_has_module(west_python, "elftools")
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
        result = subprocess.run(
            [str(python_path), "-m", "west", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _python_has_module(python_path: Path, module_name: str) -> bool:
    try:
        result = subprocess.run(
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
        if candidate.exists() and _can_run_west(candidate):
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


def _sdk_version(workspace_dir: Path) -> str:
    sdk_version_path = workspace_dir / "zephyr" / "SDK_VERSION"
    if not sdk_version_path.is_file():
        raise RuntimeError(f"Missing Zephyr SDK_VERSION file: {sdk_version_path}")
    version = sdk_version_path.read_text(encoding="utf-8").splitlines()[0].strip()
    if not version:
        raise RuntimeError(f"Zephyr SDK_VERSION file was empty: {sdk_version_path}")
    return version


def _sdk_minimal_archive_filename(version: str) -> str:
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
    return f"zephyr-sdk-{version}_{os_name}-{arch}_minimal{ext}"


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


def _run_sdk_setup(west_python: Path, sdk_dir: Path, toolchain: str) -> None:
    if platform.system() == "Windows":
        setup_path = sdk_dir / "setup.cmd"
        option_prefix = "/"
    else:
        setup_path = sdk_dir / "setup.sh"
        option_prefix = "-"
    if not setup_path.is_file():
        raise RuntimeError(f"Managed Zephyr SDK is missing setup script: {setup_path}")
    env = _west_env(west_python, sdk_dir)
    _run([str(setup_path), f"{option_prefix}c"], cwd=sdk_dir, env=env)
    _run(
        [str(setup_path), f"{option_prefix}t", toolchain, f"{option_prefix}h"], cwd=sdk_dir, env=env
    )


def _install_managed_sdk(
    *,
    west_python: Path,
    workspace_dir: Path,
    managed_sdk_dir: Path,
    toolchain: str,
) -> None:
    version = _sdk_version(workspace_dir)
    filename = _sdk_minimal_archive_filename(version)
    archive_url = (
        f"https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v{version}/{filename}"
    )
    expected_sha = _expected_sdk_sha256(version, filename)

    with tempfile.TemporaryDirectory(prefix="firmcli-zephyr-sdk-") as temp_dir_text:
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
        if managed_sdk_dir.exists():
            shutil.rmtree(managed_sdk_dir)
        managed_sdk_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sdk_root), str(managed_sdk_dir))

    _run_sdk_setup(west_python, managed_sdk_dir, toolchain)


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
    for candidate in _iter_zephyr_workspace_candidates(
        explicit_workspace_dir=workspace_dir,
        managed_workspace_dir=managed_workspace_dir,
    ):
        if _is_zephyr_workspace(candidate.path) and _workspace_supports_board(
            candidate.path, board
        ):
            _print_step(f"using workspace: {candidate.path} ({candidate.source})")
            return candidate.path, candidate.source

    if skip_workspace_bootstrap:
        raise RuntimeError(
            "No usable Zephyr workspace found. Set ZEPHYR_WORKSPACE_DIR/--workspace-dir or allow managed bootstrap."
        )

    managed_workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    manifest_dir = managed_workspace_dir / "manifest"
    manifest_path = manifest_dir / "west.yml"

    if (
        managed_workspace_dir.exists()
        and any(managed_workspace_dir.iterdir())
        and not _is_zephyr_workspace(managed_workspace_dir)
        and not manifest_path.exists()
    ):
        raise RuntimeError(
            f"Managed workspace dir exists but is not a Zephyr workspace: {managed_workspace_dir}"
        )

    if not _is_zephyr_workspace(managed_workspace_dir):
        managed_workspace_dir.mkdir(parents=True, exist_ok=True)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            _managed_manifest_text(zephyr_repo=zephyr_repo, zephyr_ref=zephyr_ref),
            encoding="utf-8",
        )
        _print_step(
            f"bootstrap managed workspace at {managed_workspace_dir} from {zephyr_repo}@{zephyr_ref}"
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
        _install_zephyr_python_requirements(west_python, managed_workspace_dir)
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
        _install_zephyr_python_requirements(west_python, managed_workspace_dir)
    return managed_workspace_dir, "managed-bootstrap"


def _resolve_sdk_dir(
    *,
    west_python: Path,
    workspace_dir: Path,
    sdk_dir: Path | None,
    managed_sdk_dir: Path,
    toolchain: str,
    skip_sdk_install: bool,
) -> tuple[Path, str]:
    for candidate in _iter_sdk_candidates(
        explicit_sdk_dir=sdk_dir,
        managed_sdk_dir=managed_sdk_dir,
        workspace_dir=workspace_dir,
    ):
        if _is_zephyr_sdk(candidate.path):
            _print_step(f"using sdk: {candidate.path} ({candidate.source})")
            return candidate.path, candidate.source

    if skip_sdk_install:
        raise RuntimeError(
            "No usable Zephyr SDK found. Set ZEPHYR_SDK_INSTALL_DIR/--sdk-dir or allow managed SDK install."
        )

    managed_sdk_dir.parent.mkdir(parents=True, exist_ok=True)
    _print_step(f"install managed sdk at {managed_sdk_dir}")
    _install_managed_sdk(
        west_python=west_python,
        workspace_dir=workspace_dir,
        managed_sdk_dir=managed_sdk_dir,
        toolchain=toolchain,
    )
    if not _is_zephyr_sdk(managed_sdk_dir):
        raise RuntimeError(
            f"Managed SDK install completed but sdk_version was not found at {managed_sdk_dir}"
        )
    return managed_sdk_dir, "managed-install"


def ensure_runtime(args: argparse.Namespace) -> ZephyrRuntime:
    west_venv_dir = Path(args.west_venv_dir).expanduser().resolve()
    managed_workspace_dir = Path(args.managed_workspace_dir).expanduser().resolve()
    managed_sdk_dir = Path(args.managed_sdk_dir).expanduser().resolve()
    workspace_dir = Path(args.workspace_dir).expanduser().resolve() if args.workspace_dir else None
    sdk_dir = Path(args.sdk_dir).expanduser().resolve() if args.sdk_dir else None

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
    if resolved_workspace_dir == managed_workspace_dir:
        _run(
            _west_cmd(west_python, "zephyr-export"),
            cwd=resolved_workspace_dir,
            env=_west_env(west_python),
        )
        _install_zephyr_python_requirements(west_python, resolved_workspace_dir)
    resolved_sdk_dir, sdk_source = _resolve_sdk_dir(
        west_python=west_python,
        workspace_dir=resolved_workspace_dir,
        sdk_dir=sdk_dir,
        managed_sdk_dir=managed_sdk_dir,
        toolchain=args.toolchain,
        skip_sdk_install=args.skip_sdk_install,
    )
    west_python = _resolve_toolchain_python(resolved_sdk_dir, west_python)
    return ZephyrRuntime(
        workspace_dir=resolved_workspace_dir,
        workspace_source=workspace_source,
        sdk_dir=resolved_sdk_dir,
        sdk_source=sdk_source,
        west_python=west_python,
        managed_workspace_dir=managed_workspace_dir,
    )


def _clean_build_dir(build_dir: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    for child in build_dir.iterdir():
        if child.name in {".gitkeep", ".gitignore"}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _cmake_cache_source_dir(build_dir: Path) -> Path | None:
    cache_path = build_dir / "CMakeCache.txt"
    if not cache_path.is_file():
        return None
    for line in cache_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("CMAKE_HOME_DIRECTORY:"):
            _key, _sep, value = line.partition("=")
            value = value.strip()
            if value:
                return Path(value).expanduser().resolve()
    return None


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


def _copy_artifacts(work_build_dir: Path, canonical_build_dir: Path, *, app_dir_name: str) -> None:
    elf_path, hex_path = _resolve_artifact_paths(work_build_dir, app_dir_name=app_dir_name)
    if not elf_path.is_file():
        raise RuntimeError(f"Build succeeded but artifact is missing: {elf_path}")

    same_build_dir = work_build_dir.resolve() == canonical_build_dir.resolve()
    if not same_build_dir:
        _clean_build_dir(canonical_build_dir)
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
    app_dir = Path(args.app_dir).expanduser().resolve()
    build_dir = Path(args.build_dir).expanduser().resolve()
    if not app_dir.is_dir():
        raise RuntimeError(f"App dir does not exist: {app_dir}")
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
        shutil.copytree(app_dir, work_app_dir)
        _copy_adjacent_common_for_scratch(app_dir, scratch_root)
    elif args.pristine != "never" and not _build_cache_matches_app(work_build_dir, work_app_dir):
        _print_step(
            f"clean stale build cache at {work_build_dir} because it points at a different app source"
        )
        _clean_build_dir(work_build_dir)

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
        _copy_artifacts(work_build_dir, build_dir, app_dir_name=work_app_dir.name)
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
