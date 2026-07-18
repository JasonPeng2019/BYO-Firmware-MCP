from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from pyocd_debug_mcp import zephyr_build


def test_workspace_candidates_prefer_explicit_env_and_common_locations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "explicit-workspace"
    env_workspace = tmp_path / "env-workspace"
    home = tmp_path / "home"
    managed = tmp_path / "managed-workspace"
    (explicit / "zephyr").mkdir(parents=True)
    (env_workspace / "zephyr").mkdir(parents=True)
    (home / "zephyrproject" / "zephyr").mkdir(parents=True)
    monkeypatch.setenv("ZEPHYR_WORKSPACE_DIR", str(env_workspace))
    monkeypatch.setattr(zephyr_build.Path, "home", staticmethod(lambda: home))

    candidates = zephyr_build._iter_zephyr_workspace_candidates(
        explicit_workspace_dir=explicit,
        managed_workspace_dir=managed,
    )

    assert candidates[0].path == explicit.resolve()
    assert candidates[0].source == "--workspace-dir"
    assert any(candidate.path == env_workspace.resolve() for candidate in candidates)
    assert any(candidate.path == (home / "zephyrproject").resolve() for candidate in candidates)
    assert candidates[-1].path == managed.resolve()


def test_sdk_candidates_include_workspace_adjacent_ncs_toolchain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ncs" / "v3.3.1"
    managed = tmp_path / "managed-sdk"
    toolchain_sdk = workspace.parent / "toolchains" / "1234" / "opt" / "zephyr-sdk"
    toolchain_sdk.mkdir(parents=True)
    (toolchain_sdk / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)

    candidates = zephyr_build._iter_sdk_candidates(
        explicit_sdk_dir=None,
        managed_sdk_dir=managed,
        workspace_dir=workspace,
    )

    assert candidates[-1].path == managed.resolve()
    assert any(candidate.path == toolchain_sdk.resolve() for candidate in candidates)
    assert candidates.index(
        next(candidate for candidate in candidates if candidate.path == toolchain_sdk.resolve())
    ) < candidates.index(
        next(candidate for candidate in candidates if candidate.path == managed.resolve())
    )


def test_workspace_support_requires_exact_board_target_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "modules" / "hal" / "nordic").mkdir(parents=True)
    board_dir = workspace / "zephyr" / "boards" / "nordic" / "nrf52840dk"
    board_dir.mkdir(parents=True)
    (board_dir / "nrf52840dk_nrf52840_defconfig").write_text("", encoding="utf-8")

    assert zephyr_build._workspace_supports_board(workspace, "nrf52840dk/nrf52840")
    assert not zephyr_build._workspace_supports_board(workspace, "nrf52840dk/nrf52833")
    assert not zephyr_build._workspace_supports_board(workspace, "nrf52840dk_similar/nrf52840")
    assert not zephyr_build._workspace_supports_board(workspace, "../nrf52840dk")


