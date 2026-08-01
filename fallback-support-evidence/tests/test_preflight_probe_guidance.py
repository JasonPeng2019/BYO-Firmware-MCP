import unittest

from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightEngine,
    PreflightInventory,
    SetupUserInput,
)


class MissingProbeGuidanceTests(unittest.TestCase):
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
