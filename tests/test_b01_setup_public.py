"""Independent M04 public-form consumers; no schema or admission mocks."""

from __future__ import annotations

import copy
from contextlib import ExitStack
import json
import re
import unittest
import zipfile
from unittest.mock import patch

from pyocd.target.builtin import BUILTIN_TARGETS

from pyocd_debug_mcp.firmstore.profiles import ProfileError

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


class SetupPublicTests(unittest.TestCase):
    def fixture(
        self, *, lite=False, targets=None, part="nRF52840-QIAA", cpuid=0x410FC241, cleanup=None
    ):
        cleanup = cleanup or self.addCleanup
        project = isolated_project()
        root = project.__enter__()
        cleanup(project.__exit__, None, None, None)
        context = tiered_test_server(root)
        server = context.__enter__()
        cleanup(context.__exit__, None, None, None)
        backend = DeterministicTargetBackend(read_value=cpuid)
        server.configure_tiered_test_seams(backend=backend)
        inventory = patch.object(server, "_hardware_inventory", _setup_inventory())
        inventory.start()
        cleanup(inventory.stop)
        dependency = (
            patch.object(
                server._setup_workflow, "inventory_provider", return_value=_ready_setup_preflight()
            )
            if lite
            else patch.object(
                server, "_built_in_target_names", return_value=targets or {"nrf52840", "cortex_m"}
            )
        )
        dependency.start()
        cleanup(dependency.stop)
        board, _, plan = _accepted_setup_plan(
            self,
            server,
            display_name="Independent B01 setup",
            target_tier="setup-lite" if lite else "setup-full",
            datasheet=_minimal_local_pdf(root, "official-fixture.pdf"),
            mcu_part_number=part,
        )
        fallback = plan["stable_client_fallback"]
        pending = _child_setup_response(
            _batch_payload(public_text(server, fallback["tool_name"], fallback["arguments"]))
        )
        return root, server, backend, board, pending

    @staticmethod
    def submit(server, board, pending, response):
        return json.loads(
            public_text(
                server,
                "continue_setup",
                {
                    "board_id": board,
                    "continuation_id": pending["continuation_id"],
                    "response": response,
                },
            )
        )

    def installed_response(self):
        # Obtain the installed envelope from a compatible public form before
        # creating the request that must refuse this otherwise valid response.
        with ExitStack() as scope:
            _, _, _, _, compatible = self.fixture(cleanup=scope.callback)
            alternative = copy.deepcopy(compatible["accepted_response"]["alternatives"][0])
        self.assertEqual(alternative["eligible_targets"], ["nrf52840"])
        response = copy.deepcopy(alternative["response"])
        response.update(
            pyocd_target=alternative["eligible_targets"][0],
            evidence=[
                {
                    "source": "Controlled official support fixture",
                    "claim": "The installed target supports the supplied part.",
                }
            ],
            reasoning_summary="Research claims support; admission must check installed metadata.",
        )
        return response

    def assert_pack_refusal_without_admission(self, server, backend, board, pending, response):
        reply = self.submit(server, board, pending, response)
        self.assertEqual(reply["status"], "setup_research_required", reply)
        self.assertEqual(
            set(reply["exact_response_fields"]), set(pending["accepted_response"]["response"])
        )
        self.assertEqual(reply["continuation_id"], pending["continuation_id"])
        self.assertIn("CMSIS-Pack", reply["agent_prompt"])
        self.assertEqual(
            reply["rejected_candidates"][0]["observed"], {"research_route": "official_pack"}
        )
        self.assertEqual(backend.calls, [], "Wrong support must be refused before attachment")
        for candidates in (
            server._setup_builtin_candidates,
            server._setup_target_overrides,
            server._setup_attachment_overrides,
        ):
            self.assertNotIn(board, candidates)
        with self.assertRaises(ProfileError):
            server._profile_repository.load(board)

    def test_full_nested_lite_form_and_each_required_field_error(self):
        _, server, backend, board, pending = self.fixture(lite=True)
        form = pending["accepted_response"]
        response = copy.deepcopy(form["response_example"])
        schema = form["response_schema"]
        self.assertEqual(set(response), set(schema["required"]))
        # Every name comes from the returned schema/example. Facts below describe
        # a synthetic one-sector device, never either attached physical board.
        region = copy.deepcopy(response["regions"][0])
        kinds = schema["properties"]["regions"]["items"]["properties"]["kind"]["enum"]
        self.assertIn("physical_flash", kinds)
        region.update(
            name="controlled physical flash",
            kind="physical_flash",
            start=0,
            end=4096,
            readable=True,
            writable=False,
            executable=False,
            source_pages=[2],
            source_note="Synthetic official fixture: full physical flash span.",
        )
        response["regions"].append(region)
        response["flash"]["backend_target"] = "nrf52840"
        sector_schema = schema["properties"]["flash"]["properties"]["erase_sectors"]["items"]
        response["flash"]["erase_sectors"] = [
            {key: {"start": 0, "end": 4096}[key] for key in sector_schema["required"]}
        ]
        recovery_schema = next(
            item for item in schema["properties"]["recovery"]["anyOf"] if item["type"] == "object"
        )
        values = {
            "mechanism": recovery_schema["properties"]["mechanism"]["const"],
            "source_pages": [3],
            "source_note": "Synthetic official fixture: native recovery covers physical flash.",
        }
        response["recovery"] = {key: values[key] for key in recovery_schema["required"]}
        objects = (
            ("response", schema, lambda r: r),
            ("regions[0]", schema["properties"]["regions"]["items"], lambda r: r["regions"][0]),
            ("flash", schema["properties"]["flash"], lambda r: r["flash"]),
            ("recovery", recovery_schema, lambda r: r["recovery"]),
        )
        before = list(backend.calls)
        for path, object_schema, get in objects:
            for key in object_schema["required"]:
                malformed = copy.deepcopy(response)
                del get(malformed)[key]
                with (
                    self.subTest(missing=f"{path}.{key}"),
                    self.assertRaisesRegex(Exception, re.escape(f"{path}.{key}")),
                ):
                    self.submit(server, board, pending, malformed)
            malformed = copy.deepcopy(response)
            get(malformed)["mistyped_field"] = True
            with self.subTest(extra=path), self.assertRaises(Exception) as error:
                self.submit(server, board, pending, malformed)
            self.assertIn(f"{path}.mistyped_field", str(error.exception))
        self.assertEqual(backend.calls, before)
        accepted = self.submit(server, board, pending, response)
        self.assertEqual(accepted["accepted"], "lite_confirmation")
        self.assertEqual(
            accepted["identity"], {"assertion": "trusted-not-proven", "capability": None}
        )
        # Observe the actual admitted evidence, without replacing its validator.
        confirmation = server._setup_lite_confirmations[board]
        self.assertEqual(confirmation.evidence["confirmed_regions"], response["regions"])
        self.assertEqual(confirmation.evidence["recovery"], response["recovery"])
        self.assertEqual(confirmation.map_snapshot["flash"], response["flash"])

    def test_eligible_installed_form_hits_live_cpuid_boundary_without_claiming_package(self):
        _, server, backend, board, pending = self.fixture()
        form = pending["accepted_response"]
        alternative = form["alternatives"][0]
        response = copy.deepcopy(alternative["response"])
        self.assertEqual(alternative["eligible_targets"], ["nrf52840"])
        response.update(
            pyocd_target=alternative["eligible_targets"][0],
            evidence=[
                {
                    "source": "Controlled official support fixture",
                    "claim": "Installed target supports supplied nRF52840-QIAA.",
                }
            ],
            reasoning_summary="Exact-part support is supplied as research; CPUID only proves core compatibility.",
        )
        accepted = self.submit(server, board, pending, response)
        self.assertEqual(accepted["accepted"], "target")
        self.assertEqual([call[0] for call in backend.calls], ["open", "read_memory", "close"])
        self.assertEqual(backend.calls[1][2:], (0xE000ED00, 32))
        self.assertNotIn(
            "identity", accepted, "Core CPUID must not be reported as exact package proof"
        )

    def test_installed_form_does_not_admit_unrecognized_live_core(self):
        _, server, backend, board, pending = self.fixture(cpuid=0)
        response = copy.deepcopy(pending["accepted_response"]["alternatives"][0]["response"])
        response.update(
            pyocd_target="nrf52840",
            evidence=[
                {
                    "source": "Controlled official support fixture",
                    "claim": "Supports supplied part.",
                }
            ],
            reasoning_summary="Attempt researched installed support with live validation.",
        )
        reply = self.submit(server, board, pending, response)
        self.assertEqual(reply["status"], "setup_research_required")
        self.assertNotIn(board, server._setup_builtin_candidates)
        self.assertEqual(sum(call[0] == "open" for call in backend.calls), 3)
        self.assertEqual(sum(call[0] == "close" for call in backend.calls), 3)

    def test_incompatible_installed_choice_is_absent(self):
        _, server, backend, board, pending = self.fixture(part="STM32L476RGT6")
        form = pending["accepted_response"]
        self.assertNotIn("alternatives", form)
        self.assertEqual(backend.calls, [])
        self.assertNotIn(board, server._setup_builtin_candidates)

    def test_geometry_and_shared_cpuid_do_not_admit_incompatible_installed_support(self):
        response = self.installed_response()
        root, server, backend, board, pending = self.fixture(part="STM32L476RGT6")
        # Deliberately submit even when correctly absent from the offered choices.
        # Positive evidence prose cannot turn Nordic support into STM32 support.
        self.assert_pack_refusal_without_admission(server, backend, board, pending, response)
        self.admit_synthetic_pack(root, server, backend, board, pending)
        self.assertNotIn(board, server._setup_builtin_candidates)

    def test_same_family_device_prefix_does_not_establish_installed_support(self):
        response = self.installed_response()
        _, server, backend, board, pending = self.fixture(
            targets={"nrf52832", "nrf52840", "cortex_m"}, part="nRF52832-QFAA"
        )
        # Real installed metadata, not an invented fixture or a two-vendor rule.
        self.assertEqual(
            BUILTIN_TARGETS["nrf52832"].__module__,
            "pyocd.target.builtin.target_nRF52832_xxAA",
        )
        self.assertIsNot(BUILTIN_TARGETS["nrf52832"], BUILTIN_TARGETS["nrf52840"])
        choices = pending["accepted_response"]["alternatives"][0]["eligible_targets"]
        self.assertEqual(choices, ["nrf52832"])
        self.assert_pack_refusal_without_admission(server, backend, board, pending, response)

    def test_same_family_compatible_installed_control(self):
        _, server, backend, board, pending = self.fixture(
            targets={"nrf52832", "cortex_m"}, part="nRF52832-QFAA"
        )
        alternative = pending["accepted_response"]["alternatives"][0]
        self.assertEqual(alternative["eligible_targets"], ["nrf52832"])
        response = copy.deepcopy(alternative["response"])
        response.update(
            pyocd_target=alternative["eligible_targets"][0],
            evidence=[{"source": "Controlled official source", "claim": "Supports nRF52832-QFAA."}],
            reasoning_summary="Use the installed device pattern for the supplied ordering code.",
        )
        accepted = self.submit(server, board, pending, response)
        self.assertEqual(accepted["accepted"], "target", accepted)
        self.assertEqual(accepted["pyocd_target"], "nrf52832")
        self.assertEqual([call[0] for call in backend.calls], ["open", "read_memory", "close"])
        self.assertEqual(server._setup_builtin_candidates[board].part_number, "nRF52832-QFAA")

    def test_insufficient_installed_support_falls_back_and_public_pack_form_is_usable(self):
        root, server, backend, board, pending = self.fixture(
            targets={"cortex_m"}, part="STM32L476RGT6"
        )
        form = pending["accepted_response"]
        self.assertNotIn("alternatives", form)
        # Submit the documented installed shape from the other public form, with
        # core-only support. The response must provide the complete pack remedy.
        refused = self.submit(
            server,
            board,
            pending,
            {
                "pyocd_target": "cortex_m",
                "evidence": [
                    {
                        "source": "Controlled official source",
                        "claim": "Core-only support lacks flash geometry.",
                    }
                ],
                "reasoning_summary": "Insufficient geometry requires an official device pack.",
            },
        )
        self.assertEqual(refused["status"], "setup_research_required")
        self.assertEqual(set(refused["exact_response_fields"]), set(form["response"]))
        self.assertEqual(backend.calls, [])
        self.admit_synthetic_pack(root, server, backend, board, pending)

    def admit_synthetic_pack(self, root, server, backend, board, pending):
        form = pending["accepted_response"]
        # An actual official-shaped local pack goes through real PDSC binding,
        # quarantine, checksum, live connection and project-local promotion.
        pack = root / "TestVendor.Controlled_DFP.1.0.0.pack"
        with zipfile.ZipFile(pack, "w") as archive:
            archive.writestr(
                "TestVendor.Controlled_DFP.pdsc",
                """<package><vendor>TestVendor</vendor><name>Controlled_DFP</name><description>Controlled fixture</description><url>https://example.test/</url><releases><release version="1.0.0">Fixture</release></releases><devices><family Dfamily="Controlled" Dvendor="TestVendor:1"><processor Dcore="Cortex-M4"/><device Dname="STM32L476RGT6"><memory name="IROM1" start="0x08000000" size="0x100000" access="rx"/><memory name="IRAM1" start="0x20000000" size="0x18000" access="rwx"/></device></family></devices></package>""",
            )
        response = copy.deepcopy(form["response"])
        response.update(
            pack_id="TestVendor.Controlled_DFP",
            version="1.0.0",
            filename=pack.name,
            url="https://example.test/TestVendor.Controlled_DFP.1.0.0.pack",
            source_path=str(pack),
            official_sha256=None,
            evidence=[
                {
                    "source": "Controlled official vendor pack fixture",
                    "claim": "PDSC explicitly names the requested part.",
                }
            ],
            reasoning_summary="Use the disclosed local official-pack route; fixture is synthetic, not vendor hardware proof.",
        )
        accepted = self.submit(server, board, pending, response)
        self.assertEqual(accepted["accepted"], "target_and_pack", accepted)
        self.assertEqual(accepted["pyocd_target"], "stm32l476rgt6")
        self.assertTrue(
            any(call[0] == "open" and call[1]["pack_path"] is not None for call in backend.calls)
        )


if __name__ == "__main__":
    unittest.main()
