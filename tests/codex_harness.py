"""Drive a real Codex agent against a real instance of this MCP server.

These are not smoke tests. Codex launches the server exactly as a deployed client
would -- its own process, over stdio, discovered through the MCP handshake -- and
a real model decides which tools to call. What we then assert is what the monitor
actually recorded about that traffic.

Model is pinned to ``gpt-5.4-mini`` and the run fails rather than silently
downgrading: a test that quietly used a different model would be testing
something other than what it claims.

Isolation: the monitoring store resolves to the per-user application-data
directory through the platform's shell API, which no environment variable can
redirect. Each run therefore uses a unique temporary project directory, whose
anonymized workspace digest gives it a private folder inside the real store. The
harness removes exactly those folders afterwards.
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

from pyocd_debug_mcp.monitor.counters import (
    CHECKIN_CADENCE_ENV,
    SNAPSHOT_CADENCE_ENV,
)

REQUIRED_MODEL = "gpt-5.4-mini"
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

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}"

    def called(self, tool: str) -> bool:
        return any(tool in entry for entry in self.tool_calls)


def _store_root() -> Path:
    from platformdirs import user_data_dir

    return Path(user_data_dir("BYO", appauthor=False, roaming=False))


def _workspace_digest(project: Path) -> str:
    """Compute the store folder name this project maps to.

    Imported in-process purely to locate the folder for assertions and cleanup;
    the salt is shared because both use the same per-user store.
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
        self.project = Path(tempfile.mkdtemp(prefix="byo-codex-proj-"))
        (self.project / "README.md").write_text(
            "Toy firmware project used by the monitor integration tests.\n",
            encoding="utf-8",
        )
        self.addCleanup(shutil.rmtree, self.project, True)
        self.store = _store_root()
        self.workspace = _workspace_digest(self.project)
        self.addCleanup(self._clean_workspace)
        self.codex_home = self._isolated_codex_home()
        self.addCleanup(shutil.rmtree, self.codex_home, True)

    def _isolated_codex_home(self) -> Path:
        """Build a codex home containing only this test's MCP server.

        The developer's real config may already register a BYO server whose tools
        have identical names. With both visible the agent picks whichever it likes
        and the test silently measures the wrong server -- which is exactly what
        happened before this existed. Carrying over only the credentials keeps the
        run authentic while making the tool surface unambiguous.
        """

        home = Path(tempfile.mkdtemp(prefix="byo-codex-home-"))
        source_auth = Path.home() / ".codex" / "auth.json"
        if source_auth.exists():
            shutil.copy2(source_auth, home / "auth.json")
        project = str(SERVER_PROJECT).replace("\\", "/")
        (home / "config.toml").write_text(
            "\n".join(
                [
                    f'model = "{REQUIRED_MODEL}"',
                    'model_reasoning_effort = "medium"',
                    "",
                    f"[mcp_servers.{SERVER_NAME}]",
                    "command = 'uv'",
                    f"args = ['run','--project','{project}','pyocd-debug-mcp']",
                    f"cwd = '{project}'",
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
                    f'{SNAPSHOT_CADENCE_ENV} = "{E2E_SNAPSHOT_CADENCE}"',
                    f'{CHECKIN_CADENCE_ENV} = "{E2E_CHECKIN_CADENCE}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return home

    def _clean_workspace(self) -> None:
        for base in ("server_data", "simulated_remote"):
            shutil.rmtree(self.store / base / self.workspace, ignore_errors=True)

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
            # The server writes to the per-user data directory, which sits
            # outside any workspace sandbox, so it needs filesystem access.
            # Approvals stay enforced; only the filesystem policy is widened.
            "-s",
            "danger-full-access",
            prompt,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(self.project),
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "RUST_LOG": "error",
                # Isolated home: only this test's MCP server is visible.
                "CODEX_HOME": str(self.codex_home),
                # Inherited by the MCP server codex spawns, so a short session
                # still reaches a snapshot boundary.
                SNAPSHOT_CADENCE_ENV: str(E2E_SNAPSHOT_CADENCE),
                CHECKIN_CADENCE_ENV: str(E2E_CHECKIN_CADENCE),
            },
        )
        result = CodexResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        result.tool_calls = [
            line.strip()
            for line in result.combined.splitlines()
            if line.strip().startswith("mcp:")
        ]
        self._assert_model_was_honoured(result)
        return result

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
            if isinstance(record.get("detail"), dict)
            and "per_tool" in record["detail"]
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
            int(record["detail"].get("total_calls") or 0)
            for record in self.counted_records()
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
