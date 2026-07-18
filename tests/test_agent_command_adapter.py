from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pyocd_debug_mcp import benchmark_support as benchmark
from pyocd_debug_mcp.agent_command_adapter import (
    AgentCommandAdapter,
    AgentCommandConfig,
    AgentCommandError,
    AgentCommandResultError,
    load_agent_command_config,
)


def config_document(executable: str, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "name": "fake-agent",
        "command": [
            executable,
            "fake_agent.py",
            "{prompt_path}",
            "{result_path}",
            "{mcp_manifest_path}",
        ],
        "version_command": [executable, "--version"],
        "registration_check": None,
        "mcp_mode": "launch_manifest",
        "result_transport": "file",
        "permission_profile": "operator-approved-full-access",
        "model": "operator-selected-model",
        "effort": None,
        "inherit_env": ["PATH"],
        "env": {"FAKE_AGENT_MODE": "test"},
    }
    document.update(overrides)
    return document


def write_config(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_config_rejects_shell_strings_unknown_keys_and_unknown_placeholders(
    tmp_path: Path,
) -> None:
    path = write_config(tmp_path, config_document(sys.executable, command="agent --run"))
    with pytest.raises(AgentCommandError, match="argv array"):
        load_agent_command_config(path)

    path = write_config(tmp_path, config_document(sys.executable, surprise=True))
    with pytest.raises(AgentCommandError, match="unknown fields"):
        load_agent_command_config(path)

    path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            command=[sys.executable, "{prompt_path}", "{result_path}", "{bogus}"],
        ),
    )
    with pytest.raises(AgentCommandError, match="unknown placeholder"):
        load_agent_command_config(path)


def test_config_requires_prompt_result_and_manifest_delivery_contract(tmp_path: Path) -> None:
    for missing in ("{prompt_path}", "{result_path}", "{mcp_manifest_path}"):
        command = config_document(sys.executable)["command"]
        assert isinstance(command, list)
        path = write_config(
            tmp_path,
            config_document(sys.executable, command=[item for item in command if item != missing]),
        )
        with pytest.raises(AgentCommandError, match="requires placeholder"):
            load_agent_command_config(path)


def test_config_rejects_literal_secret_environment_values(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        config_document(sys.executable, env={"API_TOKEN": "must-not-be-persisted"}),
    )
    with pytest.raises(AgentCommandError, match="secret-like"):
        load_agent_command_config(path)


def test_local_fake_agent_subprocess_receives_manifest_and_writes_result(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "fake_agent.py"
    script.write_text(
        """
import json, pathlib, sys
prompt_path, result_path, manifest_path = map(pathlib.Path, sys.argv[1:4])
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
assert manifest['server']['transport'] == 'stdio'
assert 'hello agent' in prompt_path.read_text(encoding='utf-8')
result_path.write_text(json.dumps({'status': 'ok'}), encoding='utf-8')
print('fake agent complete')
""".strip(),
        encoding="utf-8",
    )
    document = config_document(
        sys.executable,
        command=[
            sys.executable,
            str(script),
            "{prompt_path}",
            "{result_path}",
            "{mcp_manifest_path}",
        ],
        version_command=[sys.executable, "--version"],
    )
    config_path = write_config(tmp_path, document)
    adapter = AgentCommandAdapter(load_agent_command_config(config_path))

    run = adapter.run(
        workspace=workspace,
        prompt_text="hello agent",
        result_schema_path=tmp_path / "schema.json",
        repo_root=tmp_path,
        timeout_seconds=10,
    )

    assert run.exit_code == 0
    assert json.loads(run.result_path.read_text(encoding="utf-8")) == {"status": "ok"}
    assert run.metadata["adapter_name"] == "fake-agent"
    assert run.metadata["declared_model"] == "operator-selected-model"
    assert run.metadata["permission_profile"] == "operator-approved-full-access"
    assert run.metadata["inherited_env_names"] == ["PATH"]
    assert "must-not" not in json.dumps(run.metadata)
    assert run.started_at.endswith("+00:00") and run.completed_at.endswith("+00:00")


@pytest.mark.parametrize("mode", ["missing", "invalid"])
def test_missing_or_invalid_result_is_a_failed_run(tmp_path: Path, mode: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "fake_agent.py"
    body = (
        "print('provider output before missing result')"
        if mode == "missing"
        else (
            "import pathlib, sys; print('provider output before invalid result'); "
            "pathlib.Path(sys.argv[2]).write_text('[]', encoding='utf-8')"
        )
    )
    script.write_text(body, encoding="utf-8")
    path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            command=[
                sys.executable,
                str(script),
                "{prompt_path}",
                "{result_path}",
                "{mcp_manifest_path}",
            ],
        ),
    )

    with pytest.raises(AgentCommandResultError, match="result") as caught:
        AgentCommandAdapter(load_agent_command_config(path)).run(
            workspace=workspace,
            prompt_text="prompt",
            result_schema_path=tmp_path / "schema.json",
            repo_root=tmp_path,
            timeout_seconds=10,
        )
    assert caught.value.run.exit_code == 0
    assert "provider output before" in caught.value.run.stdout_text
    assert caught.value.run.completed_at


