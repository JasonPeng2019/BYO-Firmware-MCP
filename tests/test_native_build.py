from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from pyocd_debug_mcp import native_build


def _elf_bytes(elf_type: int = 2, payload: bytes = b"") -> bytes:
    return b"\x7fELF\x01\x01\x01" + (b"\x00" * 9) + elf_type.to_bytes(2, "little") + payload


def _write_toolchain_environment(root: Path) -> Path:
    metadata = root / "toolchains" / "toolchain-a" / "environment.json"
    metadata.parent.mkdir(parents=True)
    (metadata.parent / "bin").mkdir()
    metadata.write_text(
        json.dumps(
            {
                "env_vars": [
                    {
                        "type": "relative_paths",
                        "key": "PATH",
                        "values": ["bin"],
                        "existing_value_treatment": "prepend_to",
                    },
                    {
                        "type": "string",
                        "key": "ZEPHYR_TOOLCHAIN_VARIANT",
                        "value": "zephyr",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return metadata


def test_local_environment_discovery_never_provisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ncs" / "v3.3.1"
    (workspace / ".west").mkdir(parents=True)
    (workspace / ".west" / "config").write_text("[manifest]", encoding="utf-8")
    (workspace / "zephyr").mkdir()
    metadata = _write_toolchain_environment(tmp_path / "ncs")
    west = metadata.parent / "bin" / ("west.exe" if native_build.os.name == "nt" else "west")
    west.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        native_build, "_candidate_install_roots", lambda _env=None: (tmp_path / "ncs",)
    )
    monkeypatch.setattr(native_build.shutil, "which", lambda *_args, **_kwargs: str(west))

    selected = native_build.discover_local_environment(environ={"PATH": "original"})

    assert selected.workspace_dir == workspace
    assert selected.toolchain_env == metadata
    assert selected.provider == "zephyr-west"
    assert selected.environment["ZEPHYR_TOOLCHAIN_VARIANT"] == "zephyr"


def test_run_build_executes_one_native_command_and_reports_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    (project / "prj.conf").write_text("CONFIG_GPIO=y\n", encoding="utf-8")
    (project / "CMakeLists.txt").write_text("find_package(Zephyr REQUIRED)\n", encoding="utf-8")
    build = tmp_path / "build"
    (tmp_path / "ncs").mkdir()
    environment = native_build.LocalBuildEnvironment(
        provider="zephyr-west",
        workspace_dir=tmp_path / "ncs",
        toolchain_env=tmp_path / "environment.json",
        executable=tmp_path / "west",
        environment={"PATH": "local-only"},
    )
    monkeypatch.setattr(
        native_build, "discover_local_environment", lambda **_kwargs: environment
    )
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
        timeout: float,
    ) -> object:
        calls.append((argv, cwd, env))
        assert timeout == native_build.BUILD_TIMEOUT_SECONDS
        (build / "zephyr").mkdir(parents=True)
        (build / "zephyr" / "zephyr.elf").write_bytes(_elf_bytes())
        (build / "zephyr" / "zephyr.map").write_text("map", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    result = native_build.run_build(
        Namespace(project_dir=str(project), build_dir=str(build), target="board/soc")
    )

    evidence = json.loads(capsys.readouterr().out)
    assert result == 0
    assert len(calls) == 1
    assert calls[0][0][1:4] == ["build", "--board", "board/soc"]
    assert evidence["offline_guards"] is False
    assert evidence["network_policy"] == "inherited"
    assert evidence["helper_provisioning"] is False
    assert evidence["artifacts"]["elf"].endswith("zephyr.elf")


def test_build_rejects_nonempty_output_before_environment_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    (build / "keep.txt").write_text("preserve", encoding="utf-8")
    called = False

    def discover() -> native_build.LocalBuildEnvironment:
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(native_build, "discover_local_environment", discover)

    with pytest.raises(RuntimeError, match="new or empty"):
        native_build.run_build(
            Namespace(project_dir=str(project), build_dir=str(build), target="board")
        )
    assert called is False
    assert (build / "keep.txt").read_text(encoding="utf-8") == "preserve"


def test_command_template_is_general_and_parameterized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_build,
        "discover_local_environment",
        lambda **_kwargs: pytest.fail("guidance must not preselect a named environment"),
    )
    template = native_build.command_template()
    argv = template["argv_template"]
    assert isinstance(argv, list)
    assert argv[1:3] == ["-m", "pyocd_debug_mcp.native_build"]
    assert "zephyr_build" not in " ".join(argv)
    assert template["offline_guards"] is False
    assert template["network_policy"] == "inherited_by_default"
    assert template["provider_selection"] == "agent_argv_or_optional_detection"
    assert "<build-executable>" in argv
    assert "'<project-dir>'" in str(template["powershell_template"])
    assert template["helper_provisioning"] is False
    assert template["resolved_local_environment"] == {
        "status": "not_selected",
        "reason": "Resolve the project's real build command before choosing an environment.",
    }
    assert template["optional_convenience_providers"] == ["zephyr-west", "gnu-make"]


def test_command_template_does_not_probe_missing_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**_kwargs: object) -> native_build.LocalBuildEnvironment:
        raise RuntimeError("no complete local install")

    monkeypatch.setattr(native_build, "discover_local_environment", unavailable)

    template = native_build.command_template()

    selected = template["resolved_local_environment"]
    assert isinstance(selected, dict)
    assert selected["status"] == "not_selected"


def test_global_west_fallback_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ncs" / "v3.3.1"
    (workspace / ".west").mkdir(parents=True)
    (workspace / ".west" / "config").write_text("[manifest]", encoding="utf-8")
    (workspace / "zephyr").mkdir()
    _write_toolchain_environment(tmp_path / "ncs")
    global_west = tmp_path / "global" / "west"
    global_west.parent.mkdir()
    global_west.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        native_build, "_candidate_install_roots", lambda _env=None: (tmp_path / "ncs",)
    )
    monkeypatch.setattr(native_build.shutil, "which", lambda *_args, **_kwargs: str(global_west))

    with pytest.raises(RuntimeError, match="global PATH fallback"):
        native_build.discover_local_environment(environ={"PATH": "global"})


def test_sysbuild_artifacts_follow_default_domain(tmp_path: Path) -> None:
    build = tmp_path / "build"
    app = build / "application" / "zephyr"
    app.mkdir(parents=True)
    (app / "zephyr.elf").write_bytes(b"elf")
    (app / "zephyr.hex").write_text("hex", encoding="utf-8")
    (app / "zephyr.map").write_text("map", encoding="utf-8")
    (build / "domains.yaml").write_text(
        "default: app\ndomains:\n  - name: app\n    build_dir: application\n",
        encoding="utf-8",
    )

    artifacts = native_build._artifact_paths(build)

    assert artifacts["elf"] == str((app / "zephyr.elf").resolve())
    assert artifacts["hex"] == str((app / "zephyr.hex").resolve())
    assert artifacts["map"] == str((app / "zephyr.map").resolve())


def test_offline_environment_overrides_common_network_clients() -> None:
    environment = native_build._offline_environment(
        {"HTTP_PROXY": "http://real-proxy", "PIP_NO_INDEX": "0"}
    )

    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["CARGO_NET_OFFLINE"] == "true"
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert environment["http_proxy"] == "http://127.0.0.1:9"
    assert environment["GIT_CONFIG_KEY_0"] == "http.proxy"


def test_explicit_install_root_excludes_default_candidates(tmp_path: Path) -> None:
    selected = tmp_path / "selected-ncs"

    assert native_build._candidate_install_roots(
        {"NCS_INSTALL_ROOT": str(selected)}
    ) == (selected.resolve(),)


def test_posix_defaults_never_reinterpret_windows_path() -> None:
    defaults = native_build._default_install_root_values("posix")

    assert defaults == ("~/ncs", "/opt/ncs")
    assert "C:/ncs" not in defaults


def test_make_provider_uses_fresh_build_variable_and_reports_generic_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "bare-metal"
    project.mkdir()
    (project / "Makefile").write_text("all:\n\t@echo build\n", encoding="utf-8")
    build = tmp_path / "out"
    make = tmp_path / "tools" / "make.exe"
    make.parent.mkdir()
    make.write_bytes(b"tool")
    gcc = make.parent / "arm-none-eabi-gcc.exe"
    gcc.write_bytes(b"tool")
    environment = native_build.LocalBuildEnvironment(
        provider="gnu-make",
        workspace_dir=make.parent,
        toolchain_env=gcc,
        executable=make,
        environment={"PATH": str(make.parent)},
    )
    monkeypatch.setattr(
        native_build,
        "discover_local_environment",
        lambda **kwargs: environment
        if kwargs.get("provider") == "gnu-make"
        else pytest.fail("wrong provider"),
    )
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
        timeout: float,
    ) -> object:
        del check, timeout
        calls.append((argv, cwd, env))
        build.mkdir(parents=True, exist_ok=True)
        (build / "firmware.elf").write_bytes(_elf_bytes(payload=b"firmware"))
        (build / "firmware.map").write_text("map", encoding="utf-8")
        (build / "firmware.hex").write_text(":00000001FF\n", encoding="ascii")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    result = native_build.run_build(
        Namespace(project_dir=str(project), build_dir=str(build), target="firmware")
    )

    evidence = json.loads(capsys.readouterr().out)
    assert result == 0
    assert calls[0][0] == [
        str(make),
        "-C",
        str(project.resolve()),
        f"BUILD_DIR={build.resolve()}",
        "firmware",
    ]
    assert calls[0][1] == project.resolve()
    assert evidence["provider"] == "gnu-make"
    assert evidence["artifacts"]["elf"] == str((build / "firmware.elf").resolve())
    assert evidence["artifacts"]["map"] == str((build / "firmware.map").resolve())
    assert evidence["helper_provisioning"] is False


