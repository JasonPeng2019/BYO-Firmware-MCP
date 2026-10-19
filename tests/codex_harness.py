"""Drive a real Codex agent against a real instance of this MCP server.

These are not smoke tests. Codex launches the server exactly as a deployed client
would -- its own process, over stdio, discovered through the MCP handshake -- and
a real model decides which tools to call. What we then assert is what the monitor
actually recorded about that traffic.

Model is pinned to ``gpt-5.6-luna`` and the run fails rather than silently
downgrading: a test that quietly used a different model would be testing
something other than what it claims.

Isolation: each test supplies a private BYO_MCP_MONITOR_ROOT to both the server
and the in-process observation helpers. They share its deployment salt and
workspace digest. Cleanup removes the entire private store, including unbound
startup records, without reading or deleting the user's monitor data.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pyocd_debug_mcp.monitor.counters import (
    CHECKIN_CADENCE_ENV,
    SNAPSHOT_CADENCE_ENV,
)

REQUIRED_MODEL = "gpt-5.6-luna"
SERVER_PROJECT = Path(__file__).resolve().parents[1]
SERVER_NAME = "byo_monitor_itest"
DEFAULT_TIMEOUT = 600

# Scaled down from the shipped 100/500 so a handful of real agent calls crosses a
# boundary. The ratio is kept -- the check-in cadence is a multiple of the snapshot
# cadence -- so the interaction between the two is the same one production sees.
E2E_SNAPSHOT_CADENCE = 2
E2E_CHECKIN_CADENCE = 4


# Model renames that codex's own `[notice.model_migrations]` mechanism is
# known to apply: a session pinned to a retired name reports under its
# replacement instead, with a notice, not the name that was requested.
#
# This is deliberately a checked-in mapping, not a live read of the user's own
# `~/.codex/config.toml`. REQUIRED_MODEL exists specifically as a hard pin --
# "fail loudly rather than silently testing a different model" -- and reading
# the accepted-name set from a file that lives outside the repo, is not
# version-controlled, and can differ machine to machine would let the same
# commit's test outcome vary or drift over time, undermining that exact
# guarantee. When codex renames a pinned model, update this mapping in the
# same commit that notices it, reviewed like any other test expectation.
_KNOWN_MODEL_MIGRATIONS: dict[str, str] = {
    "gpt-5.4-mini": "gpt-5.6-luna",
}


def _known_model_migration(model: str) -> str | None:
    """Return the name codex is known to report for `model` after its own
    server-side rename, per `_KNOWN_MODEL_MIGRATIONS` above. `None` means no
    known migration; the caller falls back to the pinned name unchanged.
    """

    return _KNOWN_MODEL_MIGRATIONS.get(model)


def codex_executable() -> str | None:
    return shutil.which("codex")


def codex_available() -> tuple[bool, str]:
    """Return whether a logged-in codex CLI is present."""

    executable = codex_executable()
    if executable is None:
        return False, "codex CLI is not on PATH"
    try:
        status = subprocess.run(
            [executable, "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"codex login status failed: {exc}"
    # `codex login status` reports on stderr, not stdout.
    combined = f"{status.stdout or ''}\n{status.stderr or ''}"
    if status.returncode != 0 or "Logged in" not in combined:
        return False, f"codex CLI is not logged in: {combined.strip()[:200]}"
    return True, ""


@dataclass
class CodexResult:
    returncode: int
    stdout: str
    stderr: str
    tool_calls: list[str] = field(default_factory=list)
    protocol_calls: list[dict] = field(default_factory=list)

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}"

    def called(self, tool: str) -> bool:
        return any(tool in entry for entry in self.tool_calls)


def _store_root() -> Path:
    from pyocd_debug_mcp.monitor import paths

    root = paths.resolve_store_root().root
    if root is None:
        raise AssertionError("the isolated monitor store is unavailable")
    return root


def _workspace_digest(project: Path) -> str:
    """Compute the store folder name this project maps to.

    Imported in-process purely to locate the folder for assertions and cleanup;
    the salt is shared because both use the same explicit private store.
    """

    from pyocd_debug_mcp.monitor import paths

    paths._reset_cache(None)
    return paths.workspace_id(project)


class CodexAgentTestCase(unittest.TestCase):
    """Base case that runs a real agent and exposes what the monitor recorded."""

    @classmethod
    def setUpClass(cls) -> None:
        available, reason = codex_available()
        if not available:
            raise unittest.SkipTest(reason)

    def setUp(self) -> None:
        from pyocd_debug_mcp.monitor import paths

        # An explicit suite root owns only these disposable children; never use
        # the suite root itself as a test store or delete its existing contents.
        parent = os.environ.get(paths.MONITOR_ROOT_ENV, "").strip()
        if parent:
            Path(parent).expanduser().mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(
            prefix="byo-codex-test-", dir=Path(parent).expanduser() if parent else None
        )
        self.addCleanup(temporary.cleanup)
        self.test_root = Path(temporary.name).resolve()
        self.monitor_root = self.test_root / "monitor"
        self.artifact_root = self.test_root / "artifacts"
        environment = patch.dict(os.environ, {paths.MONITOR_ROOT_ENV: str(self.monitor_root)})
        environment.start()
        self.addCleanup(environment.stop)
        paths._reset_cache(None)
        self.addCleanup(paths._reset_cache, None)
        self.project = Path(tempfile.mkdtemp(prefix="byo-codex-proj-"))
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / "README.md").write_text(
            "Toy firmware project used by the monitor integration tests.\n",
            encoding="utf-8",
        )
        self.store = _store_root()
        self.workspace = _workspace_digest(self.project)
        self.codex_home = self._isolated_codex_home()

    def _isolated_codex_home(self) -> Path:
        """Build a codex home containing only this test's MCP server.

        The developer's real config may already register a BYO server whose tools
        have identical names. With both visible the agent picks whichever it likes
        and the test silently measures the wrong server -- which is exactly what
        happened before this existed. Carrying over only the credentials keeps the
        run authentic while making the tool surface unambiguous.
        """

        home = Path(tempfile.mkdtemp(prefix="byo-codex-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        source_auth = Path.home() / ".codex" / "auth.json"
        if source_auth.exists():
            shutil.copy2(source_auth, home / "auth.json")
        project = str(SERVER_PROJECT).replace("\\", "/")
        (home / "config.toml").write_text(
            "\n".join(
                [
                    f'model = "{REQUIRED_MODEL}"',
                    'model_reasoning_effort = "medium"',
                    'web_search = "disabled"',
                    "",
                    # These tests exercise MCP only. A prompt alone does not
                    # prevent a client from reading unrelated source/stores.
                    "[features]",
                    "shell_tool = false",
                    "multi_agent = false",
                    "",
                    f"[mcp_servers.{SERVER_NAME}]",
                    "command = 'uv'",
                    f"args = ['run','--locked','--project','{project}','pyocd-debug-mcp']",
                    f"cwd = '{project}'",
                    # Optional servers have a short initial catalog grace period.
                    # This target is essential; wait for its real initialization.
                    "required = true",
                    "startup_timeout_sec = 180",
                    "tool_timeout_sec = 120",
                    "",
                    # Scaled-down cadences so a handful of real agent calls reaches
                    # the snapshot and check-in paths. Without this the centrepiece
                    # of the recording design is unreachable end to end: no test
                    # session makes 100 tool calls. Set here as well as in the
                    # subprocess environment because the server is codex's child
                    # and either route could be the one that carries it.
                    f"[mcp_servers.{SERVER_NAME}.env]",
                    f"BYO_MCP_MONITOR_ROOT = {json.dumps(str(self.monitor_root))}",
                    f"BYO_MCP_ARTIFACT_ROOT = {json.dumps(str(self.artifact_root))}",
                    f'{SNAPSHOT_CADENCE_ENV} = "{E2E_SNAPSHOT_CADENCE}"',
                    f'{CHECKIN_CADENCE_ENV} = "{E2E_CHECKIN_CADENCE}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return home

    # -- driving the agent ------------------------------------------------

    def bind_preamble(self) -> str:
        """Instruction that binds this run's workspace.

        Without it the session records under the shared ``unbound`` workspace,
        which neither isolates the run nor exercises workspace binding.
        """

        return (
            "Do not use web search; use only the MCP server's tools. "
            "First call the MCP tool initialization_handshake with workspace_path "
            f"set to '{self.project.as_posix()}'. Then "
        )

    def run_agent(
        self,
        prompt: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        bind_workspace: bool = True,
    ) -> CodexResult:
        executable = codex_executable()
        assert executable is not None
        if bind_workspace:
            prompt = self.bind_preamble() + prompt
        command = [
            executable,
            "exec",
            "--model",
            REQUIRED_MODEL,
            "--skip-git-repo-check",
            # The server writes to the private monitor directory, which sits
            # outside the firmware workspace, so it needs filesystem access.
            # Approvals stay enforced; only the filesystem policy is widened.
            "-s",
            "danger-full-access",
            prompt,
        ]
        environment = {
            **os.environ,
            "RUST_LOG": "error",
            # Isolated home: only this test's MCP server is visible.
            "CODEX_HOME": str(self.codex_home),
            "BYO_MCP_MONITOR_ROOT": str(self.monitor_root),
            "BYO_MCP_ARTIFACT_ROOT": str(self.artifact_root),
            # Inherited by the MCP server codex spawns, so a short session
            # still reaches a snapshot boundary.
            SNAPSHOT_CADENCE_ENV: str(E2E_SNAPSHOT_CADENCE),
            CHECKIN_CADENCE_ENV: str(E2E_CHECKIN_CADENCE),
        }
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(self.project),
                stdin=subprocess.DEVNULL,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            self._save_agent_evidence(command, environment, exc)
            raise
        self._save_agent_evidence(command, environment, completed)
        result = CodexResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        result.tool_calls = [
            line.strip() for line in result.combined.splitlines() if line.strip().startswith("mcp:")
        ]
        self._assert_model_was_honoured(result)
        result.protocol_calls = self._protocol_calls()
        if bind_workspace:
            self.assert_bound_activity(result)
        return result

    def _protocol_calls(self) -> list[dict]:
        """Native terminal MCP events, independent of the model's printed answer."""
        calls = []
        for path in sorted((self.codex_home / "sessions").rglob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                payload = event.get("payload", {})
                item = payload.get("item", {})
                if (
                    event.get("type") == "event_msg"
                    and payload.get("type") == "item_completed"
                    and item.get("type") == "McpToolCall"
                    and item.get("server") == SERVER_NAME
                ):
                    calls.append(item)
        return calls

    def target_replies(self, result: CodexResult, tool: str) -> list[dict]:
        """Actual target responses, including expected MCP tool errors.

        Codex labels an MCP isError response as failed. A transport failure
        without an MCP result cannot establish target behavior.
        """
        return [
            call
            for call in result.protocol_calls
            if call.get("server") == SERVER_NAME
            and call.get("tool") == tool
            and isinstance(call.get("result"), dict)
            and (
                call.get("status") == "completed"
                or (call.get("status") == "failed" and call["result"].get("isError") is True)
            )
        ]

    def assert_bound_activity(self, result: CodexResult, *, health: bool = False) -> None:
        """Negative claims require successful traffic and this workspace's durable boot.

        Resource discovery, a failed handshake, or an empty store proves nothing.
        Native MCP events carry actual arguments/results; ledger and live readout
        must corroborate the same server run. Never parse around damaged JSON.
        """
        self.assertEqual(result.returncode, 0, result.combined[-2000:])
        handshakes = [
            call
            for call in self.target_replies(result, "initialization_handshake")
            if not call["result"].get("isError")
            and call.get("arguments", {}).get("workspace_path")
            and Path(call["arguments"]["workspace_path"]).resolve() == self.project.resolve()
        ]
        self.assertTrue(handshakes, "UNPROVEN: no successful target handshake bound this project")
        boots = [
            record
            for record in self.ledger_records()
            if record.get("kind") == "boot"
            and record.get("workspace") == self.workspace
            and record.get("run_id")
        ]
        self.assertTrue(boots, "UNPROVEN: no persisted boot for the bound workspace")
        if not health:
            return
        calls = [
            call
            for call in self.target_replies(result, "server_health_check")
            if not call["result"].get("isError")
        ]
        self.assertTrue(calls, "UNPROVEN: no successful target health observation")
        observations = [json.loads(call["result"]["content"][0]["text"]) for call in calls]
        run_ids = {boot["run_id"] for boot in boots}
        matching = [
            value
            for value in observations
            if value.get("run_id") in run_ids
            and value.get("recording", {}).get("workspace_bound") is True
            and value.get("counters", {}).get("per_tool", {}).get("initialization_handshake", 0) > 0
        ]
        self.assertTrue(matching, "UNPROVEN: health did not observe the bound boot/handshake")
        self.assertTrue(
            any(
                record.get("run_id") in run_ids
                and record["detail"]["per_tool"].get("server_health_check", 0) > 0
                and record["detail"]["per_tool"].get("initialization_handshake", 0) > 0
                for record in self.counted_records()
            ),
            "UNPROVEN: no durable handshake/health counters",
        )

    def _save_agent_evidence(
        self,
        command: list[str],
        environment: dict[str, str],
        completed: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired,
    ) -> None:
        """Retain authentic traffic and store contents before temporary cleanup."""

        configured = os.environ.get("BYO_MANAGED_TEST_EVIDENCE")
        if not configured:
            return
        destination = Path(configured) / self.id() / uuid4().hex
        destination.mkdir(parents=True)
        for stream in ("stdout", "stderr"):
            value = getattr(completed, stream, None) or ""
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            (destination / f"{stream}.txt").write_text(value, encoding="utf-8")
        shutil.copy2(self.codex_home / "config.toml", destination / "mcp-config.toml")
        sessions = self.codex_home / "sessions"
        if sessions.exists():
            shutil.copytree(sessions, destination / "sessions")
        shutil.copytree(self.store, destination / "monitor")
        receipt = {
            "command": command,
            "cwd": str(self.project),
            "required_model": REQUIRED_MODEL,
            "returncode": getattr(completed, "returncode", None),
            "timeout": isinstance(completed, subprocess.TimeoutExpired),
            "workspace": self.workspace,
            "environment": {
                key: environment[key]
                for key in (
                    "CODEX_HOME",
                    "BYO_MCP_MONITOR_ROOT",
                    "BYO_MCP_ARTIFACT_ROOT",
                    SNAPSHOT_CADENCE_ENV,
                    CHECKIN_CADENCE_ENV,
                )
            },
        }
        (destination / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    def _assert_model_was_honoured(self, result: CodexResult) -> None:
        """Fail loudly rather than silently testing a different model.

        Accepts either the pinned name itself, or the name codex's own
        `[notice.model_migrations]` table says that pinned name now resolves
        to -- see `_known_model_migration`. A session that reports neither is
        still rejected: this must not become a rubber stamp for an arbitrary
        model change.
        """

        expected = [REQUIRED_MODEL]
        migrated = _known_model_migration(REQUIRED_MODEL)
        if migrated:
            expected.append(migrated)
        self.assertTrue(
            any(f"model: {name}" in result.combined for name in expected),
            "codex did not report the pinned model "
            f"({' or its known migration '.join(expected)}); refusing to accept the run",
        )

    # -- reading what the monitor recorded --------------------------------

    @property
    def workspace_dir(self) -> Path:
        return self.store / "server_data" / self.workspace

    @property
    def delivered_dir(self) -> Path:
        return self.store / "simulated_remote" / self.workspace

    def ledger_records(self) -> list[dict]:
        """Every record the monitor wrote for this workspace, delivered or not."""

        records: list[dict] = []
        for base in (self.workspace_dir, self.delivered_dir):
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records

    def reports(self) -> list[dict]:
        found: list[dict] = []
        for base in (self.workspace_dir, self.delivered_dir):
            if not base.exists():
                continue
            for path in sorted(base.rglob("rpt-*.json")):
                try:
                    found.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return found

    def counted_records(self) -> list[dict]:
        """Records carrying the run's cumulative counts.

        The ledger has no per-call record, so "what ran" is answered by the
        cumulative counts a snapshot, check-in, report, or close record carries.
        A short session never reaches a snapshot boundary, but its close record
        still carries the totals -- which is why these tests shut the server down
        before reading.
        """

        return [
            record
            for record in self.ledger_records()
            if isinstance(record.get("detail"), dict) and "per_tool" in record["detail"]
        ]

    def tools_recorded(self) -> set[str]:
        """Every tool the run counted, from the latest cumulative counts.

        Because the counts are cumulative rather than per-window, the most recent
        counted record alone is authoritative -- no replay or union across
        snapshots is required for correctness. The union is taken anyway so the
        helper does not depend on which record happened to be written last.
        """

        found: set[str] = set()
        for record in self.counted_records():
            found.update(str(name) for name in record["detail"]["per_tool"])
        return found

    def call_count_recorded(self) -> int:
        """The highest cumulative call total any record reported."""

        totals = [
            int(record["detail"].get("total_calls") or 0) for record in self.counted_records()
        ]
        return max(totals) if totals else 0

    def outcomes_recorded(self) -> set[str]:
        found: set[str] = set()
        for record in self.counted_records():
            found.update(str(o) for o in record["detail"].get("per_outcome", {}))
        return found

    def record_kinds(self) -> set[str]:
        return {str(record.get("kind")) for record in self.ledger_records()}

    def store_text(self) -> str:
        chunks: list[str] = []
        for base in (self.workspace_dir, self.delivered_dir):
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_file():
                    try:
                        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue
        return "\n".join(chunks)


__all__ = [
    "REQUIRED_MODEL",
    "CodexAgentTestCase",
    "CodexResult",
    "codex_available",
]
