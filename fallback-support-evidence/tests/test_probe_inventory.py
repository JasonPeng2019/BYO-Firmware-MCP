"""FIX 13 (M2): direct coverage for `list_connected_probes_detailed`.

Step 3's named deliverable had zero direct tests before this file -- every existing
test either constructed a `NativeProbeListing` by hand or exercised the CLI path only
incidentally through a higher-level flow. This covers the function's own contract:
which attempt's diagnostics survive, exit-code classification, summarization, and the
`list_connected_probes_cli` backward-compatibility wrapper.
"""

from __future__ import annotations

import unittest
from typing import Sequence
from unittest.mock import patch

from pyocd_debug_mcp import probe_inventory
from pyocd_debug_mcp.probe_inventory import (
    EMPTY_NATIVE_PROBE_LISTING,
    MAX_PROBE_SUMMARY_CHARS,
    PROBE_CLI_TIMEOUT_EXIT_CODE,
    list_connected_probes_cli,
    list_connected_probes_detailed,
)

PROBE_TABLE = (
    "#   Probe/Board          Unique ID                  State\n"
    "-------------------------------------------------------\n"
    "0   J-Link probe          683710208                  \n"
)
NO_PROBES_TABLE = "No available debug probes were found.\n"


class _FakeRunner:
    """Replays a fixed sequence of (exit_code, stdout, stderr) results in call order."""

    def __init__(self, results: Sequence[tuple[int, str, str]]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.calls.append(tuple(command))
        return self._results[len(self.calls) - 1]


def _with_commands(*commands: tuple[str, ...]):
    return patch.object(probe_inventory, "configured_probe_cli_commands", return_value=commands)


class DetailedListingTests(unittest.TestCase):
    def test_no_commands_configured_returns_the_empty_sentinel(self) -> None:
        with _with_commands():
            listing = list_connected_probes_detailed(lambda command: (0, "", ""))

        self.assertEqual(listing, EMPTY_NATIVE_PROBE_LISTING)

    def test_probes_found_on_the_first_configured_command(self) -> None:
        runner = _FakeRunner([(0, PROBE_TABLE, "")])
        with _with_commands(("pyocd", "list", "--probes")):
            listing = list_connected_probes_detailed(runner)

        self.assertEqual(len(listing.probes), 1)
        self.assertEqual(listing.probes[0].uid, "683710208")
        self.assertEqual(listing.command, ("pyocd", "list", "--probes"))
        self.assertEqual(listing.exit_code, 0)
        self.assertTrue(listing.available)
        self.assertEqual(len(runner.calls), 1, "a second command must not run once one succeeds")

    def test_diagnostics_belong_to_the_successful_attempt_not_the_first(self) -> None:
        """The first configured command parses to zero rows; the second finds probes."""

        runner = _FakeRunner(
            [
                (0, NO_PROBES_TABLE, ""),
                (0, PROBE_TABLE, ""),
            ]
        )
        with _with_commands(("first", "cmd"), ("second", "cmd")):
            listing = list_connected_probes_detailed(runner)

        self.assertEqual(len(listing.probes), 1)
        self.assertEqual(
            listing.command, ("second", "cmd"), "diagnostics must belong to the attempt with rows"
        )
        self.assertEqual(len(runner.calls), 2)

    def test_all_commands_fail_diagnostics_belong_to_the_first_canonical_attempt(self) -> None:
        runner = _FakeRunner(
            [
                (127, "", "command not found: first\n"),
                (127, "", "command not found: second\n"),
            ]
        )
        with _with_commands(("first", "cmd"), ("second", "cmd")):
            listing = list_connected_probes_detailed(runner)

        self.assertEqual(listing.probes, ())
        self.assertEqual(
            listing.command,
            ("first", "cmd"),
            "when nothing produced rows, the FIRST (canonical) attempt's diagnostics win",
        )
        self.assertIn("first", listing.stderr_summary)
        self.assertEqual(len(runner.calls), 2, "every configured fallback must still run")

    def test_timeout_exit_code_sets_timed_out(self) -> None:
        runner = _FakeRunner([(PROBE_CLI_TIMEOUT_EXIT_CODE, "", "")])
        with _with_commands(("cmd",)):
            listing = list_connected_probes_detailed(runner)

        self.assertTrue(listing.timed_out)
        self.assertFalse(listing.available)

    def test_command_not_found_exit_code_does_not_set_timed_out(self) -> None:
        runner = _FakeRunner([(127, "", "command not found")])
        with _with_commands(("cmd",)):
            listing = list_connected_probes_detailed(runner)

        self.assertFalse(listing.timed_out)
        self.assertFalse(listing.available)

    def test_available_is_true_only_for_exit_zero(self) -> None:
        for exit_code in (0, 1, PROBE_CLI_TIMEOUT_EXIT_CODE, 127):
            with self.subTest(exit_code=exit_code):
                runner = _FakeRunner([(exit_code, "", "")])
                with _with_commands(("cmd",)):
                    listing = list_connected_probes_detailed(runner)
                self.assertEqual(listing.available, exit_code == 0)

    def test_stdout_summary_truncates_with_a_marker(self) -> None:
        long_text = "x" * (MAX_PROBE_SUMMARY_CHARS + 500)
        runner = _FakeRunner([(0, long_text, "")])
        with _with_commands(("cmd",)):
            listing = list_connected_probes_detailed(runner)

        self.assertTrue(listing.stdout_summary.endswith("...[truncated]"))
        self.assertLessEqual(
            len(listing.stdout_summary), MAX_PROBE_SUMMARY_CHARS + len("...[truncated]")
        )

    def test_stderr_summary_truncates_with_a_marker(self) -> None:
        long_text = "y" * (MAX_PROBE_SUMMARY_CHARS + 500)
        runner = _FakeRunner([(1, "", long_text)])
        with _with_commands(("cmd",)):
            listing = list_connected_probes_detailed(runner)

        self.assertTrue(listing.stderr_summary.endswith("...[truncated]"))
        self.assertLessEqual(
            len(listing.stderr_summary), MAX_PROBE_SUMMARY_CHARS + len("...[truncated]")
        )

    def test_short_output_is_not_marked_truncated(self) -> None:
        runner = _FakeRunner([(0, PROBE_TABLE, "")])
        with _with_commands(("cmd",)):
            listing = list_connected_probes_detailed(runner)

        self.assertNotIn("...[truncated]", listing.stdout_summary)

    def test_list_connected_probes_cli_returns_a_plain_list_with_identical_contents(self) -> None:
        with _with_commands(("cmd",)):
            probes = list_connected_probes_cli(_FakeRunner([(0, PROBE_TABLE, "")]))
            detailed = list_connected_probes_detailed(_FakeRunner([(0, PROBE_TABLE, "")]))

        self.assertIsInstance(probes, list)
        self.assertEqual(probes, list(detailed.probes))

    def test_diagnostic_row_shape(self) -> None:
        runner = _FakeRunner([(0, PROBE_TABLE, "")])
        with _with_commands(("cmd", "arg")):
            listing = list_connected_probes_detailed(runner)

        row = listing.diagnostic_row()

        self.assertEqual(
            set(row),
            {
                "command",
                "exit_code",
                "timed_out",
                "stdout_summary",
                "stderr_summary",
                "probe_count",
            },
        )
        self.assertEqual(row["command"], ["cmd", "arg"])
        self.assertEqual(row["exit_code"], 0)
        self.assertFalse(row["timed_out"])
        self.assertEqual(row["probe_count"], 1)

    def test_empty_sentinel_diagnostic_row_is_well_formed(self) -> None:
        row = EMPTY_NATIVE_PROBE_LISTING.diagnostic_row()

        self.assertEqual(row["command"], [])
        self.assertIsNone(row["exit_code"])
        self.assertFalse(row["timed_out"])
        self.assertEqual(row["probe_count"], 0)
        self.assertFalse(EMPTY_NATIVE_PROBE_LISTING.available)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
