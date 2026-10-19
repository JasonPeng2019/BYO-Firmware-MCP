"""Deterministic regressions for the real-agent test fixture; no AI clients."""

from __future__ import annotations

import json
import copy
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.monitor import paths
from tests import codex_harness as harness


class CodexHarnessIsolationTests(unittest.TestCase):
    def tearDown(self) -> None:
        paths._reset_cache(None)

    def fixture(self) -> harness.CodexAgentTestCase:
        case = harness.CodexAgentTestCase()
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def test_server_process_and_oracle_share_private_store_salt_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            sentinel = parent / "existing-user-data"
            sentinel.write_bytes(b"preserve me")
            with (
                patch.dict(os.environ, {paths.MONITOR_ROOT_ENV: str(parent)}),
                patch.object(paths, "_app_data_candidate", side_effect=AssertionError("AppData")),
            ):
                paths._reset_cache(parent / "stale-cache")
                case = self.fixture()
                self.assertEqual(case.store, case.monitor_root)
                self.assertNotEqual(case.store, parent)
                self.assertTrue(case.store.is_relative_to(parent))
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import json,sys; from pathlib import Path; "
                            "from pyocd_debug_mcp.monitor import paths; "
                            "print(json.dumps([str(paths.resolve_store_root().root), "
                            "paths.workspace_id(Path(sys.argv[1])), paths.deployment_salt().hex()]))"
                        ),
                        str(case.project),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                self.assertEqual(
                    json.loads(child.stdout),
                    [
                        str(case.store),
                        case.workspace,
                        paths.deployment_salt().hex(),
                    ],
                )
                # Unbound startup and delivery records belong to the private
                # root too, so cleanup must remove more than the bound digest.
                (case.store / "server_data" / "unbound").mkdir()
                (case.store / "simulated_remote" / "pending").write_text("private")
                case.doCleanups()
                self.assertFalse(case.test_root.exists())
                self.assertFalse(case.project.exists())
                self.assertFalse(case.codex_home.exists())
                self.assertEqual(os.environ[paths.MONITOR_ROOT_ENV], str(parent))
                self.assertIsNone(paths._cached_root)
                self.assertIsNone(paths._cached_salt)
                self.assertIsNone(paths._override_root)
            self.assertEqual(sentinel.read_bytes(), b"preserve me")
            self.assertEqual(list(parent.iterdir()), [sentinel])

    def test_without_ambient_root_never_resolves_appdata_and_restores_environment(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop(paths.MONITOR_ROOT_ENV, None)
            with patch.object(paths, "_app_data_candidate", side_effect=AssertionError("AppData")):
                case = self.fixture()
                self.assertEqual(paths.resolve_store_root().root, case.store)
                case.doCleanups()
                self.assertNotIn(paths.MONITOR_ROOT_ENV, os.environ)
                self.assertIsNone(paths._cached_root)

    def test_sequential_tests_get_distinct_stores_and_fresh_salts(self) -> None:
        first = self.fixture()
        first_salt = paths.deployment_salt()
        first.doCleanups()
        second = self.fixture()
        self.assertNotEqual(first.store, second.store)
        self.assertNotEqual(first_salt, paths.deployment_salt())
        self.assertFalse(first.store.exists())

    def test_setup_failure_cleans_private_store_project_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {paths.MONITOR_ROOT_ENV: temporary}):
                case = harness.CodexAgentTestCase()
                try:
                    with patch.object(
                        case, "_isolated_codex_home", side_effect=RuntimeError("setup")
                    ):
                        with self.assertRaisesRegex(RuntimeError, "setup"):
                            case.setUp()
                finally:
                    case.doCleanups()
                self.assertFalse(case.project.exists())
                self.assertEqual(list(Path(temporary).iterdir()), [])
                self.assertEqual(os.environ[paths.MONITOR_ROOT_ENV], temporary)
                self.assertIsNone(paths._cached_root)

    def test_mcp_config_and_agent_environment_use_exact_private_roots_and_pin(self) -> None:
        case = self.fixture()
        configuration = tomllib.loads((case.codex_home / "config.toml").read_text())
        self.assertEqual(configuration["model"], harness.REQUIRED_MODEL)
        self.assertFalse(configuration["features"]["shell_tool"])
        self.assertFalse(configuration["features"]["multi_agent"])
        self.assertEqual(configuration["web_search"], "disabled")
        server = configuration["mcp_servers"][harness.SERVER_NAME]
        self.assertTrue(server["required"])
        self.assertEqual(
            server["args"],
            [
                "run",
                "--locked",
                "--project",
                harness.SERVER_PROJECT.as_posix(),
                "pyocd-debug-mcp",
            ],
        )
        completed = subprocess.CompletedProcess([], 0, "DONE", f"model: {harness.REQUIRED_MODEL}")
        with (
            patch.object(harness, "codex_executable", return_value="codex-fixture"),
            patch.object(harness.subprocess, "run", return_value=completed) as run,
        ):
            case.run_agent("call server_health_check", bind_workspace=False)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], harness.REQUIRED_MODEL)
        self.assertEqual(run.call_args.kwargs["cwd"], str(case.project))
        for key, value in server["env"].items():
            self.assertEqual(run.call_args.kwargs["env"][key], value)
        self.assertEqual(server["env"][paths.MONITOR_ROOT_ENV], str(case.store))
        self.assertEqual(server["env"]["BYO_MCP_ARTIFACT_ROOT"], str(case.artifact_root))

    def test_timeout_keeps_raw_output_and_config_before_cleanup(self) -> None:
        case = self.fixture()
        sessions = case.codex_home / "sessions"
        sessions.mkdir()
        (sessions / "fixture.jsonl").write_text('{"fixture":true}\n')
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.dict(os.environ, {"BYO_MANAGED_TEST_EVIDENCE": temporary}),
                patch.object(harness, "codex_executable", return_value="codex-fixture"),
                patch.object(
                    harness.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(
                        "codex-fixture",
                        1,
                        output=b"partial output",
                        stderr=b"partial error",
                    ),
                ),
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    case.run_agent("test fixture", timeout=1)
            (receipt_path,) = Path(temporary).rglob("receipt.json")
            receipt = json.loads(receipt_path.read_text())
            self.assertTrue(receipt["timeout"])
            self.assertIsNone(receipt["returncode"])
            self.assertEqual((receipt_path.parent / "stdout.txt").read_text(), "partial output")
            self.assertEqual((receipt_path.parent / "stderr.txt").read_text(), "partial error")
            self.assertTrue((receipt_path.parent / "mcp-config.toml").is_file())
            self.assertEqual(
                (receipt_path.parent / "sessions" / "fixture.jsonl").read_text(),
                '{"fixture":true}\n',
            )
            self.assertEqual(
                (receipt_path.parent / "monitor" / paths.SALT_FILE).read_bytes(),
                (case.store / paths.SALT_FILE).read_bytes(),
            )

    def test_evidence_does_not_accept_a_different_model(self) -> None:
        case = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.dict(os.environ, {"BYO_MANAGED_TEST_EVIDENCE": temporary}),
                patch.object(harness, "codex_executable", return_value="codex-fixture"),
                patch.object(
                    harness.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        "DONE",
                        "model: wrong-model",
                    ),
                ),
            ):
                with self.assertRaisesRegex(AssertionError, "pinned model"):
                    case.run_agent("test fixture")
            (receipt_path,) = Path(temporary).rglob("receipt.json")
            self.assertEqual((receipt_path.parent / "stderr.txt").read_text(), "model: wrong-model")

    def test_negative_claim_oracle_rejects_empty_failed_wrong_and_unobserved_sessions(self):
        case = self.fixture()
        handshake = {
            "server": harness.SERVER_NAME,
            "tool": "initialization_handshake",
            "status": "completed",
            "arguments": {"workspace_path": str(case.project)},
            "result": {"content": [{"type": "text", "text": "guidance"}], "isError": False},
        }
        health = {
            "server": harness.SERVER_NAME,
            "tool": "server_health_check",
            "status": "completed",
            "arguments": {},
            "result": {
                "isError": False,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "run_id": "control-run",
                                "recording": {"workspace_bound": True},
                                "counters": {"per_tool": {"initialization_handshake": 1}},
                            }
                        ),
                    }
                ],
            },
        }
        records = [
            {"kind": "boot", "workspace": case.workspace, "run_id": "control-run"},
            {
                "kind": "usage_snapshot",
                "workspace": case.workspace,
                "run_id": "control-run",
                "detail": {"per_tool": {"initialization_handshake": 1, "server_health_check": 1}},
            },
        ]
        result = harness.CodexResult(0, "DONE", "", protocol_calls=[handshake, health])
        with patch.object(case, "ledger_records", return_value=records):
            case.assert_bound_activity(result, health=True)
            for defect in (
                "resource_only",
                "failed_handshake",
                "wrong_server",
                "wrong_project",
                "no_health",
                "failed_health",
                "unbound_health",
                "different_run",
                "damaged_json",
                "empty_store",
                "no_health_counts",
            ):
                with self.subTest(defect=defect):
                    broken = copy.deepcopy(result)
                    ledger = copy.deepcopy(records)
                    if defect == "resource_only":
                        broken.protocol_calls = []
                    elif defect == "failed_handshake":
                        broken.protocol_calls[0]["result"]["isError"] = True
                    elif defect == "wrong_server":
                        broken.protocol_calls[0]["server"] = "unrelated"
                    elif defect == "wrong_project":
                        broken.protocol_calls[0]["arguments"]["workspace_path"] = str(
                            case.project.parent
                        )
                    elif defect == "no_health":
                        broken.protocol_calls.pop()
                    elif defect == "failed_health":
                        broken.protocol_calls[1]["result"]["isError"] = True
                    elif defect in ("unbound_health", "different_run", "damaged_json"):
                        block = broken.protocol_calls[1]["result"]["content"][0]
                        value = json.loads(block["text"])
                        if defect == "unbound_health":
                            value["recording"]["workspace_bound"] = False
                        elif defect == "different_run":
                            value["run_id"] = "other-run"
                        block["text"] = json.dumps(value) + (
                            "\nnot JSON" if defect == "damaged_json" else ""
                        )
                    elif defect == "empty_store":
                        ledger.clear()
                    else:
                        ledger[1]["detail"]["per_tool"]["server_health_check"] = 0
                    with patch.object(case, "ledger_records", return_value=ledger):
                        with self.assertRaises((AssertionError, json.JSONDecodeError)):
                            case.assert_bound_activity(broken, health=True)

    def test_protocol_evidence_requires_native_completed_target_events(self):
        case = self.fixture()
        sessions = case.codex_home / "sessions"
        sessions.mkdir()
        item = {
            "type": "McpToolCall",
            "server": harness.SERVER_NAME,
            "tool": "initialization_handshake",
            "status": "completed",
            "arguments": {"workspace_path": str(case.project)},
            "result": {"isError": False},
        }
        event = {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}
        unrelated = copy.deepcopy(event)
        unrelated["payload"]["item"]["server"] = "unrelated"
        (sessions / "control.jsonl").write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    unrelated,
                    {"type": "response_item", "payload": {"type": "message", "text": str(item)}},
                    event,
                )
            ),
            encoding="utf-8",
        )
        self.assertEqual(case._protocol_calls(), [item])

    def test_target_error_response_counts_but_transport_failure_or_resource_discovery_does_not(
        self,
    ):
        case = self.fixture()
        error = {
            "server": harness.SERVER_NAME,
            "tool": "report_agent_issue",
            "status": "failed",
            "result": {"isError": True, "content": [{"type": "text", "text": "missing fields"}]},
        }
        transport = dict(error, result=None)
        unfinished = dict(error, status="inProgress")
        resource = dict(error, server="codex", tool="list_mcp_resources")
        result = harness.CodexResult(
            0, "ERROR", "", protocol_calls=[error, transport, unfinished, resource]
        )
        self.assertEqual(case.target_replies(result, "report_agent_issue"), [error])
        # Even a genuine target error cannot prove a successful binding.
        failed_handshake = dict(
            error, tool="initialization_handshake", arguments={"workspace_path": str(case.project)}
        )
        result.protocol_calls = [failed_handshake]
        with self.assertRaisesRegex(AssertionError, "no successful target handshake"):
            case.assert_bound_activity(result)


if __name__ == "__main__":
    unittest.main()
