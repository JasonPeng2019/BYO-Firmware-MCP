from __future__ import annotations

import json
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from pyocd_debug_mcp import native_build


def _elf_bytes(elf_type: int = 2, payload: bytes = b"") -> bytes:
    header = bytearray(52)
    header[:7] = b"\x7fELF\x01\x01\x01"
    header[16:18] = elf_type.to_bytes(2, "little")
    header[18:20] = (40).to_bytes(2, "little")  # EM_ARM
    header[20:24] = (1).to_bytes(4, "little")
    header[28:32] = (52).to_bytes(4, "little")
    header[40:42] = (52).to_bytes(2, "little")
    header[42:44] = (32).to_bytes(2, "little")
    header[44:46] = (1).to_bytes(2, "little")
    program_header = bytearray(32)
    program_header[:4] = (1).to_bytes(4, "little")  # PT_LOAD
    return bytes(header + program_header) + payload


def test_offline_environment_overrides_common_network_clients() -> None:
    environment = native_build._offline_environment(
        {"HTTP_PROXY": "http://real-proxy", "PIP_NO_INDEX": "0"}
    )

    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["CARGO_NET_OFFLINE"] == "true"
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:9"
    if native_build.os.name == "nt":
        assert "http_proxy" not in environment
    else:
        assert environment["http_proxy"] == "http://127.0.0.1:9"
    assert environment["GIT_CONFIG_KEY_0"] == "http.proxy"


def test_agent_command_always_rejects_filesystem_root_build_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="non-root"):
        native_build._validate_paths(
            str(tmp_path), Path(tmp_path.anchor).as_posix(), require_fresh_build=False
        )


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
    assert "provider" not in evidence
    assert "provider_selection" not in evidence
    assert evidence["cwd"] == str(working.resolve())
    assert evidence["environment_overrides"] == ["BOARD"]
    assert evidence["artifacts"] == {
        "elf": str((build / "firmware.axf").resolve()),
        "hex": None,
        "map": str((build / "linker-output.txt").resolve()),
    }
    assert evidence["artifact_assurance"]["elf"] == "loadable-elf-structure-verified"
    assert evidence["artifact_assurance"]["map"] == "agent-declared-existing"


def test_agent_command_preserves_empty_non_executable_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "out"
    seen: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        seen.extend(argv)
        (build / "firmware.elf").write_bytes(_elf_bytes())
        (build / "firmware.map").write_text("map", encoding="utf-8")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    assert (
        native_build.run_build(
            Namespace(
                project_dir=str(project),
                build_dir=str(build),
                command=["--", "builder", "", "value"],
            )
        )
        == 0
    )
    assert seen == ["builder", "", "value"]


def test_agent_command_discovers_extension_independent_elf(tmp_path: Path) -> None:
    build = tmp_path / "out"
    build.mkdir()
    (build / "firmware.out").write_bytes(_elf_bytes(payload=b"payload"))
    (build / "firmware.map").write_text("map", encoding="utf-8")

    artifacts = native_build._artifact_paths(build)

    assert artifacts["elf"] == str((build / "firmware.out").resolve())
    assert artifacts["map"] == str((build / "firmware.map").resolve())


def test_discovery_ignores_relocatable_object_elf_files(tmp_path: Path) -> None:
    build = tmp_path / "out"
    build.mkdir()
    (build / "main.o").write_bytes(_elf_bytes(elf_type=1))
    (build / "firmware.axf").write_bytes(_elf_bytes())
    (build / "firmware.map").write_text("map", encoding="utf-8")

    artifacts = native_build._artifact_paths(build)

    assert artifacts["elf"] == str((build / "firmware.axf").resolve())


def test_loadable_elf_check_rejects_truncated_header(tmp_path: Path) -> None:
    candidate = tmp_path / "truncated.elf"
    candidate.write_bytes(b"\x7fELF\x01\x01\x01" + (b"\x00" * 9) + (2).to_bytes(2, "little"))

    assert native_build._is_loadable_elf(candidate) is False


def test_discovery_never_reuses_elf_bytes_as_linker_map(tmp_path: Path) -> None:
    build = tmp_path / "out"
    build.mkdir()
    (build / "firmware.map").write_bytes(_elf_bytes(payload=b"payload"))

    with pytest.raises(RuntimeError, match="exactly one linker map"):
        native_build._artifact_paths(build)


def test_unknown_project_without_command_teaches_universal_recovery(tmp_path: Path) -> None:
    project = tmp_path / "unknown"
    project.mkdir()
    build = tmp_path / "out"

    with pytest.raises(RuntimeError, match="pass its exact native argv after '--'"):
        native_build.run_build(
            Namespace(project_dir=str(project), build_dir=str(build), target=None)
        )


