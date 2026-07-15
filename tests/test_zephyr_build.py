from __future__ import annotations

from pathlib import Path

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

    assert candidates[0].path == managed.resolve()
    assert any(candidate.path == toolchain_sdk.resolve() for candidate in candidates)


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

    monkeypatch.setattr(zephyr_build, "_install_managed_sdk", fake_install)

    resolved_dir, source = zephyr_build._resolve_sdk_dir(
        west_python=tmp_path / "west-python.exe",
        workspace_dir=workspace,
        sdk_dir=None,
        managed_sdk_dir=managed,
        toolchain="arm-zephyr-eabi",
        skip_sdk_install=False,
    )

    assert resolved_dir == managed.resolve()
    assert source == "managed-install"
    assert captured["workspace_dir"] == workspace.resolve()
    assert captured["managed_sdk_dir"] == managed.resolve()
    assert captured["toolchain"] == "arm-zephyr-eabi"


def test_clean_build_dir_preserves_gitkeep_and_gitignore(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / ".gitkeep").write_text("", encoding="utf-8")
    (build_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    (build_dir / "firmware.elf").write_text("old", encoding="utf-8")
    (build_dir / "temp").mkdir()
    (build_dir / "temp" / "nested.txt").write_text("nested", encoding="utf-8")

    zephyr_build._clean_build_dir(build_dir)

    assert (build_dir / ".gitkeep").exists()
    assert (build_dir / ".gitignore").exists()
    assert not (build_dir / "firmware.elf").exists()
    assert not (build_dir / "temp").exists()


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


def test_copy_artifacts_preserves_live_build_tree_when_output_dir_matches_work_dir(
    tmp_path: Path,
) -> None:
    build_dir = tmp_path / "build"
    zephyr_dir = build_dir / "zephyr"
    zephyr_dir.mkdir(parents=True)
    elf_contents = "elf"
    hex_contents = "hex"
    (zephyr_dir / "zephyr.elf").write_text(elf_contents, encoding="utf-8")
    (zephyr_dir / "zephyr.hex").write_text(hex_contents, encoding="utf-8")
    (build_dir / "build.ninja").write_text("ninja", encoding="utf-8")

    zephyr_build._copy_artifacts(build_dir, build_dir, app_dir_name="app")

    assert (build_dir / "build.ninja").read_text(encoding="utf-8") == "ninja"
    assert (zephyr_dir / "zephyr.elf").read_text(encoding="utf-8") == elf_contents
    assert (build_dir / "firmware.elf").read_text(encoding="utf-8") == elf_contents
    assert (build_dir / "firmware.hex").read_text(encoding="utf-8") == hex_contents


def test_copy_artifacts_resolves_sysbuild_app_subdir(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    app_zephyr_dir = build_dir / "src" / "zephyr"
    app_zephyr_dir.mkdir(parents=True)
    (app_zephyr_dir / "zephyr.elf").write_text("elf", encoding="utf-8")
    (app_zephyr_dir / "zephyr.hex").write_text("hex", encoding="utf-8")

    zephyr_build._copy_artifacts(build_dir, build_dir, app_dir_name="src")

    assert (build_dir / "firmware.elf").read_text(encoding="utf-8") == "elf"
    assert (build_dir / "firmware.hex").read_text(encoding="utf-8") == "hex"


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