def test_literal_environment_values_expand_run_scoped_placeholders(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "fake_agent.py"
    script.write_text(
        "import json, os, pathlib, sys; "
        "assert pathlib.Path(os.environ['AGENT_CONFIG_HOME']) == pathlib.Path(sys.argv[1]) / '.agent'; "
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({'status':'ok'}), encoding='utf-8')",
        encoding="utf-8",
    )
    path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            command=[
                sys.executable,
                str(script),
                "{workspace}",
                "{result_path}",
                "{prompt_path}",
                "{mcp_manifest_path}",
            ],
            env={"AGENT_CONFIG_HOME": "{workspace}/.agent"},
        ),
    )

    run = AgentCommandAdapter(load_agent_command_config(path)).run(
        workspace=workspace,
        prompt_text="prompt",
        result_schema_path=tmp_path / "schema.json",
        repo_root=tmp_path,
        timeout_seconds=10,
    )

    assert run.exit_code == 0


def test_preconfigured_mode_does_not_require_manifest_placeholder(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            mcp_mode="preconfigured",
            command=[sys.executable, "fake.py", "{prompt_path}", "{result_path}"],
        ),
    )
    loaded = load_agent_command_config(path)
    assert isinstance(loaded, AgentCommandConfig)
    assert loaded.mcp_mode == "preconfigured"


def test_stdout_json_transport_needs_no_dummy_result_argument_or_version_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "stdout_agent.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "prompt = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "assert 'as the only stdout content' in prompt\n"
        "assert 'Do not write a result file' in prompt\n"
        "assert '.r11_agent_result.json' not in prompt\n"
        "print(json.dumps({'status': 'ok'}))\n",
        encoding="utf-8",
    )
    path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            command=[sys.executable, str(script), "{prompt_path}"],
            version_command=None,
            mcp_mode="preconfigured",
            result_transport="stdout_json",
        ),
    )

    run = AgentCommandAdapter(load_agent_command_config(path)).run(
        workspace=workspace,
        prompt_text="prompt",
        result_schema_path=tmp_path / "schema.json",
        repo_root=tmp_path,
        timeout_seconds=10,
    )

    assert json.loads(run.result_path.read_text(encoding="utf-8")) == {"status": "ok"}
    assert run.metadata["cli_version"] is None


def test_secret_argv_values_are_redacted_from_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "fake_agent.py"
    script.write_text(
        "import json, pathlib, sys; "
        "pathlib.Path(sys.argv[-2]).write_text(json.dumps({'status':'ok'}), encoding='utf-8')",
        encoding="utf-8",
    )
    secret = "literal-secret-value"
    path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            command=[
                sys.executable,
                str(script),
                "--api-key",
                secret,
                "--token=second-secret",
                "{prompt_path}",
                "{result_path}",
                "{mcp_manifest_path}",
            ],
        ),
    )

    run = AgentCommandAdapter(load_agent_command_config(path)).run(
        workspace=workspace,
        prompt_text="prompt",
        result_schema_path=tmp_path / "schema.json",
        repo_root=tmp_path,
        timeout_seconds=10,
    )

    metadata = json.dumps(run.metadata)
    assert secret not in metadata and "second-secret" not in metadata
    assert metadata.count("<redacted>") == 2


def test_benchmark_adapter_preserves_new_mcp_session_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "fake_agent.py"
    script.write_text(
        "import json, pathlib, sys; "
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({'status':'ok'}), encoding='utf-8')",
        encoding="utf-8",
    )
    config_path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            command=[
                sys.executable,
                str(script),
                "{prompt_path}",
                "{result_path}",
                "{mcp_manifest_path}",
            ],
        ),
    )
    run_root = tmp_path / "runs" / "session-new"
    snapshots = [{}, {"session-new": run_root}]
    monkeypatch.setattr(benchmark, "_session_dirs", lambda: snapshots.pop(0))
    monkeypatch.setattr(benchmark, "REPO_ROOT", tmp_path)

    run = benchmark._run_configured_agent(
        config_path,
        workspace,
        "prompt",
        timeout_seconds=10,
    )

    assert run.provider_name == "fake-agent"
    assert run.new_session_dirs == (run_root,)


def test_benchmark_adapter_retains_completed_provider_failure_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "failed_agent.py"
    script.write_text("print('captured provider failure')", encoding="utf-8")
    config_path = write_config(
        tmp_path,
        config_document(
            sys.executable,
            command=[
                sys.executable,
                str(script),
                "{prompt_path}",
                "{result_path}",
                "{mcp_manifest_path}",
            ],
        ),
    )
    monkeypatch.setattr(benchmark, "_session_dirs", lambda: {})
    monkeypatch.setattr(benchmark, "REPO_ROOT", tmp_path)

    run = benchmark._run_configured_agent(
        config_path,
        workspace,
        "prompt",
        timeout_seconds=10,
    )

    assert run.exit_code == 0
    assert "captured provider failure" in run.stdout_text
    assert run.result_path.is_file() is False
