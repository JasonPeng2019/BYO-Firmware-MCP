"""Retry-ticket bounds, expiry, and wrong-kind refusal.

The refusals here matter because they happen *before* anything runs. A wrong-kind or
expired ticket that still executed hooks would spend real subprocess time on a request
the server has already decided to reject, on the exact code path an agent hits while
flailing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.discovery_failures import CONTRACT_TOOL, REFRESH_TOOL
from pyocd_debug_mcp.discovery_hooks import (
    DiscoveryHookSnapshot,
    HookExecution,
    execute_eligible_hooks,
    load_hook_snapshot,
)
from pyocd_debug_mcp.tools.discovery import (
    MAX_RETRY_CONTEXTS,
    RETRY_TTL_SECONDS,
    DiscoveryRetryStore,
    DiscoveryToolServices,
    ExpiredRetry,
    WrongKindRetry,
    build_discovery_handlers,
)
from tests.discovery_hook_fixtures import hook_entry, write_manifest


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StoreMechanicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.counter = 0

        def token() -> str:
            self.counter += 1
            return f"ticket-{self.counter:04d}"

        self.store = DiscoveryRetryStore("run-1", clock=self.clock, token_factory=token)

    def test_issued_ticket_is_claimable_for_its_own_kind(self) -> None:
        ticket = self.store.issue("probe")

        claimed = self.store.claim(ticket.retry_id, kind="probe")

        self.assertEqual(claimed.retry_id, ticket.retry_id)
        self.assertEqual(claimed.kind, "probe")
        self.assertEqual(claimed.run_id, "run-1")

    def test_wrong_kind_claim_is_refused(self) -> None:
        ticket = self.store.issue("probe")

        with self.assertRaises(WrongKindRetry) as caught:
            self.store.claim(ticket.retry_id, kind="uart")

        self.assertIn("'probe'", str(caught.exception))
        self.assertIn(CONTRACT_TOOL, str(caught.exception))
        # A refused wrong-kind claim does not consume the ticket.
        self.assertTrue(self.store.known(ticket.retry_id))

    def test_unknown_ticket_is_refused(self) -> None:
        with self.assertRaises(ExpiredRetry):
            self.store.claim("never-issued")

    def test_expired_ticket_is_refused_and_evicted(self) -> None:
        ticket = self.store.issue("probe")
        self.clock.advance(RETRY_TTL_SECONDS + 1)

        with self.assertRaises(ExpiredRetry) as caught:
            self.store.claim(ticket.retry_id)

        self.assertIn("expired", str(caught.exception))
        self.assertFalse(self.store.known(ticket.retry_id))

    def test_ticket_just_inside_the_ttl_is_still_valid(self) -> None:
        ticket = self.store.issue("probe")
        self.clock.advance(RETRY_TTL_SECONDS - 0.001)

        self.assertEqual(self.store.claim(ticket.retry_id).retry_id, ticket.retry_id)

    def test_inserting_past_the_cap_evicts_the_oldest_not_a_random_one(self) -> None:
        issued = [self.store.issue("probe").retry_id for _ in range(MAX_RETRY_CONTEXTS)]
        self.assertEqual(self.store.count(), MAX_RETRY_CONTEXTS)

        extra = self.store.issue("probe").retry_id

        self.assertEqual(self.store.count(), MAX_RETRY_CONTEXTS)
        self.assertFalse(self.store.known(issued[0]), "the oldest ticket was not evicted")
        for survivor in issued[1:]:
            self.assertTrue(self.store.known(survivor))
        self.assertTrue(self.store.known(extra))

    def test_eviction_order_is_strictly_oldest_first(self) -> None:
        issued = [self.store.issue("probe").retry_id for _ in range(MAX_RETRY_CONTEXTS)]

        for index in range(5):
            self.store.issue("probe")
            self.assertEqual(
                self.store.retry_ids()[0],
                issued[index + 1],
                "eviction did not advance one ticket at a time",
            )

    def test_consuming_a_ticket_makes_it_unusable(self) -> None:
        ticket = self.store.issue("probe")

        self.store.consume(ticket.retry_id)

        with self.assertRaises(ExpiredRetry):
            self.store.claim(ticket.retry_id)

    def test_consuming_an_unknown_ticket_is_harmless(self) -> None:
        self.store.consume("never-issued")

        self.assertEqual(self.store.count(), 0)

    def test_ticket_carries_the_original_call_and_board(self) -> None:
        ticket = self.store.issue(
            "probe",
            retry_tool="setup_overview",
            retry_arguments={"board_names": ["Nucleo"]},
            board_id="nucleo_1",
        )

        self.assertEqual(
            ticket.retry_call(),
            {"tool": "setup_overview", "arguments": {"board_names": ["Nucleo"]}},
        )
        self.assertEqual(ticket.board_id, "nucleo_1")

    def test_a_ticket_without_a_tool_has_no_retry_call(self) -> None:
        self.assertIsNone(self.store.issue("probe").retry_call())

    def test_retry_arguments_are_copied_not_aliased(self) -> None:
        arguments = {"board_names": ["Nucleo"]}
        ticket = self.store.issue("probe", retry_tool="setup_overview", retry_arguments=arguments)

        arguments["board_names"].append("Injected")  # type: ignore[union-attr]

        call = ticket.retry_call()
        assert call is not None
        self.assertEqual(call["arguments"], {"board_names": ["Nucleo"]})

    def test_real_tokens_are_unguessable_and_distinct(self) -> None:
        store = DiscoveryRetryStore("run-2")

        tokens = {store.issue("probe").retry_id for _ in range(20)}

        self.assertEqual(len(tokens), 20)
        for token in tokens:
            self.assertGreaterEqual(len(token), 16)


class HandlerRefusalTests(unittest.TestCase):
    """Refusals must not execute a hook. Verified by counting launches."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        write_manifest(
            self.root,
            [
                hook_entry("probe-hook", "probe", argv=["probe"]),
                hook_entry("uart-hook", "uart", argv=["uart"]),
            ],
        )
        self.clock = _FakeClock()
        self.store = DiscoveryRetryStore("run-1", clock=self.clock)
        self.launches: list[str] = []
        self.replaced: list[DiscoveryHookSnapshot] = []

        def run_hooks(
            snapshot: DiscoveryHookSnapshot, kind: str
        ) -> Sequence[HookExecution]:
            self.launches.append(kind)
            return execute_eligible_hooks(snapshot, kind)

        self.handlers = build_discovery_handlers(
            DiscoveryToolServices(
                hook_root=lambda: self.root,
                load_snapshot=lambda: load_hook_snapshot(self.root, environ={}),
                current_snapshot=lambda: discovery_hooks.EMPTY_SNAPSHOT,
                replace_snapshot=lambda snapshot: (
                    self.replaced.append(snapshot) or snapshot
                ),
                retry_store=self.store,
                registered_providers=lambda: ("cmsisdap",),
                run_hooks=run_hooks,
            )
        )

    def contract(self, kind: str, retry_id: str | None = None) -> dict[str, Any]:
        return json.loads(self.handlers[CONTRACT_TOOL](kind, retry_id))

    def refresh(self, retry_id: str | None = None) -> dict[str, Any]:
        return json.loads(self.handlers[REFRESH_TOOL](retry_id))

    def test_a_probe_ticket_presented_for_a_uart_contract_is_refused(self) -> None:
        ticket = self.store.issue("probe")

        payload = self.contract("uart", ticket.retry_id)

        self.assertEqual(payload["status"], "discovery_contract_rejected")
        self.assertEqual(payload["code"], "discovery/retry-ticket-invalid")
        self.assertFalse(payload["executable"])
        self.assertNotIn("refresh_call", payload)
        self.assertEqual(self.launches, [], "a refused wrong-kind ticket executed a hook")

    def test_a_refused_wrong_kind_ticket_runs_nothing_through_refresh_either(self) -> None:
        ticket = self.store.issue("probe")
        # The ticket is valid for refresh (which is kind-agnostic), so prove the
        # contract-level refusal is what stops execution, not luck.
        payload = self.contract("uart", ticket.retry_id)

        self.assertEqual(payload["status"], "discovery_contract_rejected")
        self.assertEqual(self.launches, [])
        self.assertEqual(self.replaced, [], "a refused request reloaded the manifest")

    def test_an_expired_ticket_is_refused_without_executing_a_hook(self) -> None:
        ticket = self.store.issue("probe")
        self.clock.advance(RETRY_TTL_SECONDS + 1)

        payload = self.refresh(ticket.retry_id)

        self.assertEqual(payload["status"], "discovery_refresh_rejected")
        self.assertEqual(payload["code"], "discovery/retry-ticket-invalid")
        self.assertEqual(self.launches, [], "an expired ticket executed a hook")
        self.assertEqual(self.replaced, [])

    def test_an_unknown_ticket_is_refused_without_executing_a_hook(self) -> None:
        payload = self.refresh("never-issued")

        self.assertEqual(payload["status"], "discovery_refresh_rejected")
        self.assertEqual(self.launches, [])

    def test_a_successful_refresh_clears_its_own_ticket(self) -> None:
        ticket = self.store.issue("probe", retry_tool="setup_overview")

        payload = self.refresh(ticket.retry_id)

        self.assertEqual(payload["status"], "discovery_hooks_refreshed")
        self.assertEqual(sorted(self.launches), ["probe", "uart"])
        self.assertFalse(self.store.known(ticket.retry_id))

    def test_replaying_a_consumed_ticket_is_refused_and_runs_nothing(self) -> None:
        ticket = self.store.issue("probe", retry_tool="setup_overview")
        self.refresh(ticket.retry_id)
        launches_after_first = list(self.launches)

        payload = self.refresh(ticket.retry_id)

        self.assertEqual(payload["status"], "discovery_refresh_rejected")
        self.assertEqual(
            self.launches, launches_after_first, "a replayed ticket executed hooks again"
        )

    def test_a_failing_refresh_keeps_its_ticket_for_another_attempt(self) -> None:
        write_manifest(self.root, [hook_entry("bad-hook", "probe", argv=["nonzero"])])
        ticket = self.store.issue("probe", retry_tool="setup_overview")

        payload = self.refresh(ticket.retry_id)

        self.assertEqual(payload["status"], "discovery_hooks_partial")
        self.assertEqual(payload["retry_id"], ticket.retry_id)
        self.assertEqual(payload["refresh_call"]["arguments"]["retry_id"], ticket.retry_id)
        self.assertTrue(
            self.store.known(ticket.retry_id), "a repairable failure discarded the ticket"
        )

    def test_null_retry_id_is_accepted_for_inspection_and_omits_refresh_call(self) -> None:
        payload = self.contract("probe", None)

        self.assertEqual(payload["status"], "discovery_hook_contract")
        self.assertFalse(payload["executable"])
        self.assertNotIn("refresh_call", payload)
        self.assertNotIn("retry_id", payload)
        self.assertEqual(self.launches, [], "contract inspection executed a hook")

    def test_a_valid_ticket_makes_the_contract_executable_with_a_refresh_call(self) -> None:
        ticket = self.store.issue(
            "probe", retry_tool="setup_overview", retry_arguments={"board_names": ["N"]}
        )

        payload = self.contract("probe", ticket.retry_id)

        self.assertTrue(payload["executable"])
        self.assertEqual(payload["retry_id"], ticket.retry_id)
        self.assertEqual(payload["refresh_call"]["tool"], REFRESH_TOOL)
        self.assertEqual(payload["refresh_call"]["arguments"]["retry_id"], ticket.retry_id)
        self.assertEqual(payload["original_call"]["tool"], "setup_overview")
        self.assertEqual(self.launches, [], "the contract tool executed a hook")

    def test_a_valid_ticket_is_not_consumed_by_contract_inspection(self) -> None:
        ticket = self.store.issue("probe")

        self.contract("probe", ticket.retry_id)

        self.assertTrue(self.store.known(ticket.retry_id))

    def test_refresh_returns_the_captured_original_call(self) -> None:
        ticket = self.store.issue(
            "probe",
            retry_tool="setup_overview",
            retry_arguments={"board_names": ["Nucleo"]},
            board_id="nucleo_1",
        )

        payload = self.refresh(ticket.retry_id)

        self.assertEqual(
            payload["retry_call"],
            {"tool": "setup_overview", "arguments": {"board_names": ["Nucleo"]}},
        )
        self.assertEqual(payload["board_id"], "nucleo_1")

    def test_a_malformed_manifest_leaves_the_previous_snapshot_untouched(self) -> None:
        (self.root / discovery_hooks.MANIFEST_FILENAME).write_text("{bad", encoding="utf-8")

        payload = self.refresh()

        self.assertEqual(payload["status"], "discovery_refresh_rejected")
        self.assertEqual(payload["code"], "discovery/manifest-invalid")
        self.assertIn("manifest_schema", payload)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.replaced, [], "a rejected manifest replaced the live snapshot")

    def test_refresh_with_no_hooks_declared_runs_nothing(self) -> None:
        write_manifest(self.root, [])

        payload = self.refresh()

        self.assertEqual(payload["status"], "discovery_hooks_absent")
        self.assertEqual(payload["hooks"], [])
        self.assertEqual(self.launches, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