def test_make_environment_prefers_explicit_tools_and_prepends_arm_gcc(
    tmp_path: Path,
) -> None:
    make = tmp_path / "make" / "make.exe"
    gcc = tmp_path / "gcc" / "arm-none-eabi-gcc.exe"
    make.parent.mkdir()
    gcc.parent.mkdir()
    make.write_bytes(b"tool")
    gcc.write_bytes(b"tool")

    selected = native_build.discover_local_environment(
        provider="gnu-make",
        environ={
            "NATIVE_MAKE": str(make),
            "ARM_GCC": str(gcc),
            "PATH": "original",
        },
    )

    assert selected.provider == "gnu-make"
    assert selected.executable == make.resolve()
    assert selected.toolchain_env == gcc.resolve()
    assert selected.environment["PATH"].split(native_build.os.pathsep)[:2] == [
        str(gcc.parent.resolve()),
        str(make.parent.resolve()),
    ]


def test_make_artifact_discovery_rejects_ambiguity(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    for name in ("one.elf", "two.elf"):
        (build / name).write_bytes(_elf_bytes())

    with pytest.raises(RuntimeError, match="exactly one ELF"):
        native_build._artifact_paths(build, "gnu-make")


def test_failed_make_reports_child_exit_without_guessing_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "Makefile").write_text("firmware:\n\t@false\n", encoding="utf-8")
    build = tmp_path / "build"
    environment = native_build.LocalBuildEnvironment(
        provider="gnu-make",
        workspace_dir=tmp_path,
        toolchain_env=None,
        executable=tmp_path / "make",
        environment={},
    )
    monkeypatch.setattr(
        native_build, "discover_local_environment", lambda **_kwargs: environment
    )
    monkeypatch.setattr(
        native_build, "run_owned", lambda *_args, **_kwargs: Namespace(returncode=2)
    )

    result = native_build.run_build(
        Namespace(project_dir=str(project), build_dir=str(build), target="firmware")
    )

    evidence = json.loads(capsys.readouterr().out)
    assert result == 2
    assert evidence["exit_code"] == 2
    assert evidence["artifacts"] == {"elf": None, "hex": None, "map": None}