def test_sdk_candidates_include_detected_global_ncs_toolchain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    managed = tmp_path / "managed-sdk"
    home = tmp_path / "home"
    ncs_sdk = home / "ncs" / "toolchains" / "abcd1234" / "opt" / "zephyr-sdk"
    ncs_sdk.mkdir(parents=True)
    (ncs_sdk / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    monkeypatch.setattr(zephyr_build, "sys", type("Sys", (), {"platform": "win32"})())
    monkeypatch.setattr(zephyr_build.Path, "home", staticmethod(lambda: home))

    candidates = zephyr_build._iter_sdk_candidates(
        explicit_sdk_dir=None,
        managed_sdk_dir=managed,
        workspace_dir=workspace,
    )

    assert any(candidate.path == ncs_sdk.resolve() for candidate in candidates)


def test_sdk_minimal_archive_filename_matches_zephyr_release_naming(monkeypatch) -> None:
    monkeypatch.setattr(zephyr_build.platform, "system", lambda: "Windows")
    monkeypatch.setattr(zephyr_build.platform, "machine", lambda: "AMD64")
    assert zephyr_build._sdk_minimal_archive_filename("0.17.4") == (
        "zephyr-sdk-0.17.4_windows-x86_64_minimal.7z"
    )

    monkeypatch.setattr(zephyr_build.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(zephyr_build.platform, "machine", lambda: "arm64")
    assert zephyr_build._sdk_minimal_archive_filename("0.17.4") == (
        "zephyr-sdk-0.17.4_macos-aarch64_minimal.tar.xz"
    )


def test_resolve_sdk_dir_uses_managed_installer_when_no_sdk_exists(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    managed = tmp_path / "managed-sdk"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    monkeypatch.setattr(
        zephyr_build,
        "_iter_sdk_candidates",
        lambda *, explicit_sdk_dir, managed_sdk_dir, workspace_dir: [
            zephyr_build.CandidatePath(path=managed_sdk_dir.resolve(), source="managed-cache")
        ],
    )

    def fake_install(
        *, west_python: Path, workspace_dir: Path, managed_sdk_dir: Path, toolchain: str
    ) -> None:
        captured["west_python"] = west_python
        captured["workspace_dir"] = workspace_dir
        captured["managed_sdk_dir"] = managed_sdk_dir
        captured["toolchain"] = toolchain
        managed_sdk_dir.mkdir(parents=True, exist_ok=True)
        (managed_sdk_dir / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
        compiler = (
            managed_sdk_dir
            / "arm-zephyr-eabi"
            / "bin"
            / (
                "arm-zephyr-eabi-gcc.exe"
                if zephyr_build.sys.platform == "win32"
                else "arm-zephyr-eabi-gcc"
            )
        )
        compiler.parent.mkdir(parents=True)
        compiler.write_text("", encoding="utf-8")
        zephyr_build._write_managed_sdk_identity(
            managed_sdk_dir, version="0.17.4", toolchain="arm-zephyr-eabi"
        )

    monkeypatch.setattr(zephyr_build, "_install_managed_sdk", fake_install)
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: True)

    resolved_dir, source = zephyr_build._resolve_sdk_dir(
        west_python=tmp_path / "west-python.exe",
        workspace_dir=workspace,
        sdk_dir=None,
        managed_sdk_dir=managed,
        toolchain="arm-zephyr-eabi",
        skip_sdk_install=False,
    )

    assert resolved_dir == (managed / "0.17.4").resolve()
    assert source == "managed-install"
    assert captured["workspace_dir"] == workspace.resolve()
    assert captured["managed_sdk_dir"] == (managed / "0.17.4").resolve()
    assert captured["toolchain"] == "arm-zephyr-eabi"


def test_clean_build_dir_preserves_gitkeep_and_gitignore(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / ".gitkeep").write_text("", encoding="utf-8")
    (build_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    zephyr_build._claim_build_dir(app_dir, build_dir)
    (build_dir / "firmware.elf").write_text("old", encoding="utf-8")
    (build_dir / "temp").mkdir()
    (build_dir / "temp" / "nested.txt").write_text("nested", encoding="utf-8")

    zephyr_build._clean_build_dir(build_dir, app_dir=app_dir)

    assert (build_dir / ".gitkeep").exists()
    assert (build_dir / ".gitignore").exists()
    assert not (build_dir / "firmware.elf").exists()
    assert not (build_dir / "temp").exists()


@pytest.mark.parametrize("scratch", [False, True])
def test_run_build_preserves_foreign_output_sentinel_before_stale_or_scratch_cleanup(
    monkeypatch, tmp_path: Path, scratch: bool
) -> None:
    app_dir = tmp_path / ("app with spaces" if scratch else "app")
    app_dir.mkdir()
    build_dir = tmp_path / ("output with spaces" if scratch else "output")
    build_dir.mkdir()
    sentinel = build_dir / "customer-data.bin"
    sentinel.write_bytes(b"preserve")
    if not scratch:
        (build_dir / "CMakeCache.txt").write_text(
            f"CMAKE_HOME_DIRECTORY:INTERNAL={tmp_path / 'different-app'}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        zephyr_build,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("foreign output must fail before west build"),
    )
    runtime = zephyr_build.ZephyrRuntime(
        workspace_dir=tmp_path / "workspace",
        workspace_source="test",
        sdk_dir=tmp_path / "sdk",
        sdk_source="test",
        west_python=tmp_path / "python",
        managed_workspace_dir=tmp_path / "managed",
    )
    args = Namespace(
        app_dir=str(app_dir),
        build_dir=str(build_dir),
        board="nrf52840dk/nrf52840",
        pristine="auto",
    )

    with pytest.raises(RuntimeError, match="nonempty unowned build directory"):
        zephyr_build.run_build(args, runtime)

    assert sentinel.read_bytes() == b"preserve"


def test_two_process_builds_sharing_output_serialize(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    build_dir = tmp_path / "build"
    log_path = tmp_path / "order.log"
    script = r"""
import os, sys, time
from argparse import Namespace
from pathlib import Path
from pyocd_debug_mcp import zephyr_build as zb

app = Path(sys.argv[1])
build = Path(sys.argv[2])
log = Path(sys.argv[3])
zb._ensure_workspace_projects = lambda *_args: None
def fake_run(cmd, **_kwargs):
    with log.open('a', encoding='utf-8') as handle:
        handle.write(f'START {os.getpid()}\n')
    time.sleep(0.35)
    output = Path(cmd[cmd.index('-d') + 1]) / 'zephyr'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'zephyr.elf').write_bytes(str(os.getpid()).encode())
    (output / 'zephyr.map').write_text('map', encoding='utf-8')
    with log.open('a', encoding='utf-8') as handle:
        handle.write(f'END {os.getpid()}\n')
zb._run = fake_run
runtime = zb.ZephyrRuntime(Path('.'), 'test', Path('.'), 'test', Path(sys.executable), Path('.'))
args = Namespace(app_dir=str(app), build_dir=str(build), board='nrf52840dk/nrf52840', pristine='auto')
zb.run_build(args, runtime)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(app_dir), str(build_dir), str(log_path)],
            cwd=Path.cwd(),
        )
        for _index in range(2)
    ]
    for process in processes:
        assert process.wait(timeout=10) == 0

    rows = log_path.read_text(encoding="utf-8").splitlines()
    assert [row.split()[0] for row in rows] == ["START", "END", "START", "END"]
    assert rows[0].split()[1] == rows[1].split()[1]
    assert rows[2].split()[1] == rows[3].split()[1]


def test_build_cache_matches_app_uses_cmake_home_directory(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    app_dir = tmp_path / "app"
    other_app_dir = tmp_path / "other-app"
    build_dir.mkdir()
    app_dir.mkdir()
    other_app_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={app_dir.resolve()}\n",
        encoding="utf-8",
    )

    assert zephyr_build._build_cache_matches_app(build_dir, app_dir) is True
    assert zephyr_build._build_cache_matches_app(build_dir, other_app_dir) is False


def test_build_cache_matches_sysbuild_main_app_dir(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    app_dir = tmp_path / "app"
    sysbuild_template = tmp_path / "zephyr" / "share" / "sysbuild"
    build_dir.mkdir()
    app_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text(
        f"APP_DIR:PATH={app_dir.resolve()}\n"
        f"CMAKE_HOME_DIRECTORY:INTERNAL={sysbuild_template.resolve()}\n",
        encoding="utf-8",
    )

    assert zephyr_build._build_cache_matches_app(build_dir, app_dir) is True


def test_copy_artifacts_preserves_live_build_tree_when_output_dir_matches_work_dir(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    build_dir = tmp_path / "build"
    zephyr_dir = build_dir / "zephyr"
    zephyr_dir.mkdir(parents=True)
    elf_contents = "elf"
    hex_contents = "hex"
    map_contents = "map"
    (zephyr_dir / "zephyr.elf").write_text(elf_contents, encoding="utf-8")
    (zephyr_dir / "zephyr.hex").write_text(hex_contents, encoding="utf-8")
    (zephyr_dir / "zephyr.map").write_text(map_contents, encoding="utf-8")
    (build_dir / "build.ninja").write_text("ninja", encoding="utf-8")

    zephyr_build._copy_artifacts(build_dir, build_dir, app_dir=app_dir)

    assert (build_dir / "build.ninja").read_text(encoding="utf-8") == "ninja"
    assert (zephyr_dir / "zephyr.elf").read_text(encoding="utf-8") == elf_contents
    assert (build_dir / "firmware.elf").read_text(encoding="utf-8") == elf_contents
    assert (build_dir / "firmware.hex").read_text(encoding="utf-8") == hex_contents
    assert (build_dir / "firmware.map").read_text(encoding="utf-8") == map_contents
    assert (build_dir / "build-manifest.json").is_file()


def test_copy_artifacts_resolves_sysbuild_app_subdir(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    build_dir = tmp_path / "build"
    app_zephyr_dir = build_dir / "main_domain" / "zephyr"
    app_zephyr_dir.mkdir(parents=True)
    (app_zephyr_dir / "zephyr.elf").write_text("elf", encoding="utf-8")
    (app_zephyr_dir / "zephyr.hex").write_text("hex", encoding="utf-8")
    (app_zephyr_dir / "zephyr.map").write_text("map", encoding="utf-8")

    (app_zephyr_dir / "zephyr.bin").write_bytes(b"bin")
    aggregate = build_dir / "zephyr"
    aggregate.mkdir()
    (aggregate / "zephyr.elf").write_text("aggregate", encoding="utf-8")
    (aggregate / "zephyr.map").write_text("aggregate-map", encoding="utf-8")
    bootloader = build_dir / "mcuboot" / "zephyr"
    bootloader.mkdir(parents=True)
    (bootloader / "zephyr.elf").write_text("bootloader", encoding="utf-8")
    (bootloader / "zephyr.map").write_text("bootloader-map", encoding="utf-8")
    (build_dir / "domains.yaml").write_text(
        "default: main_domain\n"
        "build_dir: ignored-root\n"
        "domains:\n"
        "  - name: mcuboot\n"
        f"    build_dir: {bootloader.parent.as_posix()}\n"
        "  - name: main_domain\n"
        f"    build_dir: {app_zephyr_dir.parent.as_posix()}\n",
        encoding="utf-8",
    )

    zephyr_build._copy_artifacts(build_dir, build_dir, app_dir=app_dir)

    assert (build_dir / "firmware.elf").read_text(encoding="utf-8") == "elf"
    assert (build_dir / "firmware.hex").read_text(encoding="utf-8") == "hex"
    assert (build_dir / "firmware.map").read_text(encoding="utf-8") == "map"
    assert (build_dir / "firmware.bin").read_bytes() == b"bin"


def test_copy_artifacts_requires_map_and_preserves_prior_exports_when_zephyr_omits_it(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    build_dir = tmp_path / "build"
    zephyr_dir = build_dir / "zephyr"
    zephyr_dir.mkdir(parents=True)
    (zephyr_dir / "zephyr.elf").write_text("elf", encoding="utf-8")
    (build_dir / "firmware.elf").write_text("prior", encoding="utf-8")
    (build_dir / "build-manifest.json").write_text("prior-manifest", encoding="utf-8")

    with pytest.raises(RuntimeError, match="linker map"):
        zephyr_build._copy_artifacts(build_dir, build_dir, app_dir=app_dir)

    assert (build_dir / "firmware.elf").read_text(encoding="utf-8") == "prior"
    assert (build_dir / "build-manifest.json").read_text(encoding="utf-8") == "prior-manifest"
    assert not (build_dir / "firmware.map").exists()


def test_resolve_artifacts_rejects_ambiguous_sysbuild_default(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    for name in ("one", "two"):
        zephyr_dir = build_dir / name / "zephyr"
        zephyr_dir.mkdir(parents=True)
        (zephyr_dir / "zephyr.elf").write_text(name, encoding="utf-8")
        (zephyr_dir / "zephyr.map").write_text(f"{name}-map", encoding="utf-8")
    (build_dir / "domains.yaml").write_text(
        "default: app\n"
        "domains:\n"
        f"  - {{name: app, build_dir: {str(build_dir / 'one')!r}}}\n"
        f"  - {{name: app, build_dir: {str(build_dir / 'two')!r}}}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        zephyr_build._resolve_artifact_paths(build_dir)


def test_build_parser_defaults_to_incremental_pristine_mode() -> None:
    args = zephyr_build.build_parser().parse_args(
        [
            "--app-dir",
            "app",
            "--build-dir",
            "build",
            "--board",
            "nucleo_l476rg",
        ]
    )

    assert args.pristine == "auto"


def test_ensure_west_python_reinstalls_when_pyelftools_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    west_venv_dir = tmp_path / "west-venv"
    bin_dir = west_venv_dir / ("Scripts" if zephyr_build.sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    executable_suffix = ".exe" if zephyr_build.sys.platform == "win32" else ""
    west_python = bin_dir / f"python{executable_suffix}"
    cmake = bin_dir / f"cmake{executable_suffix}"
    ninja = bin_dir / f"ninja{executable_suffix}"
    west_python.write_text("", encoding="utf-8")
    cmake.write_text("", encoding="utf-8")
    ninja.write_text("", encoding="utf-8")

    run_calls: list[list[str]] = []

    def fake_has_module(_python: Path, module: str) -> bool:
        return module == "patoolib"

    def fake_run(
        cmd: list[str], cwd: Path | None = None, env=None, timeout_seconds: int = 600
    ) -> None:
        del cwd, env, timeout_seconds
        run_calls.append(cmd)

    monkeypatch.setattr(zephyr_build, "_python_has_module", fake_has_module)
    monkeypatch.setattr(zephyr_build, "_run", fake_run)

    resolved = zephyr_build._ensure_west_python(west_venv_dir)

    assert resolved == west_python
    assert any(
        cmd[:5] == [str(west_python), "-m", "pip", "install", "--upgrade"] for cmd in run_calls
    )
    assert any("pyelftools" in cmd for cmd in run_calls)


def test_ensure_west_python_installs_extractor_in_the_actual_archive_interpreter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    west_venv_dir = tmp_path / "west-venv"
    bin_dir = west_venv_dir / ("Scripts" if zephyr_build.sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    suffix = ".exe" if zephyr_build.sys.platform == "win32" else ""
    west_python = bin_dir / f"python{suffix}"
    for executable in (west_python, bin_dir / f"cmake{suffix}", bin_dir / f"ninja{suffix}"):
        executable.write_text("", encoding="utf-8")
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        zephyr_build,
        "_python_has_module",
        lambda _python, module: module in {"patoolib", "elftools"},
    )
    monkeypatch.setattr(
        zephyr_build,
        "_run",
        lambda cmd, **_kwargs: run_calls.append(cmd),
    )

    resolved = zephyr_build._ensure_west_python(west_venv_dir)

    assert resolved == west_python
    install = next(cmd for cmd in run_calls if "pip" in cmd and "py7zr" in cmd)
    assert install[0] == str(west_python)


def test_ensure_west_python_repairs_cache_when_west_or_yaml_is_unusable(
    monkeypatch, tmp_path: Path
) -> None:
    west_venv_dir = tmp_path / "west-venv"
    bin_dir = west_venv_dir / ("Scripts" if zephyr_build.sys.platform == "win32" else "bin")
    suffix = ".exe" if zephyr_build.sys.platform == "win32" else ""
    west_python = bin_dir / f"python{suffix}"
    bin_dir.mkdir(parents=True)
    for executable in (west_python, bin_dir / f"cmake{suffix}", bin_dir / f"ninja{suffix}"):
        executable.write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(zephyr_build, "_python_has_module", lambda *_args: True)
    monkeypatch.setattr(zephyr_build, "_can_run_west", lambda _path: False)
    monkeypatch.setattr(zephyr_build, "_run", lambda cmd, **_kwargs: calls.append(cmd))

    zephyr_build._ensure_west_python(west_venv_dir)

    assert any("pip" in cmd and "west" in cmd for cmd in calls)


def test_workspace_requirements_install_once_then_reuse_offline(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    requirements = workspace / "zephyr" / "scripts" / "requirements-base.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("example-package>=1\n", encoding="utf-8")
    west_venv = tmp_path / "west-venv"
    west_python = zephyr_build._venv_python_path(west_venv)
    west_python.parent.mkdir(parents=True)
    west_python.write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(zephyr_build, "_run", lambda cmd, **_kwargs: calls.append(cmd))
    monkeypatch.setattr(zephyr_build, "_workspace_requirements_satisfied", lambda *_args: True)

    zephyr_build._ensure_workspace_python_requirements(
        west_python=west_python,
        west_venv_dir=west_venv,
        workspace_dir=workspace,
    )
    zephyr_build._ensure_workspace_python_requirements(
        west_python=west_python,
        west_venv_dir=west_venv,
        workspace_dir=workspace,
    )

    installs = [cmd for cmd in calls if "pip" in cmd and "-r" in cmd]
    assert len(installs) == 1


def test_workspace_requirements_marker_is_repaired_when_environment_is_damaged(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    requirements = workspace / "zephyr" / "scripts" / "requirements-base.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("example-package>=1\n", encoding="utf-8")
    west_venv = tmp_path / "west-venv"
    west_python = zephyr_build._venv_python_path(west_venv)
    west_python.parent.mkdir(parents=True)
    west_python.write_text("", encoding="utf-8")
    healthy = False
    installs = 0

    def requirements_satisfied(*_args) -> bool:
        return healthy

    def install(*_args) -> None:
        nonlocal healthy, installs
        installs += 1
        healthy = True

    monkeypatch.setattr(zephyr_build, "_workspace_requirements_satisfied", requirements_satisfied)
    monkeypatch.setattr(zephyr_build, "_install_zephyr_python_requirements", install)

    zephyr_build._ensure_workspace_python_requirements(
        west_python=west_python,
        west_venv_dir=west_venv,
        workspace_dir=workspace,
    )
    healthy = False
    zephyr_build._ensure_workspace_python_requirements(
        west_python=west_python,
        west_venv_dir=west_venv,
        workspace_dir=workspace,
    )

    assert installs == 2


def test_different_workspace_requirements_use_immutable_fingerprinted_venvs(
    monkeypatch, tmp_path: Path
) -> None:
    workspaces = [tmp_path / "workspace-a", tmp_path / "workspace-b"]
    for index, workspace in enumerate(workspaces):
        requirements = workspace / "zephyr" / "scripts" / "requirements-base.txt"
        requirements.parent.mkdir(parents=True)
        requirements.write_text(f"example-package=={index + 1}\n", encoding="utf-8")
    base_venv = tmp_path / "west-venv"
    venvs = [
        zephyr_build._workspace_requirements_venv_dir(base_venv, workspace)
        for workspace in workspaces
    ]
    pythons = [zephyr_build._venv_python_path(venv) for venv in venvs]
    for python in pythons:
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
    healthy: set[Path] = set()
    installs: list[Path] = []

    monkeypatch.setattr(
        zephyr_build,
        "_workspace_requirements_satisfied",
        lambda python, _workspace: python in healthy,
    )

    def install(python: Path, _workspace: Path) -> None:
        installs.append(python)
        healthy.add(python)

    monkeypatch.setattr(zephyr_build, "_install_zephyr_python_requirements", install)

    def ensure(index: int) -> None:
        zephyr_build._ensure_workspace_python_requirements(
            west_python=pythons[index],
            west_venv_dir=venvs[index],
            workspace_dir=workspaces[index],
        )

    threads = [threading.Thread(target=ensure, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)
    ensure(0)

    assert venvs[0] != venvs[1]
    assert all(not thread.is_alive() for thread in threads)
    assert installs.count(pythons[0]) == 1
    assert installs.count(pythons[1]) == 1


def test_concurrent_workspace_requirement_ensure_serializes_shared_venv(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    requirements = workspace / "zephyr" / "scripts" / "requirements-base.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("example-package>=1\n", encoding="utf-8")
    west_venv = tmp_path / "west-venv"
    west_python = zephyr_build._venv_python_path(west_venv)
    west_python.parent.mkdir(parents=True)
    west_python.write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    entered = threading.Event()
    release = threading.Event()

    def fake_run(cmd: list[str], **_kwargs) -> None:
        calls.append(cmd)
        entered.set()
        assert release.wait(timeout=2.0)

    monkeypatch.setattr(zephyr_build, "_run", fake_run)
    monkeypatch.setattr(zephyr_build, "_workspace_requirements_satisfied", lambda *_args: True)

    def ensure() -> None:
        zephyr_build._ensure_workspace_python_requirements(
            west_python=west_python,
            west_venv_dir=west_venv,
            workspace_dir=workspace,
        )

    first = threading.Thread(target=ensure)
    second = threading.Thread(target=ensure)
    first.start()
    assert entered.wait(timeout=2.0)
    second.start()
    release.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive() and not second.is_alive()
    installs = [cmd for cmd in calls if "pip" in cmd and "-r" in cmd]
    assert len(installs) == 1


def test_cache_lock_is_bounded_and_reusable_after_owner_releases(tmp_path: Path) -> None:
    resource = tmp_path / "shared-cache"
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with zephyr_build._cache_lock(resource):
            acquired.set()
            assert release.wait(timeout=2.0)

    owner = threading.Thread(target=hold_lock)
    owner.start()
    assert acquired.wait(timeout=2.0)
    with pytest.raises(RuntimeError, match="Timed out waiting"):
        with zephyr_build._cache_lock(resource, timeout_seconds=0.05):
            pass
    release.set()
    owner.join(timeout=2.0)
    assert not owner.is_alive()
    with zephyr_build._cache_lock(resource, timeout_seconds=0.5):
        pass


def test_cache_lock_recovers_after_process_death_and_replaces_stale_metadata(
    tmp_path: Path,
) -> None:
    resource = tmp_path / "shared-process-cache"
    script = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from pyocd_debug_mcp.zephyr_build import _cache_lock\n"
        "with _cache_lock(Path(sys.argv[1])):\n"
        " print('LOCKED', flush=True)\n"
        " time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(resource)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(RuntimeError, match="Timed out waiting"):
            with zephyr_build._cache_lock(resource, timeout_seconds=0.05):
                pass
    finally:
        child.terminate()
        child.wait(timeout=5)

    with zephyr_build._cache_lock(resource, timeout_seconds=1.0):
        pass
    lock_path = resource.parent / f".{resource.name}{zephyr_build.CACHE_LOCK_SUFFIX}"
    owner = lock_path.read_text(encoding="utf-8")
    assert f'"pid": {os.getpid()}' in owner


def test_should_use_scratch_build_for_long_windows_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(zephyr_build, "sys", type("Sys", (), {"platform": "win32"})())
    long_root = tmp_path
    for segment in ("deep", "deep", "deep", "deep", "deep", "deep", "deep", "deep"):
        long_root = long_root / segment
    app_dir = long_root / "app"
    build_dir = long_root / "build"
    app_dir.mkdir(parents=True)
    build_dir.mkdir(parents=True)

    assert zephyr_build._should_use_scratch_build(app_dir, build_dir) is True


def test_copy_adjacent_common_for_scratch_preserves_local_app_common_layout(tmp_path: Path) -> None:
    app_root = tmp_path / "workspace"
    app_dir = app_root / "src"
    common_dir = app_root / "common"
    scratch_root = tmp_path / "scratch"
    app_dir.mkdir(parents=True)
    common_dir.mkdir(parents=True)
    (common_dir / "nucleo_l476rg.overlay").write_text("overlay", encoding="utf-8")

    zephyr_build._copy_adjacent_common_for_scratch(app_dir, scratch_root)

    assert (scratch_root / "common" / "nucleo_l476rg.overlay").read_text(
        encoding="utf-8"
    ) == "overlay"


def test_sdk_resolution_skips_incompatible_cached_version(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    incompatible = tmp_path / "sdk-old"
    compatible = tmp_path / "sdk-current"
    incompatible.mkdir()
    compatible.mkdir()
    (incompatible / "sdk_version").write_text("1.0.0\n", encoding="utf-8")
    (compatible / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
    for sdk in (incompatible, compatible):
        compiler = (
            sdk
            / "arm-zephyr-eabi"
            / "bin"
            / (
                "arm-zephyr-eabi-gcc.exe"
                if zephyr_build.sys.platform == "win32"
                else "arm-zephyr-eabi-gcc"
            )
        )
        compiler.parent.mkdir(parents=True)
        compiler.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_iter_sdk_candidates",
        lambda **_kwargs: [
            zephyr_build.CandidatePath(incompatible, "stale-cache"),
            zephyr_build.CandidatePath(compatible, "matching-cache"),
        ],
    )
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: True)

    resolved, source = zephyr_build._resolve_sdk_dir(
        west_python=tmp_path / "python",
        workspace_dir=workspace,
        sdk_dir=None,
        managed_sdk_dir=tmp_path / "managed",
        toolchain="arm-zephyr-eabi",
        skip_sdk_install=True,
    )

    assert resolved == compatible
    assert source == "matching-cache"


def test_sdk_resolution_prefers_patch_compatible_local_ncs_over_managed_cache(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "ncs" / "v3.3.1"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    local_sdk = tmp_path / "ncs-sdk"
    managed_sdk = tmp_path / "managed" / "0.17.4"
    for sdk, version in ((local_sdk, "0.17.0"), (managed_sdk, "0.17.4")):
        sdk.mkdir(parents=True)
        (sdk / "sdk_version").write_text(f"{version}\n", encoding="utf-8")
        compiler = (
            sdk
            / "arm-zephyr-eabi"
            / "bin"
            / (
                "arm-zephyr-eabi-gcc.exe"
                if zephyr_build.sys.platform == "win32"
                else "arm-zephyr-eabi-gcc"
            )
        )
        compiler.parent.mkdir(parents=True)
        compiler.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_iter_sdk_candidates",
        lambda **_kwargs: [
            zephyr_build.CandidatePath(local_sdk, "detected-ncs-toolchain"),
            zephyr_build.CandidatePath(managed_sdk, "managed-cache"),
        ],
    )
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: True)

    resolved, source = zephyr_build._resolve_sdk_dir(
        west_python=tmp_path / "python",
        workspace_dir=workspace,
        sdk_dir=None,
        managed_sdk_dir=tmp_path / "managed",
        toolchain="arm-zephyr-eabi",
        skip_sdk_install=True,
    )

    assert resolved == local_sdk
    assert source == "detected-ncs-toolchain; compatible-sdk-0.17.0"


def test_sdk_resolution_skips_incomplete_managed_cache(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    incomplete = tmp_path / "managed" / "0.17.4"
    complete = tmp_path / "local"
    for sdk in (incomplete, complete):
        sdk.mkdir(parents=True)
        (sdk / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
    compiler = (
        complete
        / "arm-zephyr-eabi"
        / "bin"
        / (
            "arm-zephyr-eabi-gcc.exe"
            if zephyr_build.sys.platform == "win32"
            else "arm-zephyr-eabi-gcc"
        )
    )
    compiler.parent.mkdir(parents=True)
    compiler.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_iter_sdk_candidates",
        lambda **_kwargs: [
            zephyr_build.CandidatePath(incomplete, "managed-cache"),
            zephyr_build.CandidatePath(complete, "detected-ncs-toolchain"),
        ],
    )
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: True)

    resolved, source = zephyr_build._resolve_sdk_dir(
        west_python=tmp_path / "python",
        workspace_dir=workspace,
        sdk_dir=None,
        managed_sdk_dir=tmp_path / "managed",
        toolchain="arm-zephyr-eabi",
        skip_sdk_install=True,
    )

    assert resolved == complete
    assert source == "detected-ncs-toolchain"


def test_ensure_runtime_uses_complete_local_ncs_offline_before_private_venv(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "ncs" / "v3.3.1"
    zephyr_dir = workspace / "zephyr"
    (workspace / "modules" / "hal" / "nordic").mkdir(parents=True)
    zephyr_dir.mkdir(parents=True)
    (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
    (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    board_dir = zephyr_dir / "boards" / "nordic" / "nrf52840dk"
    board_dir.mkdir(parents=True)
    (board_dir / "nrf52840dk_nrf52840_defconfig").write_text("", encoding="utf-8")
    sdk = tmp_path / "ncs" / "toolchains" / "hash" / "opt" / "zephyr-sdk"
    (sdk / "arm-zephyr-eabi" / "bin").mkdir(parents=True)
    (sdk / "sdk_version").write_text("0.17.0\n", encoding="utf-8")
    compiler_name = (
        "arm-zephyr-eabi-gcc.exe" if zephyr_build.sys.platform == "win32" else "arm-zephyr-eabi-gcc"
    )
    (sdk / "arm-zephyr-eabi" / "bin" / compiler_name).write_text("", encoding="utf-8")
    bundled_python = (
        sdk.parent / "bin" / ("python.exe" if zephyr_build.sys.platform == "win32" else "python")
    )
    bundled_python.parent.mkdir(parents=True)
    bundled_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(workspace, "detected-ncs")],
    )
    monkeypatch.setattr(
        zephyr_build,
        "_iter_sdk_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(sdk, "detected-ncs-toolchain")],
    )
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: True)
    monkeypatch.setattr(
        zephyr_build,
        "_is_complete_build_python",
        lambda path: path == bundled_python,
    )
    monkeypatch.setattr(
        zephyr_build,
        "_ensure_west_python",
        lambda _path: pytest.fail("offline local NCS must not run pip/bootstrap"),
    )
    monkeypatch.setattr(
        zephyr_build,
        "_install_managed_sdk",
        lambda **_kwargs: pytest.fail("offline local NCS must not download an SDK"),
    )
    monkeypatch.setattr(zephyr_build, "_workspace_requirements_satisfied", lambda *_args: True)
    args = Namespace(
        west_venv_dir=str(tmp_path / "private-venv"),
        managed_workspace_dir=str(tmp_path / "managed-workspace"),
        managed_sdk_dir=str(tmp_path / "managed-sdk"),
        workspace_dir=None,
        sdk_dir=None,
        board="nrf52840dk/nrf52840",
        toolchain="arm-zephyr-eabi",
        zephyr_repo="unused",
        zephyr_ref="unused",
        skip_workspace_bootstrap=False,
        skip_sdk_install=False,
    )

    runtime = zephyr_build.ensure_runtime(args)

    assert runtime.workspace_dir == workspace
    assert runtime.sdk_dir == sdk
    assert runtime.west_python == bundled_python


def test_external_workspace_and_standalone_sdk_fall_back_from_incomplete_vendor_python(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "external-workspace"
    zephyr_dir = workspace / "zephyr"
    (workspace / "modules" / "hal" / "nordic").mkdir(parents=True)
    board_dir = zephyr_dir / "boards" / "nordic" / "nrf52840dk"
    board_dir.mkdir(parents=True)
    (board_dir / "nrf52840dk_nrf52840_defconfig").write_text("", encoding="utf-8")
    (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
    (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    requirements = zephyr_dir / "scripts" / "requirements-base.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("example-package>=1\n", encoding="utf-8")
    sdk = tmp_path / "standalone-sdk"
    compiler = (
        sdk
        / "arm-zephyr-eabi"
        / "bin"
        / (
            "arm-zephyr-eabi-gcc.exe"
            if zephyr_build.sys.platform == "win32"
            else "arm-zephyr-eabi-gcc"
        )
    )
    compiler.parent.mkdir(parents=True)
    compiler.write_text("", encoding="utf-8")
    (sdk / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
    vendor_python = (
        sdk.parent / "bin" / ("python.exe" if zephyr_build.sys.platform == "win32" else "python")
    )
    vendor_python.parent.mkdir(parents=True)
    vendor_python.write_text("vendor-owned", encoding="utf-8")
    west_venv = tmp_path / "west-venv"
    west_python = zephyr_build._venv_python_path(west_venv)
    west_python.parent.mkdir(parents=True)
    suffix = ".exe" if zephyr_build.sys.platform == "win32" else ""
    for executable in (
        west_python,
        west_python.parent / f"cmake{suffix}",
        west_python.parent / f"ninja{suffix}",
    ):
        executable.write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(zephyr_build, "_python_has_module", lambda *_args: True)
    monkeypatch.setattr(zephyr_build, "_can_run_west", lambda _path: True)
    monkeypatch.setattr(
        zephyr_build, "_is_complete_build_python", lambda path: path == vendor_python
    )
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: True)
    monkeypatch.setattr(zephyr_build, "_run", lambda cmd, **_kwargs: calls.append(cmd))
    monkeypatch.setattr(
        zephyr_build,
        "_workspace_requirements_satisfied",
        lambda python, _workspace: python != vendor_python,
    )

    def fake_ensure(venv_dir: Path) -> Path:
        python = zephyr_build._venv_python_path(venv_dir)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("", encoding="utf-8")
        return python

    monkeypatch.setattr(zephyr_build, "_ensure_west_python", fake_ensure)
    args = Namespace(
        west_venv_dir=str(west_venv),
        managed_workspace_dir=str(tmp_path / "managed-workspace"),
        managed_sdk_dir=str(tmp_path / "managed-sdk"),
        workspace_dir=str(workspace),
        sdk_dir=str(sdk),
        board="nrf52840dk/nrf52840",
        toolchain="arm-zephyr-eabi",
        zephyr_repo="unused",
        zephyr_ref="unused",
        skip_workspace_bootstrap=False,
        skip_sdk_install=False,
    )

    first = zephyr_build.ensure_runtime(args)
    second = zephyr_build.ensure_runtime(args)

    expected_venv = zephyr_build._workspace_requirements_venv_dir(west_venv, workspace)
    assert first.west_python == zephyr_build._venv_python_path(expected_venv)
    assert second.west_python == zephyr_build._venv_python_path(expected_venv)
    assert vendor_python.read_text(encoding="utf-8") == "vendor-owned"
    installs = [cmd for cmd in calls if "pip" in cmd and "-r" in cmd]
    assert len(installs) == 1


def test_managed_workspace_resumes_after_update_failure_without_second_init(
    monkeypatch, tmp_path: Path
) -> None:
    managed = tmp_path / "managed-workspace"
    python = tmp_path / "python"
    calls: list[list[str]] = []
    fail_update_once = True

    def fake_run(cmd: list[str], **_kwargs) -> None:
        nonlocal fail_update_once
        calls.append(cmd)
        if "init" in cmd:
            config_path = managed / ".west" / "config"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("[manifest]\npath = manifest\n", encoding="utf-8")
        if "update" in cmd:
            if fail_update_once:
                fail_update_once = False
                raise RuntimeError("injected update failure")
            zephyr_dir = managed / "zephyr"
            zephyr_dir.mkdir(parents=True)
            (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
            (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
            (managed / "modules" / "hal" / "nordic").mkdir(parents=True)
            board_dir = zephyr_dir / "boards" / "nordic" / "nrf52840dk"
            board_dir.mkdir(parents=True)
            (board_dir / "nrf52840dk_nrf52840_defconfig").write_text("", encoding="utf-8")

    monkeypatch.setattr(zephyr_build, "_run", fake_run)
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(managed, "managed-cache")],
    )
    monkeypatch.setattr(zephyr_build, "_install_zephyr_python_requirements", lambda *_args: None)

    def resolve() -> tuple[Path, str]:
        return zephyr_build._resolve_workspace_dir(
            west_python=python,
            workspace_dir=None,
            managed_workspace_dir=managed,
            zephyr_repo="https://example.invalid/zephyr.git",
            zephyr_ref="v-test",
            board="nrf52840dk/nrf52840",
            skip_workspace_bootstrap=False,
        )

    with pytest.raises(RuntimeError, match="injected update failure"):
        resolve()

    resolved, source = resolve()

    init_calls = [cmd for cmd in calls if "init" in cmd]
    assert len(init_calls) == 1
    assert resolved == managed
    assert source == "managed-bootstrap"


def test_managed_workspace_does_not_promote_partial_tree_without_completion_record(
    monkeypatch, tmp_path: Path
) -> None:
    managed = tmp_path / "managed-workspace"
    update_calls = 0

    def create_partial_tree() -> None:
        zephyr_dir = managed / "zephyr"
        board_dir = zephyr_dir / "boards" / "nordic" / "nrf52840dk"
        board_dir.mkdir(parents=True, exist_ok=True)
        (board_dir / "nrf52840dk_nrf52840_defconfig").write_text("", encoding="utf-8")
        (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
        (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
        (managed / "modules" / "hal" / "nordic").mkdir(parents=True, exist_ok=True)

    def fake_run(cmd: list[str], **_kwargs) -> None:
        nonlocal update_calls
        if "init" in cmd:
            config_path = managed / ".west" / "config"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("[manifest]\npath = manifest\n", encoding="utf-8")
        if "update" in cmd:
            update_calls += 1
            create_partial_tree()
            if update_calls == 1:
                raise RuntimeError("interrupted after board and HAL appeared")

    monkeypatch.setattr(zephyr_build, "_run", fake_run)
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(managed, "managed-cache")],
    )

    def resolve() -> tuple[Path, str]:
        return zephyr_build._resolve_workspace_dir(
            west_python=tmp_path / "python",
            workspace_dir=None,
            managed_workspace_dir=managed,
            zephyr_repo="https://example.invalid/zephyr.git",
            zephyr_ref="v-test",
            board="nrf52840dk/nrf52840",
            skip_workspace_bootstrap=False,
        )

    with pytest.raises(RuntimeError, match="interrupted after board"):
        resolve()
    assert not (managed / zephyr_build.MANAGED_WORKSPACE_COMPLETE).exists()

    resolved, _source = resolve()

    assert resolved == managed
    assert update_calls == 2
    assert zephyr_build._managed_workspace_is_complete(
        managed,
        zephyr_repo="https://example.invalid/zephyr.git",
        zephyr_ref="v-test",
        board="nrf52840dk/nrf52840",
    )


def test_existing_second_board_is_validated_without_global_west_update(
    monkeypatch, tmp_path: Path
) -> None:
    managed = tmp_path / "managed-workspace"
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    config_path = managed / ".west" / "config"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[manifest]\npath = manifest\n", encoding="utf-8")
    zephyr_dir = managed / "zephyr"
    (zephyr_dir / "CMakeLists.txt").parent.mkdir(parents=True)
    (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
    (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    for board, qualifier in (
        ("nrf52840dk", "nrf52840"),
        ("nrf52833dk", "nrf52833"),
    ):
        board_dir = zephyr_dir / "boards" / "nordic" / board
        board_dir.mkdir(parents=True)
        (board_dir / f"{board}_{qualifier}_defconfig").write_text("", encoding="utf-8")
    (managed / "modules" / "hal" / "nordic").mkdir(parents=True)
    zephyr_build._write_managed_workspace_completion(
        managed,
        zephyr_repo=repo,
        zephyr_ref=ref,
        board="nrf52840dk/nrf52840",
    )
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(managed, "managed-cache")],
    )
    monkeypatch.setattr(
        zephyr_build,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("existing board must not rerun west update"),
    )

    resolved, source = zephyr_build._resolve_workspace_dir(
        west_python=tmp_path / "python",
        workspace_dir=None,
        managed_workspace_dir=managed,
        zephyr_repo=repo,
        zephyr_ref=ref,
        board="nrf52833dk/nrf52833",
        skip_workspace_bootstrap=False,
    )

    assert resolved == managed
    assert source == "managed-cache"
    assert zephyr_build._managed_workspace_is_complete(
        managed,
        zephyr_repo=repo,
        zephyr_ref=ref,
        board="nrf52833dk/nrf52833",
    )


def test_managed_workspace_identity_recovers_after_marker_promotion_failure(
    monkeypatch, tmp_path: Path
) -> None:
    managed = tmp_path / "managed-workspace"
    managed.mkdir()
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    original_replace = zephyr_build.os.replace
    fail_once = True

    def failing_replace(source: Path, destination: Path) -> None:
        nonlocal fail_once
        if destination == managed / zephyr_build.MANAGED_WORKSPACE_MARKER and fail_once:
            fail_once = False
            raise OSError("injected marker promotion failure")
        original_replace(source, destination)

    monkeypatch.setattr(zephyr_build.os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected marker"):
        zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)

    assert zephyr_build._managed_workspace_identity_can_resume(
        managed, zephyr_repo=repo, zephyr_ref=ref
    )
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    assert zephyr_build._managed_workspace_is_owned(managed, zephyr_repo=repo, zephyr_ref=ref)


def test_completion_promotion_never_rewrites_immutable_workspace_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed-workspace"
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    marker_path = managed / zephyr_build.MANAGED_WORKSPACE_MARKER
    marker_before = marker_path.read_bytes()
    original_write_text = Path.write_text

    def guarded_write_text(path: Path, *args, **kwargs):
        if path == marker_path:
            pytest.fail("completion must not rewrite the immutable owner marker")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)

    zephyr_build._write_managed_workspace_completion(
        managed,
        zephyr_repo=repo,
        zephyr_ref=ref,
        board="nrf52840dk/nrf52840",
    )

    assert marker_path.read_bytes() == marker_before


@pytest.mark.parametrize("configured_manifest", ["../manifest", "C:/manifest"])
def test_managed_workspace_rejects_manifest_path_outside_owned_root(
    monkeypatch, tmp_path: Path, configured_manifest: str
) -> None:
    managed = tmp_path / "managed-workspace"
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    config_path = managed / ".west" / "config"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(f"[manifest]\npath = {configured_manifest}\n", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(managed, "managed-cache")],
    )
    monkeypatch.setattr(
        zephyr_build,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("foreign workspace must not be updated"),
    )

    with pytest.raises(RuntimeError, match="not owned|manifest identity"):
        zephyr_build._resolve_workspace_dir(
            west_python=tmp_path / "python",
            workspace_dir=None,
            managed_workspace_dir=managed,
            zephyr_repo=repo,
            zephyr_ref=ref,
            board="nrf52840dk/nrf52840",
            skip_workspace_bootstrap=False,
        )


def test_managed_workspace_rejects_complete_same_name_foreign_cache(
    monkeypatch, tmp_path: Path
) -> None:
    managed = tmp_path / "managed-workspace"
    zephyr_dir = managed / "zephyr"
    (managed / "modules" / "hal" / "nordic").mkdir(parents=True)
    zephyr_dir.mkdir(parents=True)
    (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
    (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(managed, "managed-cache")],
    )

    with pytest.raises(RuntimeError, match="unowned or misdirected managed workspace"):
        zephyr_build._resolve_workspace_dir(
            west_python=tmp_path / "python",
            workspace_dir=None,
            managed_workspace_dir=managed,
            zephyr_repo="https://example.invalid/zephyr.git",
            zephyr_ref="v-test",
            board="nrf52840dk/nrf52840",
            skip_workspace_bootstrap=False,
        )


@pytest.mark.parametrize("config_value", [None, "../foreign"])
def test_complete_owned_managed_workspace_requires_exact_west_manifest_path(
    monkeypatch, tmp_path: Path, config_value: str | None
) -> None:
    managed = tmp_path / "managed-workspace"
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    zephyr_dir = managed / "zephyr"
    (managed / "modules" / "hal" / "nordic").mkdir(parents=True)
    zephyr_dir.mkdir()
    (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
    (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    if config_value is not None:
        config_path = managed / ".west" / "config"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(f"[manifest]\npath = {config_value}\n", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(managed, "managed-cache")],
    )

    with pytest.raises(RuntimeError, match="unowned or misdirected managed workspace"):
        zephyr_build._resolve_workspace_dir(
            west_python=tmp_path / "python",
            workspace_dir=None,
            managed_workspace_dir=managed,
            zephyr_repo=repo,
            zephyr_ref=ref,
            board="nrf52840dk/nrf52840",
            skip_workspace_bootstrap=False,
        )


def test_complete_managed_workspace_is_rechecked_under_cache_lock(
    monkeypatch, tmp_path: Path
) -> None:
    managed = tmp_path / "managed-workspace"
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    config_path = managed / ".west" / "config"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[manifest]\npath = manifest\n", encoding="utf-8")
    zephyr_dir = managed / "zephyr"
    board_dir = zephyr_dir / "boards" / "nordic" / "nrf52840dk"
    board_dir.mkdir(parents=True)
    (board_dir / "nrf52840dk_nrf52840_defconfig").write_text("", encoding="utf-8")
    (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
    (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    (managed / "modules" / "hal" / "nordic").mkdir(parents=True)
    zephyr_build._write_managed_workspace_completion(
        managed,
        zephyr_repo=repo,
        zephyr_ref=ref,
        board="nrf52840dk/nrf52840",
    )
    locks: list[Path] = []

    @contextmanager
    def fake_lock(resource: Path):
        locks.append(resource)
        yield

    monkeypatch.setattr(zephyr_build, "_cache_lock", fake_lock)
    monkeypatch.setattr(
        zephyr_build,
        "_iter_zephyr_workspace_candidates",
        lambda **_kwargs: [zephyr_build.CandidatePath(managed, "managed-cache")],
    )

    resolved, source = zephyr_build._resolve_workspace_dir(
        west_python=tmp_path / "python",
        workspace_dir=None,
        managed_workspace_dir=managed,
        zephyr_repo=repo,
        zephyr_ref=ref,
        board="nrf52840dk/nrf52840",
        skip_workspace_bootstrap=False,
    )

    assert resolved == managed
    assert source == "managed-cache"
    assert locks == [managed]


def test_managed_workspace_identity_rejects_manifest_symlink_escape(tmp_path: Path) -> None:
    managed = tmp_path / "managed-workspace"
    outside = tmp_path / "outside-manifest"
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    manifest_path = managed / "manifest" / "west.yml"
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.unlink()
    (managed / "manifest").rmdir()
    outside.mkdir()
    (outside / "west.yml").write_bytes(manifest_bytes)
    try:
        (managed / "manifest").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create directory symlink/junction: {exc}")

    assert not zephyr_build._managed_workspace_is_owned(managed, zephyr_repo=repo, zephyr_ref=ref)


def test_managed_workspace_identity_rejects_reported_manifest_junction(
    monkeypatch, tmp_path: Path
) -> None:
    managed = tmp_path / "managed-workspace"
    repo = "https://example.invalid/zephyr.git"
    ref = "v-test"
    zephyr_build._write_managed_workspace_identity(managed, zephyr_repo=repo, zephyr_ref=ref)
    original = getattr(Path, "is_junction", lambda _path: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path.name == "manifest" or original(path),
        raising=False,
    )

    assert not zephyr_build._managed_workspace_is_owned(managed, zephyr_repo=repo, zephyr_ref=ref)


@pytest.mark.parametrize("invalid_kind", ["workspace", "sdk"])
def test_explicit_invalid_runtime_path_fails_before_venv_or_network(
    monkeypatch, tmp_path: Path, invalid_kind: str
) -> None:
    workspace = tmp_path / "workspace"
    sdk = tmp_path / "sdk"
    if invalid_kind == "sdk":
        zephyr_dir = workspace / "zephyr"
        (workspace / "modules" / "hal" / "nordic").mkdir(parents=True)
        zephyr_dir.mkdir(parents=True)
        (zephyr_dir / "CMakeLists.txt").write_text("# zephyr\n", encoding="utf-8")
        (zephyr_dir / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "_ensure_west_python",
        lambda _path: pytest.fail("invalid explicit path must fail before venv/pip"),
    )
    args = Namespace(
        west_venv_dir=str(tmp_path / "private-venv"),
        managed_workspace_dir=str(tmp_path / "managed-workspace"),
        managed_sdk_dir=str(tmp_path / "managed-sdk"),
        workspace_dir=str(workspace),
        sdk_dir=str(sdk) if invalid_kind == "sdk" else None,
        board="nrf52840dk/nrf52840",
        toolchain="arm-zephyr-eabi",
        zephyr_repo="unused",
        zephyr_ref="unused",
        skip_workspace_bootstrap=False,
        skip_sdk_install=False,
    )

    with pytest.raises(RuntimeError, match="Explicit Zephyr"):
        zephyr_build.ensure_runtime(args)


def test_managed_sdk_promotion_rejects_nonexecuting_compiler(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")

    def fake_install(**kwargs) -> None:
        managed = kwargs["managed_sdk_dir"]
        managed.mkdir(parents=True)
        (managed / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
        compiler = (
            managed
            / "arm-zephyr-eabi"
            / "bin"
            / (
                "arm-zephyr-eabi-gcc.exe"
                if zephyr_build.sys.platform == "win32"
                else "arm-zephyr-eabi-gcc"
            )
        )
        compiler.parent.mkdir(parents=True)
        compiler.write_text("", encoding="utf-8")

    monkeypatch.setattr(zephyr_build, "_iter_sdk_candidates", lambda **_kwargs: [])
    monkeypatch.setattr(zephyr_build, "_install_managed_sdk", fake_install)
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: False)

    with pytest.raises(RuntimeError, match="Managed SDK install completed"):
        zephyr_build._resolve_sdk_dir(
            west_python=tmp_path / "python",
            workspace_dir=workspace,
            sdk_dir=None,
            managed_sdk_dir=tmp_path / "managed",
            toolchain="arm-zephyr-eabi",
            skip_sdk_install=False,
        )


def test_managed_sdk_refuses_unowned_destination_without_deleting_it(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    target = tmp_path / "managed" / "0.17.4"
    target.mkdir(parents=True)
    sentinel = target / "unrelated.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(zephyr_build, "_iter_sdk_candidates", lambda **_kwargs: [])

    with pytest.raises(RuntimeError, match="unowned managed SDK"):
        zephyr_build._resolve_sdk_dir(
            west_python=tmp_path / "python",
            workspace_dir=workspace,
            sdk_dir=None,
            managed_sdk_dir=tmp_path / "managed",
            toolchain="arm-zephyr-eabi",
            skip_sdk_install=False,
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_managed_sdk_staging_failure_preserves_previous_owned_cache(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    target = tmp_path / "managed" / "0.17.4"
    target.mkdir(parents=True)
    (target / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
    sentinel = target / "previous.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    zephyr_build._write_managed_sdk_identity(target, version="0.17.4", toolchain="arm-zephyr-eabi")
    monkeypatch.setattr(zephyr_build, "_expected_sdk_sha256", lambda *_args: "ok")
    monkeypatch.setattr(zephyr_build, "_sha256_file", lambda _path: "ok")
    monkeypatch.setattr(
        zephyr_build,
        "_download_file",
        lambda _url, destination: destination.write_bytes(b"archive"),
    )

    def fake_extract(_python: Path, _archive: Path, destination: Path) -> None:
        sdk_root = destination / "zephyr-sdk-0.17.4"
        sdk_root.mkdir(parents=True)
        (sdk_root / "sdk_version").write_text("0.17.4\n", encoding="utf-8")

    monkeypatch.setattr(zephyr_build, "_extract_sdk_archive", fake_extract)
    monkeypatch.setattr(
        zephyr_build,
        "_install_managed_toolchain",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("injected toolchain failure")),
    )

    with pytest.raises(RuntimeError, match="injected toolchain failure"):
        zephyr_build._install_managed_sdk(
            west_python=tmp_path / "python",
            workspace_dir=workspace,
            managed_sdk_dir=target,
            toolchain="arm-zephyr-eabi",
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert zephyr_build._managed_sdk_is_owned(target, version="0.17.4", toolchain="arm-zephyr-eabi")


def test_managed_sdk_promotes_only_complete_executable_staged_candidate(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "zephyr").mkdir(parents=True)
    (workspace / "zephyr" / "SDK_VERSION").write_text("0.17.4\n", encoding="utf-8")
    target = tmp_path / "managed" / "0.17.4"
    target.mkdir(parents=True)
    (target / "sdk_version").write_text("0.17.4\n", encoding="utf-8")
    (target / "previous.txt").write_text("old", encoding="utf-8")
    zephyr_build._write_managed_sdk_identity(target, version="0.17.4", toolchain="arm-zephyr-eabi")
    monkeypatch.setattr(zephyr_build, "_expected_sdk_sha256", lambda *_args: "ok")
    monkeypatch.setattr(zephyr_build, "_sha256_file", lambda _path: "ok")
    monkeypatch.setattr(
        zephyr_build,
        "_download_file",
        lambda _url, destination: destination.write_bytes(b"archive"),
    )

    def fake_extract(_python: Path, _archive: Path, destination: Path) -> None:
        sdk_root = destination / "zephyr-sdk-0.17.4"
        sdk_root.mkdir(parents=True)
        (sdk_root / "sdk_version").write_text("0.17.4\n", encoding="utf-8")

    def fake_toolchain(**kwargs) -> None:
        sdk_root = kwargs["sdk_dir"]
        compiler = (
            sdk_root
            / "arm-zephyr-eabi"
            / "bin"
            / (
                "arm-zephyr-eabi-gcc.exe"
                if zephyr_build.sys.platform == "win32"
                else "arm-zephyr-eabi-gcc"
            )
        )
        compiler.parent.mkdir(parents=True)
        compiler.write_text("", encoding="utf-8")

    monkeypatch.setattr(zephyr_build, "_extract_sdk_archive", fake_extract)
    monkeypatch.setattr(zephyr_build, "_install_managed_toolchain", fake_toolchain)
    monkeypatch.setattr(zephyr_build, "_sdk_toolchain_runs", lambda *_args: True)

    zephyr_build._install_managed_sdk(
        west_python=tmp_path / "python",
        workspace_dir=workspace,
        managed_sdk_dir=target,
        toolchain="arm-zephyr-eabi",
    )

    assert not (target / "previous.txt").exists()
    assert zephyr_build._managed_sdk_is_owned(target, version="0.17.4", toolchain="arm-zephyr-eabi")
    assert zephyr_build._sdk_has_toolchain(target, "arm-zephyr-eabi")


def test_managed_toolchain_install_uses_verified_python_extraction_not_setup_script(
    monkeypatch, tmp_path: Path
) -> None:
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    (sdk_dir / "sdk_toolchains").write_text("arm-zephyr-eabi\n", encoding="utf-8")
    west_python = tmp_path / "west-python.exe"
    downloads: list[tuple[str, Path]] = []
    extractions: list[tuple[Path, Path, Path]] = []
    monkeypatch.setattr(zephyr_build.platform, "system", lambda: "Windows")
    monkeypatch.setattr(zephyr_build.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(zephyr_build, "_expected_sdk_sha256", lambda *_args: "expected")
    monkeypatch.setattr(zephyr_build, "_sha256_file", lambda _path: "expected")

    def fake_download(url: str, destination: Path) -> None:
        downloads.append((url, destination))
        destination.write_bytes(b"archive")

    def fake_extract(python: Path, archive: Path, destination: Path) -> None:
        extractions.append((python, archive, destination))
        compiler = (
            destination
            / "arm-zephyr-eabi"
            / "bin"
            / (
                "arm-zephyr-eabi-gcc.exe"
                if zephyr_build.sys.platform == "win32"
                else "arm-zephyr-eabi-gcc"
            )
        )
        compiler.parent.mkdir(parents=True)
        compiler.write_text("", encoding="utf-8")

    monkeypatch.setattr(zephyr_build, "_download_file", fake_download)
    monkeypatch.setattr(zephyr_build, "_extract_sdk_archive", fake_extract)
    monkeypatch.setattr(
        zephyr_build,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("host setup script must not run"),
    )

    zephyr_build._install_managed_toolchain(
        west_python=west_python,
        sdk_dir=sdk_dir,
        version="0.17.4",
        toolchain="arm-zephyr-eabi",
    )

    assert downloads[0][0].endswith("/toolchain_windows-x86_64_arm-zephyr-eabi.7z")
    assert extractions[0][0] == west_python
    assert extractions[0][2] == sdk_dir


def test_scratch_copy_excludes_build_and_host_state(tmp_path: Path) -> None:
    app = tmp_path / "app with spaces"
    build = app / "build"
    destination = tmp_path / "scratch" / "app"
    (app / "src").mkdir(parents=True)
    build.mkdir()
    (app / ".firm").mkdir()
    (app / "acceptance").mkdir()
    (app / "src" / "main.c").write_text("source", encoding="utf-8")
    (build / "large-sentinel.bin").write_bytes(b"x" * 1024)
    (app / ".firm" / "state.json").write_text("state", encoding="utf-8")
    (app / "acceptance" / "evidence.json").write_text("evidence", encoding="utf-8")

    zephyr_build._copy_app_for_scratch(app.resolve(), build.resolve(), destination)

    assert (destination / "src" / "main.c").read_text(encoding="utf-8") == "source"
    assert not (destination / "build").exists()
    assert not (destination / ".firm").exists()
    assert not (destination / "acceptance").exists()


@pytest.mark.parametrize("build_relation", ["equal", "ancestor"])
def test_run_build_rejects_destructive_output_relationship_before_any_work(
    monkeypatch, tmp_path: Path, build_relation: str
) -> None:
    root = tmp_path / "workspace with spaces"
    app = root / "app"
    app.mkdir(parents=True)
    sentinel = app / "main.c"
    sentinel.write_text("must survive", encoding="utf-8")
    build = app if build_relation == "equal" else root
    runtime = zephyr_build.ZephyrRuntime(
        workspace_dir=tmp_path / "zephyr-workspace",
        workspace_source="test",
        sdk_dir=tmp_path / "sdk",
        sdk_source="test",
        west_python=tmp_path / "python",
        managed_workspace_dir=tmp_path / "managed",
    )
    args = Namespace(
        app_dir=str(app),
        build_dir=str(build),
        board="nrf52840dk/nrf52840",
    )

    with pytest.raises(RuntimeError, match="must not equal or contain"):
        zephyr_build.run_build(args, runtime)

    assert sentinel.read_text(encoding="utf-8") == "must survive"


def test_cli_rejects_destructive_build_path_before_runtime_bootstrap(
    monkeypatch, tmp_path: Path
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    sentinel = app / "main.c"
    sentinel.write_text("source", encoding="utf-8")
    monkeypatch.setattr(
        zephyr_build,
        "ensure_runtime",
        lambda _args: pytest.fail("runtime bootstrap ran before path validation"),
    )
    monkeypatch.setattr(
        zephyr_build.sys,
        "argv",
        [
            "pyocd-zephyr-build",
            "--app-dir",
            str(app),
            "--build-dir",
            str(app),
            "--board",
            "nrf52840dk/nrf52840",
        ],
    )

    with pytest.raises(RuntimeError, match="must not equal or contain"):
        zephyr_build.main()

    assert sentinel.read_text(encoding="utf-8") == "source"


@pytest.mark.parametrize("invalid_kind", ["missing-app", "foreign-output", "linked-output"])
def test_cli_preflights_invalid_build_request_before_runtime_provisioning(
    monkeypatch, tmp_path: Path, invalid_kind: str
) -> None:
    app = tmp_path / "app"
    if invalid_kind != "missing-app":
        app.mkdir()
    build = tmp_path / "build"
    if invalid_kind in {"foreign-output", "linked-output"}:
        build.mkdir()
    if invalid_kind == "foreign-output":
        (build / "customer-data.bin").write_bytes(b"preserve")
    if invalid_kind == "linked-output":
        original_link_check = zephyr_build._path_is_link_or_junction
        monkeypatch.setattr(
            zephyr_build,
            "_path_is_link_or_junction",
            lambda path: path == build or original_link_check(path),
        )
    monkeypatch.setattr(
        zephyr_build,
        "ensure_runtime",
        lambda _args: pytest.fail("invalid request must not provision or access network"),
    )
    monkeypatch.setattr(
        zephyr_build.sys,
        "argv",
        [
            "pyocd-zephyr-build",
            "--app-dir",
            str(app),
            "--build-dir",
            str(build),
            "--board",
            "nrf52840dk/nrf52840",
        ],
    )

    with pytest.raises(RuntimeError):
        zephyr_build.main()
