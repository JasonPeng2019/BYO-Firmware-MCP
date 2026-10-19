"""Implementer checks for returned setup forms; independent B01-T is still required."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from tests.test_tiered_acceptance_lite import (
    _accepted_setup_plan,
    _batch_payload,
    _child_setup_response,
    _minimal_local_pdf,
    _ready_setup_preflight,
    _setup_inventory,
)
from tests.tiered_acceptance_support import (
    DeterministicTargetBackend,
    isolated_project,
    public_text,
    tiered_test_server,
)


class SetupFormTests(unittest.TestCase):
    def test_public_lite_example_is_admitted_and_omissions_name_fields(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            server.configure_tiered_test_seams(backend=DeterministicTargetBackend())
            with (
                patch.object(server, "_hardware_inventory", _setup_inventory()),
                patch.object(
                    server._setup_workflow,
                    "inventory_provider",
                    return_value=_ready_setup_preflight(),
                ),
            ):
                board_id, _, plan = _accepted_setup_plan(
                    self,
                    server,
                    display_name="Returned form lite",
                    target_tier="setup-lite",
                    datasheet=_minimal_local_pdf(root, "lite.pdf"),
                )
                call = plan["stable_client_fallback"]
                pending = _child_setup_response(
                    _batch_payload(public_text(server, call["tool_name"], call["arguments"]))
                )
                form = pending["accepted_response"]
                response = form["response"]
                self.assertEqual(response, form["response_example"])
                region_schema = form["response_schema"]["properties"]["regions"]["items"]
                self.assertEqual(set(response["regions"][0]), set(region_schema["required"]))
                for path, extra in (
                    ("regions", "note"),
                    ("flash", "target"),
                    ("recovery", "pages"),
                    ("response", "unknown"),
                ):
                    malformed = copy.deepcopy(response)
                    if path == "regions":
                        obj, missing = malformed["regions"][0], "source_note"
                        expected = "regions[0].source_note"
                    elif path == "flash":
                        obj, missing, expected = (
                            malformed["flash"],
                            "backend_target",
                            "flash.backend_target",
                        )
                    elif path == "recovery":
                        malformed["recovery"] = {
                            "mechanism": "backend_mass_erase",
                            "source_pages": [1],
                            "source_note": "Documented native recovery",
                        }
                        obj, missing, expected = (
                            malformed["recovery"],
                            "source_pages",
                            "recovery.source_pages",
                        )
                    else:
                        obj, missing, expected = malformed, "recovery", "response.recovery"
                    obj[extra] = obj.pop(missing)
                    with self.subTest(path=path), self.assertRaises(Exception) as refused:
                        public_text(
                            server,
                            "continue_setup",
                            {
                                "board_id": board_id,
                                "continuation_id": pending["continuation_id"],
                                "response": malformed,
                            },
                        )
                    self.assertIn(expected, str(refused.exception))
                    self.assertIn(extra, str(refused.exception))
                accepted = json.loads(
                    public_text(
                        server,
                        "continue_setup",
                        {
                            "board_id": board_id,
                            "continuation_id": pending["continuation_id"],
                            "response": response,
                        },
                    )
                )
                self.assertEqual(accepted["accepted"], "lite_confirmation")

    def test_public_installed_alternative_is_admitted_with_evidence_and_live_check(self) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend(read_value=0x410FC241)
            server.configure_tiered_test_seams(backend=backend)
            with (
                patch.object(server, "_hardware_inventory", _setup_inventory()),
                patch.object(
                    server, "_built_in_target_names", return_value={"nrf52840", "cortex_m"}
                ),
            ):
                board_id, _, plan = _accepted_setup_plan(
                    self,
                    server,
                    display_name="Installed form full",
                    target_tier="setup-full",
                    datasheet=_minimal_local_pdf(root, "full.pdf"),
                    mcu_part_number="nRF52840-QIAA",
                )
                call = plan["stable_client_fallback"]
                pending = _child_setup_response(
                    _batch_payload(public_text(server, call["tool_name"], call["arguments"]))
                )
                form = pending["accepted_response"]
                self.assertIn("source_path", form["response"])
                alternative = form["alternatives"][0]
                self.assertEqual(alternative["eligible_targets"], ["nrf52840"])
                response = alternative["response"]
                response["pyocd_target"] = alternative["eligible_targets"][0]
                response["evidence"] = [
                    {"source": "official test mapping", "claim": "Supports the supplied exact part"}
                ]
                response["reasoning_summary"] = (
                    "Use documented installed support for the unchanged exact part."
                )
                accepted = json.loads(
                    public_text(
                        server,
                        "continue_setup",
                        {
                            "board_id": board_id,
                            "continuation_id": pending["continuation_id"],
                            "response": response,
                        },
                    )
                )
                self.assertEqual(accepted["status"], "setup_continuation_accepted")
                self.assertTrue(any(row[0] == "open" for row in backend.calls))
                self.assertTrue(any(row[0] == "read_memory" for row in backend.calls))
                self.assertFalse(any(row[0] in {"mass_erase", "program"} for row in backend.calls))

    def test_core_only_installed_support_offers_pack_and_refuses_target_before_live_io(
        self,
    ) -> None:
        with isolated_project() as root, tiered_test_server(root) as server:
            backend = DeterministicTargetBackend()
            server.configure_tiered_test_seams(backend=backend)
            with (
                patch.object(server, "_hardware_inventory", _setup_inventory()),
                patch.object(server, "_built_in_target_names", return_value={"cortex_m"}),
            ):
                board_id, _, plan = _accepted_setup_plan(
                    self,
                    server,
                    display_name="Pack form full",
                    target_tier="setup-full",
                    datasheet=_minimal_local_pdf(root, "pack.pdf"),
                    mcu_part_number="STM32L476RGT6",
                )
                call = plan["stable_client_fallback"]
                pending = _child_setup_response(
                    _batch_payload(public_text(server, call["tool_name"], call["arguments"]))
                )
                form = pending["accepted_response"]
                self.assertNotIn("alternatives", form)
                pack_fields = {
                    "pack_id",
                    "version",
                    "filename",
                    "url",
                    "source_path",
                    "official_sha256",
                    "evidence",
                    "reasoning_summary",
                }
                self.assertEqual(set(form["response"]), pack_fields)
                refused = json.loads(
                    public_text(
                        server,
                        "continue_setup",
                        {
                            "board_id": board_id,
                            "continuation_id": pending["continuation_id"],
                            "response": {
                                "pyocd_target": "cortex_m",
                                "evidence": [
                                    {"source": "official test source", "claim": "core only"}
                                ],
                                "reasoning_summary": "No physical memory geometry is available.",
                            },
                        },
                    )
                )
                self.assertEqual(refused["status"], "setup_research_required")
                self.assertEqual(set(refused["exact_response_fields"]), pack_fields)
                self.assertEqual(backend.calls, [])
