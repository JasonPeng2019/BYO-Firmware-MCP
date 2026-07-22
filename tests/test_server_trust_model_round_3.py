"""Comprehensive retained-correctness regressions for trust-model round three."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.artifact_collector import ArtifactRole, collect_artifacts
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.kernel.finalizers import FinalizerValidationError, parse_finalizer
from pyocd_debug_mcp.kernel.hygiene import cleanup_stale_owned_processes, require_clean_startup
from pyocd_debug_mcp.kernel.processes import ProcessIdentityUnavailable, ProcessMarker
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.pack_provision import (
    DeviceBinding,
    PackProvisionError,
    PackSpec,
    VerifiedPack,
    sha256_bytes,
)
from pyocd_debug_mcp.services import symbols
from pyocd_debug_mcp.services.session_runtime import PolicyRefusal
from pyocd_debug_mcp.services.symbols import ResolvedSymbol
from pyocd_debug_mcp.services.uart_exchange_schema import validate_serial_exchange_parameters
from pyocd_debug_mcp.safety.map_build import GenericSafetyMapDocument
from pyocd_debug_mcp.setup_flow.device_support import (
    DeviceSupportResolver,
    derive_candidate_binding,
)
from pyocd_debug_mcp.setup_flow.packs import (
    PackCandidate,
    PackCandidateError,
    PackCandidatePipeline,
)
from pyocd_debug_mcp.tools.batch import BatchChild, BatchValidationError, build_batch_handlers
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.handshake import build_initialization_guidance
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers
from pyocd_debug_mcp.tools.serial import SerialToolServices, write_serial


def _exchange() -> dict[str, object]:
    return {
        "steps": [{"text": "x", "expected_text": "ok", "line_ending": "lf"}],
        "read_seconds": 1.0,
        "baudrate": None,
        "port": None,
        "ready_text": None,
        "ready_seconds": 0.0,
        "ready_probe_text": None,
        "ready_probe_line_ending": "none",
        "ready_probe_delay_seconds": 0.0,
        "clear_input": False,
    }


def _memory(artifact: Path, finder: object) -> MemoryToolServices:
    return MemoryToolServices(
        lambda _: None,
        lambda _: None,
        lambda _: 0,
        lambda *a, **k: None,
        lambda refusal, **k: f"Refused [{refusal.code}]: {refusal.message}",
        lambda _: object(),
        lambda _: artifact,
        finder,
        lambda *_: None,
        lambda *_: 0,
        lambda *_: [],
        lambda *_: None,
        lambda *_: None,
    )  # type: ignore[arg-type]


def _serial() -> SerialToolServices:
    return SerialToolServices(
        lambda _: None,
        lambda _: None,
        lambda _: 0,
        lambda *a, **k: None,
        lambda *a, **k: None,
        lambda refusal, **k: f"{refusal.code}:{refusal.message}",
        lambda blocked, **k: blocked.message,
        lambda _: None,
        lambda _: None,
        lambda *a, **k: None,
        lambda *a, **k: None,
        lambda *a, **k: None,
        lambda *a, **k: None,
        lambda _: None,
        lambda *a: None,
        "no board",
    )


def _pdsc(leading: str = "") -> str:
    return (
        '<?xml version="1.0"?>\n'
        + leading
        + '<package schemaVersion="1.0"><name>T</name><vendor>Vendor</vendor><description>x</description><releases><release version="1" date="2026-01-01">x</release></releases><devices><family Dvendor="Vendor" Dfamily="Test"><device Dname="TEST123"><processor Dcore="Cortex-M4"/></device></family></devices></package>'
    )


def _pack_bytes(pdsc: str) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "candidate.pack"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("descriptor.pdsc", pdsc)
        return path.read_bytes()


class RoundThreeRegressionTests(unittest.TestCase):
    def test_r3_01_public_symbol_handler_returns_all_and_refuses_malformed(self) -> None:
        matches = tuple(
            ResolvedSymbol(f"Match_{i:02d}", 0x08000000 + i, 4, "STT_FUNC") for i in range(25)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.elf"
            path.write_bytes(b"\x7fELF")
            handler = build_memory_handlers(_memory(path, lambda *_: matches))["find_symbol"]
            response = handler("board", "match", str(path))
            self.assertEqual(response.count("@0x"), 25)
            self.assertLess(response.index("Match_00@"), response.index("Match_24@"))
            malformed = Path(directory) / "bad.elf"
            malformed.write_bytes(b"\x7fELFnot-complete")
            response = build_memory_handlers(_memory(malformed, symbols.find_symbols))[
                "find_symbol"
            ]("board", "match", str(malformed))
            self.assertIn("Refused [memory/symbol-artifact-parse-failed]", response)
            self.assertIn("could not be parsed safely", response)

    def test_r3_02_serial_and_finalizer_validation_rejects_invalid_durations(self) -> None:
        valid = _exchange()
        valid["steps"] = [{"text": "x" * 5000, "expected_text": "ok", "line_ending": "lf"}]
        valid["read_seconds"] = 86_401.0
        self.assertIsNone(validate_serial_exchange_parameters(valid))
        for field in ("read_seconds", "ready_seconds"):
            for value in (0.0, -1.0):
                candidate = _exchange()
                candidate.update(
                    {
                        "ready_text": "ready",
                        "ready_seconds": 1.0,
                        "ready_probe_text": "probe",
                        "ready_probe_line_ending": "lf",
                        field: value,
                    }
                )
                self.assertIsNotNone(validate_serial_exchange_parameters(candidate))
        negative_delay = _exchange()
        negative_delay.update(
            {
                "ready_text": "ready",
                "ready_seconds": 1.0,
                "ready_probe_text": "probe",
                "ready_probe_line_ending": "lf",
                "ready_probe_delay_seconds": -1.0,
            }
        )
        self.assertIsNotNone(validate_serial_exchange_parameters(negative_delay))
        delayed = _exchange()
        delayed.update(
            {
                "ready_text": "ready",
                "ready_seconds": 1.0,
                "ready_probe_text": "probe",
                "ready_probe_line_ending": "lf",
                "ready_probe_delay_seconds": 2.0,
            }
        )
        self.assertIn("must not exceed", validate_serial_exchange_parameters(delayed) or "")
        inconsistent = _exchange()
        inconsistent["ready_seconds"] = 1.0
        self.assertIn("without ready_text", validate_serial_exchange_parameters(inconsistent) or "")
        for value in (0.0, -1.0):
            self.assertIn(
                "uart/invalid-timeout", write_serial(_serial(), "board", "x", timeout_seconds=value)
            )
            with self.assertRaises(FinalizerValidationError):
                parse_finalizer(
                    "write_serial", {"action": "uart_write", "text": "x", "timeout_seconds": value}
                )

    def test_r3_03_long_producer_and_batch_tool_are_accepted_while_correctness_refusals_remain(
        self,
    ) -> None:
        producer = "producer-" + "p" * 129
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.elf"
            source.write_bytes(b"ELF")
            result = collect_artifacts(
                {ArtifactRole.ELF: source}, root / "bundle", producer=producer
            )
            self.assertEqual(
                json.loads(result.manifest_path.read_text(encoding="utf-8"))["producer"], producer
            )
        name = "tool-" + "x" * 129
        seen: list[str] = []

        async def dispatch(tool: str, args: dict[str, object]) -> str:
            seen.append(tool)
            self.assertEqual(args["board_id"], "board")
            return "ok"

        handler = build_batch_handlers(dispatch, tool_exists=lambda value: value == name)[
            "action_batch"
        ]
        asyncio.run(handler("board", [BatchChild(tool_name=name, arguments={"board_id": "board"})]))
        self.assertEqual(seen, [name])
        for child in (
            BatchChild(tool_name="unknown", arguments={"board_id": "board"}),
            BatchChild(tool_name=name, arguments={"board_id": "other"}),
            BatchChild(tool_name="action_batch", arguments={"board_id": "board"}),
        ):
            with self.assertRaises(BatchValidationError):
                asyncio.run(handler("board", [child]))

    def test_r3_03_long_and_many_setup_names_are_preserved(self) -> None:
        names = ["display-" + "x" * 120] + [f"board-{index}" for index in range(1, 9)]
        probes = [
            SimpleNamespace(
                usb_serial=f"usb-{index}",
                probe_id=f"probe-{index}",
                probe_family="cmsis-dap",
                choice=lambda index=index: SimpleNamespace(label=f"Probe {index}"),
            )
            for index in range(len(names))
        ]
        inventory = SimpleNamespace(probes=probes, serial_ports=())
        assignments = {name: f"probe:usb-{index}" for index, name in enumerate(names)}
        with (
            patch.object(server, "_profile_repository", SimpleNamespace(load_all=lambda: ())),
            patch.object(server, "_validation_inventory", return_value=inventory),
            patch.object(server, "_replace_setup_assignments"),
        ):
            overview = server._setup_overview(names, assignments)
        self.assertEqual(overview["status"], "setup_routes_ready")
        self.assertEqual([route["display_name"] for route in overview["routes"]], names)

    def test_r3_04_artifact_collector_allows_empty_selected_directory_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.elf"
            source.write_bytes(b"elf")
            target = root / "target"
            target.mkdir()
            link = root / "selected"
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory links unavailable: {exc}")
            result = collect_artifacts({ArtifactRole.ELF: source}, link, producer="producer")
            self.assertEqual(result.output_dir, target.resolve())
            self.assertTrue((target / "firmware.elf").is_file())
            manifest = json.loads((target / "build-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["producer"], "producer")
            self.assertEqual(manifest["artifacts"]["elf"]["source_name"], "source.elf")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.elf"
            source.write_bytes(b"elf")
            nonempty = root / "nonempty"
            nonempty.mkdir()
            (nonempty / "x").touch()
            with self.assertRaises(ValueError):
                collect_artifacts({ArtifactRole.ELF: source}, nonempty)
            with self.assertRaises(ValueError):
                collect_artifacts({ArtifactRole.ELF: source}, root)
            file_destination = root / "not-a-directory"
            file_destination.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(ValueError):
                collect_artifacts({ArtifactRole.ELF: source}, file_destination)
            with self.assertRaises(ValueError):
                collect_artifacts({ArtifactRole.ELF: source}, root / ".firm" / "bundle")
            with self.assertRaises(ValueError):
                collect_artifacts({ArtifactRole.ELF: source}, source.parent)
            with self.assertRaises(ValueError):
                collect_artifacts({ArtifactRole.ELF: source}, Path.home())
            with self.assertRaises(ValueError):
                collect_artifacts({ArtifactRole.ELF: source}, Path(root.anchor))

    def test_r3_05_hygiene_and_long_pdsc_prolog_are_not_prefix_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = ProcessMarker(2, "id", 10, "owner", 20, "child", "hash", "tool", "now")
            for index in range(129):
                (root / f"{index}.json").write_text(json.dumps(asdict(marker)), encoding="utf-8")
            with (
                patch("pyocd_debug_mcp.kernel.hygiene._start_token", return_value=None),
                patch("pyocd_debug_mcp.kernel.hygiene.terminate_marked_group", return_value=True),
            ):
                result = cleanup_stale_owned_processes(root, timeout_seconds=30.0)
            self.assertEqual(result.inspected, 129)
            (root / "unresolved.json").write_text(json.dumps(asdict(marker)), encoding="utf-8")
            with patch(
                "pyocd_debug_mcp.kernel.hygiene._start_token",
                side_effect=ProcessIdentityUnavailable("unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "unresolved marker"):
                    require_clean_startup(root)
            pack = root / "descriptor.pack"
            with zipfile.ZipFile(pack, "w") as archive:
                archive.writestr("descriptor.pdsc", _pdsc("<!--" + "x" * 300 + "-->\n"))
            self.assertEqual(derive_candidate_binding(pack, "TEST123").pdsc_device, "TEST123")

    def test_r3_06_long_server_flash_and_handshake_outputs_preserve_suffixes(self) -> None:
        suffix = "-DISTINGUISHING-SUFFIX"
        message = "x" * 360 + suffix
        self.assertIn(
            suffix, server._format_refusal(PolicyRefusal("test/code", message), session_id=None)
        )
        events = Mock()
        services = FlashToolServices(
            lambda _: None,
            lambda _: None,
            lambda _: 0,
            events,
            lambda *a, **k: None,
            lambda r, **k: r.message,
            lambda b, **k: b.message,
            lambda _: None,
            lambda *a: None,
            lambda _: None,
            lambda _: None,
            lambda *a: (_ for _ in ()).throw(RuntimeError(message)),
            lambda *a: Path("unused"),
            lambda *a: None,
            lambda _: "runtime/error",
        )
        with self.assertRaisesRegex(RuntimeError, suffix):
            build_flash_handlers(services)["flash_application"]("board", "artifact")
        self.assertIn(suffix, events.call_args.kwargs["details"]["message"])
        registry = ToolRegistry()
        registry.register("long")
        self.assertIn(
            suffix, build_initialization_guidance(registry, {"long": "word " * 90 + suffix})
        )

    def test_r3_07_retained_pack_gates_fail_closed(self) -> None:
        payload = _pack_bytes(_pdsc())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FirmStore(root)
            store.ensure_layout()
            source = root / "candidate.pack"
            source.write_bytes(payload)
            candidate = PackCandidate(
                "Vendor.Test",
                "1",
                "candidate.pack",
                "https://vendor.invalid/pack",
                source,
                sha256_bytes(payload),
            )
            pipeline = PackCandidatePipeline(
                store, enumerate_targets=lambda *_: ("test123",), live_connect=lambda *_: None
            )
            validated = pipeline.validate(candidate, required_target="test123")
            validated.staged_path.write_bytes(b"changed")
            with self.assertRaises(PackCandidateError) as changed:
                pipeline.promote(validated, board_id="board")
            self.assertEqual(changed.exception.code, "package/staged-bytes-changed")
            bad = VerifiedPack(
                root / "bad.pack", PackSpec("bad", "1", "bad.pack", "", "0" * 64), b"not zip"
            )
            with self.assertRaises(PackProvisionError):
                DeviceSupportResolver._validate_archive(bad)
            malformed = root / "malformed.pack"
            with zipfile.ZipFile(malformed, "w") as archive:
                archive.writestr("bad.pdsc", "<package><devices>")
            with self.assertRaises(PackProvisionError):
                derive_candidate_binding(malformed, "TEST123")
            mismatch = PackCandidatePipeline(
                store, enumerate_targets=lambda *_: ("test123",), live_connect=lambda *_: None
            )
            with self.assertRaises(PackCandidateError) as target:
                mismatch.validate_device(
                    candidate,
                    required_target="test123",
                    device_binding=DeviceBinding("TEST123", "TEST123", "other"),
                )
            self.assertEqual(target.exception.code, "package/device-binding-target-mismatch")

    def test_r3_07_exact_device_ambiguity_refuses(self) -> None:
        first = VerifiedPack(
            Path("one.pack"),
            PackSpec(
                "one",
                "1",
                "one.pack",
                "",
                "a" * 64,
                ("one",),
                (),
                (DeviceBinding("TEST123", "TEST123", "one"),),
            ),
            b"one",
        )
        second = VerifiedPack(
            Path("two.pack"),
            PackSpec(
                "two",
                "1",
                "two.pack",
                "",
                "b" * 64,
                ("two",),
                (),
                (DeviceBinding("TEST123", "TEST123", "two"),),
            ),
            b"two",
        )
        resolver = DeviceSupportResolver(
            pack_loader=lambda target: {"one": first, "two": second}.get(target),
            device_names=lambda _: ("TEST123",),
            binding_deriver=lambda selected, part: selected.spec.device_bindings[0],
        )
        with self.assertRaisesRegex(PackProvisionError, "More than one"):
            resolver.resolve("TEST123", ("one", "two"))

    def test_r3_07_missing_programmable_driver_authority_refuses_allocation(self) -> None:
        profile = SimpleNamespace(mcu_part_number="TEST123", device_support=object())
        handle = TargetSessionHandle(None, None, "probe", "worker", None)
        with (
            patch.object(
                server._safety_repository,
                "load_current",
                return_value=object.__new__(GenericSafetyMapDocument),
            ),
            patch.object(
                server._safety_policy,
                "check_generic_application_candidate",
                return_value=(None, ()),
            ),
            patch.object(server, "_current_target", return_value="test123"),
            patch.object(server._profile_repository, "load", return_value=profile),
            patch.object(server, "_replay_profile_device_support", return_value=object()),
            patch.object(
                server,
                "resolve_device_support_geometry",
                return_value=SimpleNamespace(driver_proof_digest=None),
            ),
        ):
            with self.assertRaises(server.SafetyPolicyError) as refused:
                server._stage_generic_allocation("board", Path("artifact.elf"), handle)
        self.assertEqual(refused.exception.code, "safety/driver-unbounded")