def test_command_template_has_one_provider_neutral_path() -> None:
    template = native_build.command_template()

    assert "provider_selection" not in template
    assert "convenience_argv_template" not in template
    assert "optional_convenience_providers" not in template
    assert template["network_policy"] == "inherited_by_default"
    assert "Windows PowerShell 5" in str(template["powershell_compatibility"])
    assert template["dependency_acquisition"] == (
        "allowed_when_no_compatible_local_resource_exists"
    )
    argv = template["argv_template"]
    assert isinstance(argv, list)
    assert argv[-3:] == ["--", "<build-executable>", "<build-argument>"]


def test_powershell_template_is_directly_executable() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this host")
    assert powershell is not None
    rendered = native_build._powershell_command(
        [sys.executable, "-c", "print('native-template-ok')"]
    )

    completed = subprocess.run(
        [powershell, "-NoProfile", "-Command", rendered],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "native-template-ok"


def test_windows_environment_override_replaces_case_insensitive_key() -> None:
    merged = native_build._apply_environment_overrides(
        {"PATH": "inherited", "HOME": "home"},
        {"Path": "selected"},
        platform_name="nt",
    )

    assert merged == {"Path": "selected", "HOME": "home"}


def test_windows_repeated_overrides_and_offline_guards_are_case_insensitive() -> None:
    overrides = native_build._environment_overrides(
        ["Path=first", "PATH=second"], platform_name="nt"
    )
    offline = native_build._offline_environment(
        {"Https_Proxy": "http://real-proxy", "Path": "tools"}, platform_name="nt"
    )

    assert overrides == {"PATH": "second"}
    proxy_keys = [key for key in offline if key.casefold() == "https_proxy"]
    assert len(proxy_keys) == 1
    assert offline[proxy_keys[0]] == "http://127.0.0.1:9"


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

    assert not hasattr(args, "target")
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


def test_failed_child_that_removes_artifact_root_keeps_structured_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "out"

    def fail_and_remove(*_args: object, **_kwargs: object) -> object:
        build.rmdir()
        return Namespace(returncode=2)

    monkeypatch.setattr(native_build, "run_owned", fail_and_remove)

    with pytest.raises(native_build.BuildEvidenceError) as raised:
        native_build.run_build(
            Namespace(
                project_dir=str(project),
                build_dir=str(build),
                command=["--", "builder"],
            )
        )

    assert raised.value.evidence["argv"] == ["builder"]
    assert raised.value.evidence["cwd"] == str(project.resolve())
    assert raised.value.evidence["exit_code"] == 2
    assert "disappeared" in str(raised.value.evidence["artifact_validation_error"])


def test_agent_command_reports_owned_process_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.setattr(
        native_build,
        "run_owned",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("descendants could not be cleared")
        ),
    )

    with pytest.raises(native_build.BuildEvidenceError) as raised:
        native_build.run_build(
            Namespace(
                project_dir=str(project),
                build_dir=str(tmp_path / "out"),
                command=["--", "builder"],
            )
        )

    assert raised.value.evidence["argv"] == ["builder"]
    assert raised.value.evidence["cwd"] == str(project.resolve())
    assert "ownership failed" in str(raised.value.evidence["process_error"])


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

    with pytest.raises(native_build.BuildEvidenceError, match="different files"):
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


def test_arbitrary_opaque_output_and_custom_timeout_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    build = tmp_path / "vendor-output"
    calls: list[float] = []

    def fake_run(*_args: object, timeout: float, **_kwargs: object) -> object:
        calls.append(timeout)
        build.mkdir(exist_ok=True)
        (build / "firmware.uf2").write_bytes(b"UF2 opaque image")
        return Namespace(returncode=0)

    monkeypatch.setattr(native_build, "run_owned", fake_run)

    result = native_build.run_build(
        Namespace(
            project_dir=str(project),
            build_dir=str(build),
            timeout_seconds="7200",
            artifact=["uf2=firmware.uf2"],
            command=["--", "vendor-builder"],
        )
    )

    evidence = json.loads(capsys.readouterr().out)
    assert result == 0
    assert calls == [7200.0]
    assert evidence["artifacts"]["uf2"] == str((build / "firmware.uf2").resolve())
    assert evidence["artifact_assurance"]["opaque_declared_outputs"] == {
        "uf2": "existing-nonempty-file; format-not-interpreted"
    }


@pytest.mark.parametrize("timeout", ["0", "-1", "86401", "not-a-number"])
def test_invalid_timeout_refuses_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.setattr(
        native_build,
        "run_owned",
        lambda *_args, **_kwargs: pytest.fail("invalid timeout reached process execution"),
    )

    with pytest.raises(RuntimeError, match="timeout"):
        native_build.run_build(
            Namespace(
                project_dir=str(project),
                build_dir=str(tmp_path / "out"),
                timeout_seconds=timeout,
                command=["--", "builder"],
            )
        )
