"""Independent M11 public behavior oracles, with only probe I/O replaced.

Registered MCP dispatch, inventory merge, policy resolution, UID selection,
assignment, session promotion, adapter admission and validation are real. The
in-process adapter is a host fixture, not production-worker or physical proof.
The catalog check proves a vendor contradiction, never exact package identity.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from pyocd.core.session import Session

from pyocd_debug_mcp.adapters import swd_pyocd as adapter
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.probe_inventory import NativeProbeListing, ProbeInfo
from pyocd_debug_mcp.safety.map_build import semantic_profile_digest

from tests.tiered_acceptance_support import (
    isolated_project,
    known_good_full_policy,
    public_text,
    tiered_test_server,
    write_json,
)


STM = "stm_saved"
STM_NAME = "Known STM32L476"
STM_UID = "066FFF514988525067233337"
NORDIC_UID = "683854191"
RAM = 0x20000000


def probe(uid: str, provider: str = "jlink") -> ProbeInfo:
    return ProbeInfo(uid, f"{provider} {uid}", "controlled native probe I/O", family=provider)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def stm_profile() -> tuple[dict, dict]:
    """Synthetic reviewed host fixture; its bytes never confer live authority."""
    profile, memory = known_good_full_policy()
    profile.update(
        board_id=STM,
        display_name=STM_NAME,
        mcu_part_number="STM32L476RGT6",
        mcu_family="stm32",
        probe_family="stlink",
        pyocd_target="stm32l476rgtx",
        safety_ref=f".firm/safety/{STM}/memory_map.yaml",
        datasheet_sha256="3" * 64,
        silicon_id_address=0xE0042000,
        silicon_id_expected=0x415,
        silicon_id_mask=0xFFF,
        silicon_id_width_bits=32,
        silicon_id_label="DBGMCU device group",
    )
    memory["board_id"] = STM
    memory["identity"].update(
        mcu_part_number=profile["mcu_part_number"],
        pyocd_target=profile["pyocd_target"],
        reviewed_board_type="stm32l476rg-nucleo",
    )
    memory["source_digests"]["semantic_profile"] = semantic_profile_digest(profile)
    return profile, memory


class ProbeSession:
    """Hardware lifecycle/register boundary; real Session option precedence."""

    def __init__(self, uid: str, catalog: str | None, options: dict, journal: list) -> None:
        self.journal = journal
        self.options = Session(None, options=options, no_config=True).options
        self.probe = SimpleNamespace(
            unique_id=uid,
            associated_board_info=SimpleNamespace(target=catalog),
            assert_reset=lambda value: journal.append(("reset-line", value)),
        )
        self.target = self
        self.state = "HALTED"
        self.part_number = "Cortex-M4"

    def open(self) -> None:
        self.journal.append(("session-open", self.probe.unique_id, self.options.get("auto_unlock")))

    def close(self) -> None:
        self.journal.append(("session-close", self.probe.unique_id))

    def get_state(self) -> Any:
        self.journal.append(("get-state", self.probe.unique_id))
        return SimpleNamespace(name=self.state)

    def read_memory(self, address: int, width: int) -> int:
        self.journal.append(("read", self.probe.unique_id, address, width))
        return 0x415 if address == 0xE0042000 else 0x12345678

    def write_memory(self, *args: Any) -> None:
        self.journal.append(("write", *args))
        raise AssertionError("unexpected hardware mutation")

    def halt(self) -> None:
        self.journal.append(("halt", self.probe.unique_id))
        self.state = "HALTED"

    def reset_and_halt(self) -> None:
        self.journal.append(("reset-and-halt", self.probe.unique_id))
        self.state = "HALTED"

    def reset(self) -> None:
        self.journal.append(("reset", self.probe.unique_id))
        self.state = "RUNNING"


class PublicSelectionProof(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, rows=(), *, known_stm=False, catalog=None, selected_uid=None):
        with isolated_project() as root, tiered_test_server(root) as server:
            policies = CapabilityPolicyRepository(root)
            if known_stm:
                profile, memory = stm_profile()
                write_json(root / ".firm" / "boards" / f"{STM}.json", profile)
                write_json(root / ".firm" / "safety" / STM / "memory_map.yaml", memory)
                policies.commit(
                    STM,
                    Tier.SETUP_FULL,
                    profile_snapshot=profile,
                    map_snapshot=memory,
                    evidence={"scope": "synthetic independent M11 host fixture"},
                )
            server.configure_tiered_test_seams(
                backend=adapter.PyOCDSWDInterface(),
                policy_repository=policies,
            )
            f = SimpleNamespace(server=server, root=root, rows=list(rows), journal=[], wire=[])

            def native_listing(*_args):
                return NativeProbeListing(tuple(f.rows), (), 0, False, "controlled probe I/O", "")

            def choose(**kwargs):
                f.journal.append(("choose", kwargs))
                # This replaces the native probe boundary, not the MCP selector.
                uid = selected_uid or (f.rows[0].uid if f.rows else "unexpected-open")
                if not selected_uid:
                    requested = kwargs["unique_id"] or ""
                    uid = next((r.uid for r in f.rows if r.uid in requested), uid)
                return ProbeSession(uid, catalog, kwargs["options"], f.journal)

            with (
                patch.object(server, "list_connected_probes_detailed", side_effect=native_listing),
                patch.object(server._hardware_inventory, "native_uarts", return_value=[]),
                patch.object(server._hardware_inventory, "vendor_uarts", return_value=[]),
                patch.object(
                    adapter.ConnectHelper, "session_with_chosen_probe", side_effect=choose
                ),
            ):
                try:
                    yield f
                finally:
                    for board in server.connection_manager.assigned_board_ids():
                        self.call(f, "disconnect", {"board_id": board})
                    if path := os.environ.get("M11_JOURNAL"):
                        with Path(path).open("a", encoding="utf-8") as stream:
                            stream.write(
                                json.dumps(
                                    {
                                        "test": self.id(),
                                        "wire": f.wire,
                                        "hardware": f.journal,
                                        "source": str(Path(server.__file__).resolve()),
                                        "source_sha256": hashlib.sha256(
                                            Path(server.__file__).read_bytes()
                                        ).hexdigest(),
                                    },
                                    default=str,
                                )
                                + "\n"
                            )

    def call(self, f, tool, arguments, *, structured=False):
        row = {"tool": tool, "arguments": arguments}
        f.wire.append(row)
        try:
            result = public_text(f.server, tool, arguments)
            row["result"] = json.loads(result) if structured else result
            return row["result"]
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
            raise

    def refused_connect(self, f, arguments, detail):
        before = list(f.journal)
        bindings = canonical(f.server.assignment_store.bindings())
        with self.assertRaises(Exception) as caught:
            self.call(f, "connect", arguments)
        self.assertIn(detail, str(caught.exception))
        self.assertEqual(f.journal, before, "admission refusal reached native probe selection")
        self.assertEqual(canonical(f.server.assignment_store.bindings()), bindings)
        self.assertIsNone(f.server.connection_manager.maybe_connection(arguments["board_id"]))

    def test_unknown_label_needs_intent_even_with_single_probe_or_target(self):
        for rows in ((), (probe(NORDIC_UID),), (probe(NORDIC_UID), probe("683854192"))):
            for selector in ({}, {"probe_uid": None}):
                with self.subTest(rows=rows, selector=selector), self.fixture(rows) as f:
                    self.refused_connect(
                        f,
                        {"board_id": "unknown_label", "target": "cortex_m", **selector},
                        "requires an explicit probe_uid",
                    )

    def test_mistaken_literal_null_or_missing_uid_never_adopts_singleton(self):
        for selector in ("null", " NULL ", "", "   ", "68385419x", "unplugged"):
            with self.subTest(selector=selector), self.fixture((probe(NORDIC_UID),)) as f:
                detail = (
                    "requires an explicit probe_uid"
                    if selector == "null"
                    else "current probe"
                    if selector.strip().lower() in {"null", ""}
                    else "absent or ambiguous"
                )
                self.refused_connect(
                    f, {"board_id": "bench", "probe_uid": selector, "target": "cortex_m"}, detail
                )

    def test_multiple_or_no_probes_require_an_unambiguous_current_selector(self):
        for rows, selector in (
            ((), NORDIC_UID),
            ((probe(NORDIC_UID), probe("683854192")), "68385419"),
        ):
            with self.subTest(rows=rows), self.fixture(rows) as f:
                self.refused_connect(
                    f, {"board_id": "bench", "probe_uid": selector}, "absent or ambiguous"
                )

    def test_explicit_raw_exact_partial_and_provider_selection_reach_exact_backend(self):
        for selector in (NORDIC_UID, "3854191", "jlink:" + NORDIC_UID):
            with (
                self.subTest(selector=selector),
                self.fixture((probe("other"), probe(NORDIC_UID))) as f,
            ):
                self.assertIn(
                    "Connected",
                    self.call(
                        f,
                        "connect",
                        {"board_id": "deliberate_raw", "probe_uid": selector, "target": "cortex_m"},
                    ),
                )
                choose = [row[1] for row in f.journal if row[0] == "choose"]
                self.assertEqual(len(choose), 1)
                self.assertEqual(
                    choose[0]["unique_id"], "jlink:" + NORDIC_UID if ":" in selector else NORDIC_UID
                )
                self.assertFalse(choose[0]["auto_open"])
                self.assertEqual(choose[0]["options"]["target_override"], "cortex_m")
                self.assertIs(choose[0]["options"]["auto_unlock"], False)
                self.assertEqual(
                    [r for r in f.journal if r[0] == "session-open"],
                    [("session-open", NORDIC_UID, False)],
                )
                caps = self.call(
                    f, "get_capabilities", {"board_id": "deliberate_raw"}, structured=True
                )
                self.assertEqual(
                    caps["identity"], {"assertion": "not-asserted", "capability": None}
                )
                read = self.call(
                    f,
                    "read_memory_raw",
                    {"board_id": "deliberate_raw", "address": RAM},
                    structured=True,
                )
                self.assertEqual(read["status"], "ok")
                self.assertEqual(read["result"], "0x12345678")
                self.assertFalse(
                    any(r[0] in {"write", "reset", "reset-and-halt"} for r in f.journal), f.journal
                )
                self.assertFalse(any(r[0] == "reset-line" and r[1] for r in f.journal), f.journal)

    def test_public_overview_intent_assigns_raw_label_and_opaque_token_is_usable(self):
        with self.fixture((probe(NORDIC_UID), probe("other"))) as f:
            discovery = self.call(
                f, "setup_overview", {"board_names": ["Bench raw"]}, structured=True
            )
            self.assertEqual(discovery["status"], "setup_assignment_required")
            selected = next(
                r["connection_id"]
                for r in discovery["connections"]
                if NORDIC_UID in r["connection_id"]
            )
            assigned = self.call(
                f,
                "setup_overview",
                {"board_names": ["Bench raw"], "connection_assignments": {"Bench raw": selected}},
                structured=True,
            )
            board = assigned["routes"][0]["board_id"]
            self.assertIn(
                "Connected", self.call(f, "connect", {"board_id": board, "target": "cortex_m"})
            )
            self.assertEqual(
                [r[1]["unique_id"] for r in f.journal if r[0] == "choose"], [NORDIC_UID]
            )
            self.assertEqual(f.server.assignment_store.connection_for(board), selected)

    def test_known_stm_missing_probe_preserves_profile_binding_and_validation(self):
        with self.fixture((probe(STM_UID, "stlink"),), known_stm=True) as f:
            ready = self.call(f, "setup_overview", {"board_names": [STM_NAME]}, structured=True)
            intended = ready["connections"][0]["connection_id"]
            self.assertEqual(f.server.assignment_store.connection_for(STM), intended)
            profile = f.root / ".firm" / "boards" / f"{STM}.json"
            before_profile = profile.read_bytes()
            before_assignment = canonical(f.server.assignment_store.bindings())
            f.rows = [probe(NORDIC_UID)]
            missing = self.call(f, "setup_overview", {"board_names": [STM_NAME]}, structured=True)
            self.assertEqual(missing["status"], "setup_probe_unavailable")
            route = missing["routes"][0]
            self.assertEqual(route["assigned_connection_id"], intended)
            self.assertIsNone(route["next_tool"])
            self.assertIn("not visible", route["reason"])
            self.assertNotIn("busy", route["reason"].lower())
            self.assertEqual(len(missing["connections"]), 1)
            self.assertIn(NORDIC_UID, missing["connections"][0]["connection_id"])
            self.assertIsNone(missing["connections"][0]["assigned_board_id"])
            self.assertEqual(profile.read_bytes(), before_profile)
            self.assertEqual(canonical(f.server.assignment_store.bindings()), before_assignment)
            self.call(
                f,
                "load_setup_tool",
                {"board_id": STM, "tool_name": "board_validate"},
                structured=True,
            )
            refused = self.call(
                f, "board_validate", {"board_id": STM, "probe_id": intended}, structured=True
            )
            self.assertEqual(refused["code"], "validation/no-probe")
            self.assertEqual(profile.read_bytes(), before_profile)
            self.assertEqual(canonical(f.server.assignment_store.bindings()), before_assignment)
            self.assertEqual(f.journal, [], "missing known binding opened unrelated inventory")

    def test_known_profile_control_connects_without_raw_overrides(self):
        with self.fixture((probe(NORDIC_UID), probe(STM_UID, "stlink")), known_stm=True) as f:
            overview = self.call(f, "setup_overview", {"board_names": [STM_NAME]}, structured=True)
            self.assertEqual(overview["status"], "setup_routes_ready")
            self.assertIn("Connected", self.call(f, "connect", {"board_id": STM}))
            chosen = [r[1] for r in f.journal if r[0] == "choose"]
            self.assertEqual(len(chosen), 1)
            self.assertEqual(chosen[0]["unique_id"], STM_UID)
            self.assertEqual(chosen[0]["options"]["target_override"], "stm32l476rgtx")
            with self.assertRaises(Exception) as caught:
                self.call(f, "connect", {"board_id": STM, "probe_uid": NORDIC_UID})
            self.assertIn("Only a no-setup board", str(caught.exception))
            self.assertEqual(len([r for r in f.journal if r[0] == "choose"]), 1)

    def test_evidenced_busy_probe_is_owned_by_an_actual_public_connection(self):
        with self.fixture((probe(STM_UID, "stlink"),), known_stm=True) as f:
            self.call(f, "setup_overview", {"board_names": [STM_NAME]}, structured=True)
            self.call(f, "connect", {"board_id": STM})
            before_assignment = canonical(f.server.assignment_store.bindings())
            other, memory = stm_profile()
            other["board_id"] = "other_stm"
            other["display_name"] = "Other STM"
            other["safety_ref"] = ".firm/safety/other_stm/memory_map.yaml"
            memory["board_id"] = "other_stm"
            memory["source_digests"]["semantic_profile"] = semantic_profile_digest(other)
            write_json(f.root / ".firm" / "boards" / "other_stm.json", other)
            policies = CapabilityPolicyRepository(f.root)
            policies.commit(
                "other_stm", Tier.SETUP_FULL, profile_snapshot=other, map_snapshot=memory
            )
            before = len([r for r in f.journal if r[0] == "session-open"])
            result = self.call(f, "setup_overview", {"board_names": ["Other STM"]}, structured=True)
            self.assertEqual(result["status"], "setup_probe_unavailable")
            self.assertIn("busy in this server", result["routes"][0]["reason"])
            self.assertIn(STM, result["routes"][0]["reason"])
            self.assertEqual(len([r for r in f.journal if r[0] == "session-open"]), before)
            self.assertEqual(canonical(f.server.assignment_store.bindings()), before_assignment)

    def test_provable_catalog_mismatch_refuses_before_open_or_reset(self):
        with self.fixture((probe(NORDIC_UID),), catalog="nrf52840") as f:
            with self.assertRaises(Exception) as caught:
                self.call(
                    f,
                    "connect",
                    {"board_id": "raw_wrong", "probe_uid": NORDIC_UID, "target": "stm32f103rc"},
                )
            self.assertIn("Target selection mismatch", str(caught.exception))
            self.assertEqual([r[0] for r in f.journal], ["choose", "session-close"])
            self.assertIsNone(f.server.connection_manager.maybe_connection("raw_wrong"))
            self.assertIsNone(f.server.gate_manager.snapshot("raw_wrong"))

    def test_catalog_generic_core_and_same_vendor_variants_keep_useful_raw_access(self):
        for catalog, target in (
            ("nrf52840", "cortex_m"),
            ("cortex_m", "stm32f103rc"),
            (None, "stm32f103rc"),
            ("nrf52840", "nrf52832"),
        ):
            with (
                self.subTest(catalog=catalog, target=target),
                self.fixture((probe(NORDIC_UID),), catalog=catalog) as f,
            ):
                self.assertIn(
                    "Connected",
                    self.call(
                        f,
                        "connect",
                        {"board_id": "unverified", "probe_uid": NORDIC_UID, "target": target},
                    ),
                )
                caps = self.call(f, "get_capabilities", {"board_id": "unverified"}, structured=True)
                self.assertEqual(caps["identity"]["assertion"], "not-asserted")
                self.assertIsNone(caps["identity"]["capability"])
                self.assertIsNone(f.server.gate_manager.snapshot("unverified"))

    def test_adapter_cannot_open_a_different_probe_after_selection_race(self):
        with self.fixture((probe(NORDIC_UID),), selected_uid="unrelated") as f:
            with self.assertRaises(Exception) as caught:
                self.call(
                    f,
                    "connect",
                    {"board_id": "bench", "probe_uid": NORDIC_UID, "target": "cortex_m"},
                )
            self.assertIn("does not match the requested UID", str(caught.exception))
            self.assertEqual([r[0] for r in f.journal], ["choose", "session-close"])
            self.assertIsNone(f.server.connection_manager.maybe_connection("bench"))


if __name__ == "__main__":
    unittest.main()
