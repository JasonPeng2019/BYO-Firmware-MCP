"""Phase 0 baseline capture for the issue-monitor plan.

Records what today's ``RegistryFastMCP.call_tool`` actually does — the exception
taxonomy, the normalized refusal text, the advertised surface, and stdout purity —
so the Phase 3b ``call_tool`` restructure can be proven behavior-preserving by diff.

This is a helper, not a test (same convention as ``fake_provider_worker.py``).
It touches no hardware and mutates no state: every scripted call either succeeds
read-only or is refused before backend access.

Run it:

    uv run --locked python tests/baseline_capture.py            # write transcript
    uv run --locked python tests/baseline_capture.py --check    # diff against it

The diffable payload deliberately excludes timings and absolute paths; durations
are recorded separately as informational only, so a slow machine never fails a diff.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

TRANSCRIPT_PATH = Path(__file__).with_name("baseline_transcript.json")
SCHEMA_VERSION = 1

# A board id that cannot resolve to a profile: every guarded route refuses before
# any probe, worker process, or backend call is reached.
ABSENT = "phase0-absent-board"

_NULL_PLAN = {
    "board_id": None,
    "action_parameters": None,
    "hypothesis": None,
    "hypothesis_made": None,
    "strategy": None,
    "strategy_evaluated": None,
    "expected_success_return": None,
    "expected_fail_return": None,
    "max_calls": None,
    "max_calls_buffer": None,
}


def _script() -> list[tuple[str, str, dict[str, Any]]]:
    """Return (label, tool_name, arguments) covering the refusal surface."""

    return [
        # --- successful read-only paths ---
        ("handshake", "initialization_handshake", {}),
        ("overview_empty", "setup_overview", {}),
        ("overview_sentinel", "setup_overview", {"board_names": ["no board"]}),
        ("overview_unknown_name", "setup_overview", {"board_names": ["phase0-unknown"]}),
        # --- unknown board / not connected refusals ---
        ("status_absent", "get_setup_status", {"board_id": ABSENT}),
        ("connect_absent", "connect", {"board_id": ABSENT}),
        ("state_absent", "get_state", {"board_id": ABSENT}),
        ("board_info_absent", "get_board_info", {"board_id": ABSENT}),
        ("validate_absent", "board_validate", {"board_id": ABSENT}),
        ("refresh_absent", "board_safety_refresh", {"board_id": ABSENT}),
        ("disconnect_absent", "disconnect", {"board_id": ABSENT}),
        ("wait_absent", "wait", {"board_id": ABSENT, "ms": 1}),
        ("halt_absent", "halt", {"board_id": ABSENT}),
        ("find_symbol_absent", "find_symbol", {"board_id": ABSENT, "query": "main"}),
        ("cpu_reg_absent", "read_cpu_register", {"board_id": ABSENT, "name": "pc"}),
        (
            "load_setup_tool_absent",
            "load_setup_tool",
            {"board_id": ABSENT, "tool_name": "board_validate"},
        ),
        (
            "continue_setup_bad",
            "continue_setup",
            {"board_id": ABSENT, "continuation_id": "phase0-bogus", "response": "no"},
        ),
        # --- hidden/locked handler refusals (visibility is not authorization) ---
        ("locked_write_memory", "write_memory", {"board_id": ABSENT, "address": 0, "value": 0}),
        (
            "locked_read_memory_address",
            "read_memory_address",
            {"board_id": ABSENT, "address": 0, "size": 4},
        ),
        ("locked_flash_application", "flash_application", {"board_id": ABSENT}),
        ("locked_flash_bootloader", "flash_bootloader", {"board_id": ABSENT}),
        ("locked_target_unlock", "target_unlock", {"board_id": ABSENT}),
        ("locked_set_breakpoint", "set_breakpoint", {"board_id": ABSENT, "address": 0}),
        ("locked_connect_override", "connect_override", {"board_id": ABSENT}),
        ("locked_read_serial", "read_serial", {"board_id": ABSENT}),
        # --- plan envelope: guidance path then the rejection surface ---
        ("plan_all_null", "write_memory-plan", dict(_NULL_PLAN)),
        ("plan_flattened", "write_memory-plan", {"board_id": ABSENT, "address": 0, "value": 0}),
        (
            "plan_extra_key",
            "write_memory-plan",
            {**_NULL_PLAN, "board_id": ABSENT, "phase0_unknown_field": "x"},
        ),
        ("plan_prose", "write_memory-plan", {"board_id": ABSENT, "action_parameters": "please"}),
        (
            "plan_partial_null",
            "write_memory-plan",
            {"board_id": ABSENT, "action_parameters": None, "hypothesis": "partial"},
        ),
        (
            "plan_permission_on_nonpermission",
            "write_memory-plan",
            {**_NULL_PLAN, "board_id": ABSENT, "user_permission": "granted"},
        ),
        # --- batch structural refusals ---
        (
            "batch_nested",
            "action_batch",
            {
                "board_id": ABSENT,
                "actions": [{"tool_name": "action_batch", "arguments": {"board_id": ABSENT}}],
            },
        ),
        (
            "batch_child_no_board",
            "action_batch",
            {"board_id": ABSENT, "actions": [{"tool_name": "get_state", "arguments": {}}]},
        ),
        (
            "batch_board_mismatch",
            "action_batch",
            {
                "board_id": ABSENT,
                "actions": [
                    {"tool_name": "get_state", "arguments": {"board_id": "phase0-other-board"}}
                ],
            },
        ),
        ("batch_empty", "action_batch", {"board_id": ABSENT, "actions": []}),
        # --- unknown tool + collector validation ---
        ("unknown_tool", "phase0_no_such_tool", {"board_id": ABSENT}),
        (
            "collector_missing_input",
            "collect_build_artifacts",
            {
                "output_dir": "phase0-collector-out",
                "elf_path": "phase0-does-not-exist.elf",
                "expected_roles": ["elf"],
            },
        ),
    ]


_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"run-\d{8}T\d{6}Z-[0-9a-f]+"), "<RUN_ID>"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<WINPATH>"),
    (re.compile(r"(?<![\w-])/(?:[\w.-]+/)+[\w.-]+"), "<POSIXPATH>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE), "<HEX>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<ADDR>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+Z?\b"), "<TIMESTAMP>"),
    (re.compile(r"\b\d+\.\d+\b"), "<FLOAT>"),
    (re.compile(r"\b\d+\b"), "<N>"),
    (re.compile(r"[ \t]+"), " "),
)


def normalize(text: str) -> str:
    """Mask run-, host-, and time-varying detail so the transcript is diffable."""

    for pattern, replacement in _NORMALIZERS:
        text = pattern.sub(replacement, text)
    return text.strip()


def _describe(exc: BaseException) -> dict[str, Any]:
    chain: list[str] = []
    cause = exc.__cause__
    while cause is not None and len(chain) < 4:
        chain.append(f"{type(cause).__module__}.{type(cause).__name__}")
        cause = cause.__cause__
    return {
        "outcome": "exception",
        "exception_type": type(exc).__name__,
        "exception_module": type(exc).__module__,
        "exception_mro": [base.__name__ for base in type(exc).__mro__[1:5]],
        "cause_chain": chain,
        "message_normalized": normalize(str(exc)),
    }


async def _run_script(mcp: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []
    for label, tool, arguments in _script():
        buffer = io.StringIO()
        started = time.perf_counter()
        with contextlib.redirect_stdout(buffer):
            try:
                result = await mcp.call_tool(tool, dict(arguments))
                record: dict[str, Any] = {
                    "outcome": "success",
                    "result_kind": type(result).__name__,
                    "result_normalized_prefix": normalize(str(result))[:240],
                }
            except BaseException as exc:  # noqa: BLE001 - taxonomy capture is the point
                record = _describe(exc)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        leaked = buffer.getvalue()
        entries.append(
            {
                "label": label,
                "tool": tool,
                "arg_keys": sorted(arguments),
                "stdout_leak_bytes": len(leaked),
                **record,
            }
        )
        timings.append({"label": label, "duration_ms": round(elapsed_ms, 2)})
    return entries, timings


def capture() -> dict[str, Any]:
    """Import the server, drive the script, and build the transcript document."""

    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        from pyocd_debug_mcp import server as server_module

        advertised = sorted(tool.name for tool in anyio.run(server_module.mcp.list_tools))
        entries, timings = anyio.run(_run_script, server_module.mcp)

    taxonomy: dict[str, int] = {}
    for entry in entries:
        key = entry.get("exception_type", "<success>")
        taxonomy[key] = taxonomy.get(key, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "python": platform.python_version(),
        "platform": sys.platform,
        "import_stdout_leak_bytes": len(quiet.getvalue()),
        "advertised_tools": advertised,
        "advertised_tool_count": len(advertised),
        "taxonomy": dict(sorted(taxonomy.items())),
        "entries": entries,
        "_informational_timings": timings,
    }


def diffable(document: dict[str, Any]) -> dict[str, Any]:
    """Return only the parts that must not change across the Phase 3b restructure."""

    return {
        "advertised_tools": document["advertised_tools"],
        "taxonomy": document["taxonomy"],
        "entries": document["entries"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare a fresh capture against the stored transcript instead of writing it",
    )
    args = parser.parse_args()
    document = capture()

    if not args.check:
        TRANSCRIPT_PATH.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        sys.stderr.write(f"wrote {TRANSCRIPT_PATH.name}: {len(document['entries'])} entries\n")
        sys.stderr.write(f"taxonomy: {json.dumps(document['taxonomy'])}\n")
        leaks = [e["label"] for e in document["entries"] if e["stdout_leak_bytes"]]
        sys.stderr.write(f"stdout leaks: {leaks or 'none'}\n")
        return 0

    if not TRANSCRIPT_PATH.exists():
        sys.stderr.write(f"missing {TRANSCRIPT_PATH.name}; run without --check first\n")
        return 2

    stored = json.loads(TRANSCRIPT_PATH.read_text(encoding="utf-8"))
    expected = diffable(stored)
    actual = diffable(document)
    if expected == actual:
        sys.stderr.write(f"baseline OK: {len(actual['entries'])} entries unchanged\n")
        return 0

    sys.stderr.write("BASELINE MISMATCH\n")
    for old, new in zip(expected["entries"], actual["entries"]):
        if old != new:
            sys.stderr.write(f"  {old['label']}:\n    was {old}\n    now {new}\n")
    if expected["advertised_tools"] != actual["advertised_tools"]:
        was = set(expected["advertised_tools"])
        now = set(actual["advertised_tools"])
        sys.stderr.write(f"  advertised added={sorted(now - was)} removed={sorted(was - now)}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
