import unittest

from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightEngine,
    PreflightInventory,
    PreflightSelections,
    ProbeCandidate,
    SetupUserInput,
)


class MissingProbeGuidanceTests(unittest.TestCase):
    def test_invalid_explicit_probe_cannot_fall_back_to_singleton(self) -> None:
        decision = PreflightEngine().evaluate(
            SetupUserInput("board", "connection", "Board", "MCU", None, requires_uart=False),
            PreflightInventory(probes=(ProbeCandidate("current", "Probe", "jlink"),)),
            PreflightSelections(probe_id="absent"),
        )
        self.assertEqual(decision.code, "setup/probe-selection-invalid")
        self.assertIsNone(decision.selected_probe)

    def test_missing_probe_guidance_checks_the_locked_pyocd_environment(self) -> None:
        decision = PreflightEngine().evaluate(
            SetupUserInput(
                "board",
                "connection",
                "Board",
                "MCU",
                None,
                requires_uart=False,
            ),
            PreflightInventory(),
        )

        self.assertEqual(decision.code, "setup/no-probe")
        self.assertIn("uv run --locked python -m pyocd list --probes", decision.agent_prompt)
