from __future__ import annotations

import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from pyocd_debug_mcp.adapters import backend_registry
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.kernel.singleton import ServerBAlreadyRunningError, ServerBLease
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import ServerRun
from pyocd_debug_mcp.turnkey.provider import (
    ProviderConfig,
    ProviderError,
    SubprocessMiddlemanFactory,
)
from pyocd_debug_mcp.tools.handshake import build_initialization_guidance
from pyocd_debug_mcp.turnkey.server_b_probe import ServerBIdentity, identity_from_handshake


def test_server_b_cross_process_lease_is_exclusive(tmp_path: Path) -> None:
    lock = tmp_path / "server-b.lock"
    with ServerBLease(lock):
        with pytest.raises(ServerBAlreadyRunningError):
            with ServerBLease(lock):
                pytest.fail("a second hardware owner must never start")
    with ServerBLease(lock):
        pass


def test_server_b_identity_parser_accepts_the_real_handshake_shape() -> None:
    run = ServerRun(run_id="run-real-handshake-shape")
    text = build_initialization_guidance(ToolRegistry(), server_run=run)

    assert identity_from_handshake(text) == ServerBIdentity(
        "pyocd-debug-mcp-server-b", 1, run.run_id
    )


def test_provider_wrapper_must_prove_exact_server_b_readiness(tmp_path: Path) -> None:
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        """import json, os, sys
sys.stderr.write("diagnostic-" * 20000)
sys.stderr.flush()
print(json.dumps({
    "type": "ready",
    "provider_id": "test-provider",
    "server_b_url": os.environ["BYO_SERVER_B_URL"],
    "server_b_product_id": "pyocd-debug-mcp-server-b",
    "server_b_contract_version": 1,
    "server_b_run_id": "run-unit-proof",
    "mcp_initialized": True,
    "tools_listed": True,
}), flush=True)
for line in sys.stdin:
    json.loads(line)
    print(json.dumps({"action": "fail_task"}), flush=True)
""",
        encoding="utf-8",
    )
    factory = SubprocessMiddlemanFactory(
        ProviderConfig((sys.executable, str(wrapper)), "test-provider", (), {}),
        endpoint_verifier=lambda _url: ServerBIdentity(
            "pyocd-debug-mcp-server-b", 1, "run-unit-proof"
        ),
    )
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    session = factory.open(
        workspace=tmp_path,
        server_b_url="http://127.0.0.1:8765/mcp",
        artifact_root=artifact_root,
    )
    try:
        assert session.exchange("test", timeout_seconds=2.0) == {"action": "fail_task"}
    finally:
        session.close()


def test_provider_wrapper_rejects_a_different_server_b_run(tmp_path: Path) -> None:
    wrapper = tmp_path / "wrong-wrapper.py"
    wrapper.write_text(
        """import json, os
print(json.dumps({
    "type": "ready",
    "provider_id": "test-provider",
    "server_b_url": os.environ["BYO_SERVER_B_URL"],
    "server_b_product_id": "pyocd-debug-mcp-server-b",
    "server_b_contract_version": 1,
    "server_b_run_id": "run-stale",
    "mcp_initialized": True,
    "tools_listed": True,
}), flush=True)
""",
        encoding="utf-8",
    )
    factory = SubprocessMiddlemanFactory(
        ProviderConfig((sys.executable, str(wrapper)), "test-provider", (), {}),
        endpoint_verifier=lambda _url: ServerBIdentity(
            "pyocd-debug-mcp-server-b", 1, "run-current"
        ),
    )
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    with pytest.raises(ProviderError, match="exact documented readiness frame"):
        factory.open(
            workspace=tmp_path,
            server_b_url="http://127.0.0.1:8765/mcp",
            artifact_root=artifact_root,
        )


def test_builtin_backend_is_selected_through_production_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BYO_TARGET_BACKEND", "pyocd")
    assert isinstance(backend_registry.configured_backend(), PyOCDSWDInterface)
    monkeypatch.setenv("BYO_TARGET_BACKEND", "missing")
    with pytest.raises(RuntimeError, match="available"):
        backend_registry.configured_backend()


def test_provider_config_must_match_outer_client_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "provider.json"
    config.write_text(
        '{"schema_version":2,"provider_id":"codex","command":["wrapper"],'
        '"inherit_env":[],"env":{}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("BYO_MIDDLEMAN_CONFIG", str(config))
    monkeypatch.setenv("BYO_CLIENT_PROVIDER", "claude")
    with pytest.raises(ProviderError, match="does not match"):
        SubprocessMiddlemanFactory.from_environment()


def test_invalid_provider_argv_is_a_typed_configuration_error() -> None:
    with pytest.raises(ProviderError, match="provider command is invalid"):
        ProviderConfig((), "test-provider")


def test_private_fastmcp_schema_adapter_uses_the_pinned_sdk() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp==1.28.1"' in pyproject
    assert version("mcp") == "1.28.1"