@pytest.mark.parametrize("target", ["-f", "NAME=value", "../outside"])
def test_native_target_rejects_make_option_or_variable_injection(target: str) -> None:
    with pytest.raises(RuntimeError, match="project-native target"):
        native_build._validate_target(target)


def test_make_artifacts_reject_extra_unrelated_map(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "firmware.elf").write_bytes(_elf_bytes())
    (build / "firmware.map").write_text("map", encoding="utf-8")
    (build / "stale.map").write_text("stale", encoding="utf-8")

    with pytest.raises(RuntimeError, match="exactly one linker map"):
        native_build._artifact_paths(build, "gnu-make")


def test_arm_gcc_root_resolves_conventional_bin_directory(tmp_path: Path) -> None:
    root = tmp_path / "toolchain"
    compiler = root / "bin" / (
        "arm-none-eabi-gcc.exe" if native_build.os.name == "nt" else "arm-none-eabi-gcc"
    )
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"")
    compiler.chmod(0o755)

    assert native_build._explicit_tool_root(
        {"ARM_GCC_ROOT": str(root)}, "ARM_GCC_ROOT", "arm-none-eabi-gcc", "Arm GCC"
    ) == compiler.resolve()


@pytest.mark.parametrize(
    "relative_base",
    [
        Path("STM32CubeIDE_1.18.1") / "STM32CubeIDE",
        Path("STM32CubeIDE.app") / "Contents" / "Eclipse",
        Path("stm32cubeide_1.18.1"),
    ],
)
def test_vendor_discovery_supports_windows_macos_and_linux_layouts(
    tmp_path: Path, relative_base: Path
) -> None:
    executable = (
        tmp_path
        / relative_base
        / "plugins"
        / "com.st.stm32cube.ide.mcu.externaltools.make.1"
        / "tools"
        / "bin"
        / ("make.exe" if native_build.os.name == "nt" else "make")
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    executable.chmod(0o755)

    matches = native_build._bounded_vendor_tools(
        (tmp_path,),
        (
            "plugins/com.st.stm32cube.ide.mcu.externaltools.make.*/tools/bin/make",
            "plugins/com.st.stm32cube.ide.mcu.externaltools.make.*/tools/bin/make.exe",
        ),
    )

    assert matches == (executable.resolve(),)


def test_agent_command_supports_unknown_provider_cwd_env_and_declared_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "platformio-app"
    project.mkdir()
    (project / "platformio.ini").write_text("[env:board]\n", encoding="utf-8")
    working = project / "scripts"
    working.mkdir()
    build = tmp_path / "out"
    monkeypatch.setenv("HTTPS_PROXY", "http://real-proxy")
    monkeypatch.setattr(
        native_build,
        "detect_provider",
        lambda _project: pytest.fail("explicit argv must bypass provider detection"),
    )
    monkeypatch.setattr(
        native_build,
        "discover_local_environment",
        lambda **_kwargs: pytest.fail("explicit argv must bypass environment discovery"),
    )
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
        timeout: float,
    ) -> object:
        del check, timeout
        calls.append((argv, cwd, env))
        build.mkdir(parents=True, exist_ok=True)
        (build / "firmware.axf").write_bytes(_elf_bytes(payload=b"payload"))
        (build / "linker-output.txt").write_text("map", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    result = native_build.run_build(
        Namespace(
            project_dir=str(project),
            build_dir=str(build),
            target=None,
            cwd=str(working),
            env=["BOARD=novel-part"],
            offline=False,
            artifact_elf="firmware.axf",
            artifact_map="linker-output.txt",
            artifact_hex=None,
            command=["--", "platformio", "run", "--environment", "board"],
        )
    )

    evidence = json.loads(capsys.readouterr().out)
    assert result == 0
    assert calls[0][0] == ["platformio", "run", "--environment", "board"]
    assert calls[0][1] == working.resolve()
    assert calls[0][2]["BOARD"] == "novel-part"
    assert calls[0][2]["HTTPS_PROXY"] == "http://real-proxy"
    assert evidence["provider"] == "agent-command"
    assert evidence["provider_selection"] == "agent-supplied-argv"
    assert evidence["cwd"] == str(working.resolve())
    assert evidence["environment_overrides"] == ["BOARD"]
    assert evidence["artifacts"] == {
        "elf": str((build / "firmware.axf").resolve()),
        "hex": None,
        "map": str((build / "linker-output.txt").resolve()),
    }
    assert evidence["artifact_assurance"]["elf"] == "loadable-elf-header-verified"
    assert evidence["artifact_assurance"]["map"] == "agent-declared-existing"


def test_agent_command_discovers_extension_independent_elf(tmp_path: Path) -> None:
    build = tmp_path / "out"
    build.mkdir()
    (build / "firmware.out").write_bytes(_elf_bytes(payload=b"payload"))
    (build / "firmware.map").write_text("map", encoding="utf-8")

    artifacts = native_build._artifact_paths(build, "agent-command")

    assert artifacts["elf"] == str((build / "firmware.out").resolve())
    assert artifacts["map"] == str((build / "firmware.map").resolve())


def test_discovery_ignores_relocatable_object_elf_files(tmp_path: Path) -> None:
    build = tmp_path / "out"
    build.mkdir()
    (build / "main.o").write_bytes(_elf_bytes(elf_type=1))
    (build / "firmware.axf").write_bytes(_elf_bytes())
    (build / "firmware.map").write_text("map", encoding="utf-8")

    artifacts = native_build._artifact_paths(build, "agent-command")

    assert artifacts["elf"] == str((build / "firmware.axf").resolve())


def test_discovery_never_reuses_elf_bytes_as_linker_map(tmp_path: Path) -> None:
    build = tmp_path / "out"
    build.mkdir()
    (build / "firmware.map").write_bytes(_elf_bytes(payload=b"payload"))

    with pytest.raises(RuntimeError, match="exactly one linker map"):
        native_build._artifact_paths(build, "agent-command")


def test_unknown_project_without_command_teaches_universal_recovery(tmp_path: Path) -> None:
    project = tmp_path / "unknown"
    project.mkdir()
    build = tmp_path / "out"

    with pytest.raises(RuntimeError, match="supply its exact argv after '--'"):
        native_build.run_build(
            Namespace(project_dir=str(project), build_dir=str(build), target=None)
        )


def test_offline_mode_is_explicit_and_preserves_agent_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "out"
    seen_environment: dict[str, str] = {}

    def fake_run(
        _argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
        timeout: float,
    ) -> object:
        del cwd, check, timeout
        seen_environment.update(env)
        build.mkdir(parents=True, exist_ok=True)
        (build / "app.elf").write_bytes(_elf_bytes())
        (build / "app.map").write_text("map", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)
    result = native_build.run_build(
        Namespace(
            project_dir=str(project),
            build_dir=str(build),
            target=None,
            offline=True,
            command=["--", "custom-builder"],
        )
    )

    evidence = json.loads(capsys.readouterr().out)
    assert result == 0
    assert seen_environment["PIP_NO_INDEX"] == "1"
    assert seen_environment["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert evidence["offline_guards"] is True
    assert evidence["network_policy"] == "best_effort_offline_guards"


@pytest.mark.parametrize("content", [":0000009967\n:00000001FF\n", ":00000101FE\n"])
def test_intel_hex_rejects_unknown_record_type_or_nonzero_eof_address(
    tmp_path: Path, content: str
) -> None:
    output = tmp_path / "firmware.hex"
    output.write_text(content, encoding="ascii")

    assert native_build._is_intel_hex(output) is False


def test_agent_command_accepts_existing_in_source_build_and_external_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    (project / "existing-object.o").write_bytes(b"old")
    outputs = tmp_path / "vendor-fixed-output"
    elf = outputs / "firmware.axf"
    linker_map = outputs / "firmware.map"

    def fake_run(*_args: object, **_kwargs: object) -> object:
        outputs.mkdir()
        elf.write_bytes(_elf_bytes())
        linker_map.write_text("map", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    result = native_build.run_build(
        Namespace(
            project_dir=str(project),
            build_dir=str(project),
            target=None,
            artifact_elf=str(elf),
            artifact_map=str(linker_map),
            command=["--", "vendor-ide-cli", "--incremental-build"],
        )
    )

    evidence = json.loads(capsys.readouterr().out)
    assert result == 0
    assert evidence["artifacts"]["elf"] == str(elf.resolve())
    assert evidence["artifacts"]["map"] == str(linker_map.resolve())
    assert (project / "existing-object.o").read_bytes() == b"old"


def test_parser_accepts_literal_command_without_target() -> None:
    args = native_build.build_parser().parse_args(
        [
            "--project-dir",
            "project",
            "--build-dir",
            "build",
            "--",
            "cmake",
            "--build",
            "build",
        ]
    )

    assert args.target is None
    assert args.command == ["--", "cmake", "--build", "build"]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError("missing"), "Cannot start build executable"),
        (
            native_build.subprocess.TimeoutExpired(["builder"], 1),
            "Build command exceeded",
        ),
    ],
)
def test_agent_command_reports_process_start_and_timeout_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    message: str,
) -> None:
    project = tmp_path / "app"
    project.mkdir()

    def fail(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(native_build, "run_owned", fail)

    with pytest.raises(RuntimeError, match=message):
        native_build.run_build(
            Namespace(
                project_dir=str(project),
                build_dir=str(tmp_path / "out"),
                target=None,
                command=["--", "builder"],
            )
        )


def test_successful_child_with_ambiguous_outputs_reports_execution_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "out"

    def fake_run(*_args: object, **_kwargs: object) -> object:
        build.mkdir(exist_ok=True)
        (build / "one.axf").write_bytes(_elf_bytes())
        (build / "two.out").write_bytes(_elf_bytes())
        (build / "firmware.map").write_text("map", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)
    monkeypatch.setattr(
        native_build.sys,
        "argv",
        [
            "native_build",
            "--project-dir",
            str(project),
            "--build-dir",
            str(build),
            "--",
            "custom-builder",
            "build",
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        native_build.main()

    evidence = json.loads(capsys.readouterr().err)
    assert stopped.value.code == 2
    assert evidence["argv"] == ["custom-builder", "build"]
    assert evidence["cwd"] == str(project.resolve())
    assert evidence["exit_code"] == 0
    assert evidence["artifacts"] == {"elf": None, "hex": None, "map": None}
    assert "exactly one ELF" in evidence["artifact_validation_error"]
    assert evidence["error"] == evidence["artifact_validation_error"]


def test_declared_artifact_roles_must_be_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "out"

    def fake_run(*_args: object, **_kwargs: object) -> object:
        build.mkdir(exist_ok=True)
        (build / "firmware.axf").write_bytes(_elf_bytes())
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    with pytest.raises(native_build.BuildEvidenceError, match="must be different files"):
        native_build.run_build(
            Namespace(
                project_dir=str(project),
                build_dir=str(build),
                target=None,
                artifact_elf="firmware.axf",
                artifact_map="firmware.axf",
                command=["--", "builder"],
            )
        )


def test_declared_hex_must_be_checksum_valid_intel_hex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "out"

    def fake_run(*_args: object, **_kwargs: object) -> object:
        build.mkdir(exist_ok=True)
        (build / "firmware.elf").write_bytes(_elf_bytes())
        (build / "firmware.map").write_text("map", encoding="utf-8")
        (build / "firmware.hex").write_text("not Intel HEX", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    with pytest.raises(native_build.BuildEvidenceError, match="not valid Intel HEX"):
        native_build.run_build(
            Namespace(
                project_dir=str(project),
                build_dir=str(build),
                target=None,
                artifact_elf="firmware.elf",
                artifact_map="firmware.map",
                artifact_hex="firmware.hex",
                command=["--", "builder"],
            )
        )


def test_agent_command_can_use_home_as_explicit_artifact_search_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    elf = tmp_path / "firmware.elf"
    linker_map = tmp_path / "firmware.map"

    def fake_run(*_args: object, **_kwargs: object) -> object:
        elf.write_bytes(_elf_bytes())
        linker_map.write_text("map", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    result = native_build.run_build(
        Namespace(
            project_dir=str(project),
            build_dir=str(Path.home()),
            target=None,
            artifact_elf=str(elf),
            artifact_map=str(linker_map),
            command=["--", "builder"],
        )
    )

    assert result == 0
