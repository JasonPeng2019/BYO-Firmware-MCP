"""M18: exercise the real request fixture's pin assertion without a client request."""

import unittest

import pytest

from tests import codex_harness as harness


pytestmark = pytest.mark.mvp


class ModelPinningIsEnforced(unittest.TestCase):
    def test_wrong_model_or_effort_refuses_and_exact_pair_accepts(self):
        # Use the maintained assertion directly, without the live base class's
        # setUpClass/setUp, sign-in check, store fixture, or run_agent.
        assert_pin = harness.CodexAgentTestCase._assert_model_was_honoured
        for model, effort in (
            ("another-model", harness.REQUIRED_EFFORT),
            (harness.REQUIRED_MODEL + "-other", harness.REQUIRED_EFFORT),
            (harness.REQUIRED_MODEL, "high"),
        ):
            result = harness.CodexResult(
                returncode=0,
                stdout="",
                stderr=f"model: {model}\nreasoning effort: {effort}\n",
            )
            with self.subTest(model=model, effort=effort):
                with self.assertRaises(AssertionError):
                    assert_pin(self, result)
        correct = harness.CodexResult(
            returncode=0,
            stdout="",
            stderr=(
                f"model: {harness.REQUIRED_MODEL}\n"
                f"reasoning effort: {harness.REQUIRED_EFFORT}\n"
            ),
        )
        assert_pin(self, correct)
