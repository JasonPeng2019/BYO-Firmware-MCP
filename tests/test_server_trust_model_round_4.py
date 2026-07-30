"""Regression completion for the round-four trusted-server review."""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pyocd_debug_mcp.artifact_collector import (
    ArtifactRole,
    MANIFEST_NAME,
    _validate_destination,
    collect_artifacts,
)
from pyocd_debug_mcp.kernel.finalizers import FinalizerValidationError, parse_finalizer
from pyocd_debug_mcp.kernel.hygiene import cleanup_stale_owned_processes, require_clean_startup
from pyocd_debug_mcp.kernel.operations import operation_timeout_seconds
from pyocd_debug_mcp.kernel.processes import ProcessIdentityUnavailable
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.pack_provision import (
    DeviceBinding,
    PackProvisionError,
    PackSpec,
    VerifiedPack,
    sha256_bytes,
)
from pyocd_debug_mcp.services.connections import probe_connection_id
from pyocd_debug_mcp.services.symbols import ResolvedSymbol, find_symbols
from pyocd_debug_mcp.services.session_runtime import ActionContext
from pyocd_debug_mcp.services.uart_exchange_schema import validate_serial_exchange_parameters
from pyocd_debug_mcp.setup_flow.device_support import DeviceSupportResolver
from pyocd_debug_mcp.setup_flow.packs import (
    PackCandidate,
    PackCandidateError,
    PackCandidatePipeline,
)
from pyocd_debug_mcp.safety.enforce import LoadedSafetyMap, SafetyPolicy, SafetyPolicyError
from pyocd_debug_mcp.safety.map_build import GenericMapGeometry, GenericSafetyMapDocument
from pyocd_debug_mcp.safety.regions import AddressRange
from pyocd_debug_mcp.tools.batch import BatchChild, BatchValidationError, build_batch_handlers
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from pyocd_debug_mcp.tools.handshake import build_initialization_guidance
from pyocd_debug_mcp.tools.memory import MemoryToolServices, build_memory_handlers
from pyocd_debug_mcp.tools.serial import SerialToolServices, read_serial, write_serial


def _memory_services(artifact: Path, finder: object) -> MemoryToolServices:
    return MemoryToolServices(
        runtime_for=lambda _board: None,
        active_session_id=lambda _board: None,
        duration_ms=lambda _started: 0,
        record_event=lambda *args, **kwargs: None,
        format_refusal=lambda refusal, **kwargs: f"Refused [{refusal.code}]: {refusal.message}",
        handle_for=lambda _board: object(),
        symbol_artifact_for=lambda _handle: artifact,
        find_symbols=finder,  # type: ignore[arg-type]
        resolve_symbol=lambda *_args: None,  # type: ignore[arg-type]
        read_target_memory=lambda *_args: 0,
        read_target_block=lambda *_args: [],
        write_target_memory=lambda *_args: None,
        check_memory_read=lambda *_args: None,
    )


def _serial_services() -> SerialToolServices:
    return SerialToolServices(
        runtime_for=lambda _board: None,
        active_session_id=lambda _board: None,
        duration_ms=lambda _started: 0,
        record_event=lambda *args, **kwargs: None,
        format_refusal=lambda refusal, **kwargs: f"Refused [{refusal.code}]: {refusal.message}",
        handle_for=lambda _board: None,
        resolve_port=lambda *_args, **_kwargs: None,
        capture_uart=lambda *_args, **_kwargs: None,
        write_uart=lambda *_args, **_kwargs: None,
        exchange_uart=lambda *_args, **_kwargs: None,
        reset_target=lambda _handle: None,
        no_board_config_message="no board",
    )


def _exchange(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
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
    values.update(overrides)
    return values


def _pack_bytes(pdsc: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Vendor.Device.pdsc", pdsc)
    return stream.getvalue()


class RoundFourRegressionTests(unittest.TestCase):
    def test_public_find_symbol_preserves_all_matches_and_refuses_parse_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "symbols.elf"
            artifact.write_bytes(b"\x7fELFfixture")
            matches = tuple(
                ResolvedSymbol(f"match_{index:02d}", 0x08000000 + index, 4, "STT_FUNC")
                for index in range(25)
            )
            find_symbol = build_memory_handlers(_memory_services(artifact, lambda *_: matches))[
                "find_symbol"
            ]
            rendered = find_symbol("board", "match", str(artifact))
            self.assertLess(rendered.index("match_00"), rendered.index("match_24"))
            self.assertIn("match_24", rendered)
            self.assertEqual(rendered.count("match_"), 25)

            malformed = Path(directory) / "malformed.elf"
            malformed.write_bytes(b"\x7fELFnot-valid")
            handler = build_memory_handlers(_memory_services(malformed, find_symbols))[
                "find_symbol"
            ]
            refused = handler("board", "match", str(malformed))
            self.assertIn("memory/symbol-artifact-parse-failed", refused)
            self.assertIn("could not be parsed safely", refused)
            self.assertIn("memory/empty-symbol-query", handler("board", " "))
            self.assertIn(
                "memory/symbol-artifact-unavailable", handler("board", "match", "missing.elf")
            )

    def test_serial_and_finalizer_positive_finite_boundaries(self) -> None:
        for value in (0, -1, math.inf, math.nan):
            self.assertIsNotNone(validate_serial_exchange_parameters(_exchange(read_seconds=value)))
            self.assertIn(
                "uart/invalid-read-seconds",
                read_serial(_serial_services(), "board", read_seconds=value),
            )
            self.assertIn(
                "uart/invalid-timeout",
                write_serial(_serial_services(), "board", "x", timeout_seconds=value),
            )
            with self.assertRaises(FinalizerValidationError):
                parse_finalizer(
                    "write_serial", {"action": "uart_write", "text": "x", "timeout_seconds": value}
                )
        self.assertIsNotNone(
            validate_serial_exchange_parameters(_exchange(ready_text="ready", ready_seconds=0))
        )
        for value in (-1, math.inf, math.nan):
            self.assertIsNotNone(
                validate_serial_exchange_parameters(
                    _exchange(ready_text="ready", ready_seconds=value)
                )
            )
            self.assertIsNotNone(
                validate_serial_exchange_parameters(
                    _exchange(
                        ready_text="ready",
                        ready_seconds=1,
                        ready_probe_text="p",
                        ready_probe_line_ending="lf",
                        ready_probe_delay_seconds=value,
                    )
                )
            )
        self.assertIsNotNone(
            validate_serial_exchange_parameters(
                _exchange(
                    ready_text="ready",
                    ready_seconds=1,
                    ready_probe_text="p",
                    ready_probe_line_ending="lf",
                    ready_probe_delay_seconds=2,
                )
            )
        )
        self.assertIsNotNone(validate_serial_exchange_parameters(_exchange(ready_seconds=1)))
        self.assertIsNone(
            validate_serial_exchange_parameters(
                _exchange(
                    steps=[{"text": "x" * 5000, "expected_text": "ok", "line_ending": "lf"}],
                    read_seconds=600.0,
                )
            )
        )
        finalizer = parse_finalizer(
            "write_serial", {"action": "uart_write", "text": "x" * 5000, "timeout_seconds": 6.5}
        )
        self.assertEqual(finalizer.timeout_seconds, 6.5)  # type: ignore[union-attr]
        self.assertGreater(
            operation_timeout_seconds(
                "write_serial",
                {
                    "on_exit": {
                        "action": "uart_write",
                        "text": "x" * 5000,
                        "timeout_seconds": 6.5,
                    }
                },
            ),
            6.5,
        )

    def test_long_batch_names_and_artifact_producer_are_preserved(self) -> None:
        known = "known_" + "x" * 130
        dispatched: list[str] = []

        async def dispatch(name: str, _args: dict[str, object]) -> str:
            dispatched.append(name)
            return "ok"

        handler = build_batch_handlers(dispatch, tool_exists=lambda name: name == known)[
            "action_batch"
        ]
        result = asyncio.run(
            handler("board", [BatchChild(tool_name=known, arguments={"board_id": "board"})])
        )
        self.assertEqual(dispatched, [known])
        self.assertIn(known, result)
        for child in (
            BatchChild(tool_name="unknown", arguments={"board_id": "board"}),
            BatchChild(tool_name=known, arguments={"board_id": "other"}),
            BatchChild(tool_name="action_batch", arguments={"board_id": "board"}),
        ):
            with self.assertRaises(BatchValidationError):
                asyncio.run(handler("board", [child]))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.elf"
            source.write_bytes(b"elf-data")
            producer = "producer-" + "p" * 140
            result = collect_artifacts(
                {ArtifactRole.ELF: source}, root / "output", producer=producer
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["producer"], producer)

    def test_long_display_names_and_many_setup_routes_are_preserved(self) -> None:
        from pyocd_debug_mcp import server

        names = [f"board-{index}-" + "x" * 110 for index in range(9)]
        probes = tuple(
            SimpleNamespace(
                usb_serial=f"serial-{index}",
                probe_id=f"probe-{index}",
                probe_family="jlink",
                choice=lambda index=index: SimpleNamespace(label=f"Probe {index}"),
            )
            for index in range(9)
        )
        assignments = {
            name: probe_connection_id("jlink", f"serial-{index}")
            for index, name in enumerate(names)
        }
        inventory = SimpleNamespace(probes=probes, serial_ports=())
        with (
            patch.object(server._profile_repository, "load_all", return_value=[]),
            patch("pyocd_debug_mcp.server._validation_inventory", return_value=inventory),
            patch("pyocd_debug_mcp.server._replace_setup_assignments"),
        ):
            overview = server._setup_overview(names, assignments)
            self.assertEqual(overview["status"], "setup_routes_ready")
            self.assertEqual([route["display_name"] for route in overview["routes"]], names)
            self.assertGreater(len(str(overview["routes"][-1]["display_name"])), 100)
            with self.assertRaisesRegex(ValueError, "non-empty"):
                server._setup_overview([""], {})
            with self.assertRaisesRegex(ValueError, "unique"):
                server._setup_overview([names[0], names[0].upper()], {})

    def test_artifact_collector_allows_empty_directory_link_and_refuses_invalid_destinations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.elf"
            source.write_bytes(b"ELF artifact bytes")
            target = root / "target"
            target.mkdir()
            link = root / "selected-output"
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                junction = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if junction.returncode:
                    self.skipTest(
                        "directory symlinks and junctions are unavailable: "
                        f"symlink={exc}; junction={junction.stderr.strip()}"
                    )
            producer = "evidence-" + "z" * 140
            result = collect_artifacts({ArtifactRole.ELF: source}, link, producer=producer)
            self.assertEqual(result.output_dir, target.resolve())
            self.assertEqual((target / "firmware.elf").read_bytes(), source.read_bytes())
            self.assertTrue((target / MANIFEST_NAME).is_file())
            self.assertEqual(
                json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))["producer"],
                producer,
            )

            empty = root / "empty"
            empty.mkdir()
            for destination in (Path(Path.cwd().anchor), Path.home(), root / ".firm" / "bundle"):
                with self.assertRaises(ValueError):
                    _validate_destination(destination, {ArtifactRole.ELF: source})
            file_destination = root / "file"
            file_destination.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                _validate_destination(file_destination, {ArtifactRole.ELF: source})
            nonempty = root / "nonempty"
            nonempty.mkdir()
            (nonempty / "x").write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                _validate_destination(nonempty, {ArtifactRole.ELF: source})
            with self.assertRaises(ValueError):
                _validate_destination(empty, {ArtifactRole.ELF: empty / "inside.elf"})

    def test_hygiene_inspects_every_marker_and_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = {
                "schema_version": 2,
                "marker_id": "id",
                "owner_pid": 1,
                "owner_start_token": "owner",
                "pid": 2,
                "start_token": "child",
                "argv_sha256": "digest",
                "executable": "tool",
                "created_at": "now",
            }
            for index in range(129):
                (root / f"{index:03d}.json").write_text(
                    json.dumps({**marker, "marker_id": str(index)}), encoding="utf-8"
                )
            with patch("pyocd_debug_mcp.kernel.hygiene._start_token", return_value="other"):
                result = cleanup_stale_owned_processes(root, timeout_seconds=30.0)
            self.assertEqual(result.inspected, 129)
            self.assertEqual(result.stale_removed, 129)

            (root / "unresolved.json").write_text(json.dumps(marker), encoding="utf-8")
            unavailable = ProcessIdentityUnavailable("identity unavailable")
            with patch("pyocd_debug_mcp.kernel.hygiene._start_token", side_effect=unavailable):
                result = cleanup_stale_owned_processes(root, timeout_seconds=30.0)
            self.assertEqual(result.unresolved, 1)
            with patch("pyocd_debug_mcp.kernel.hygiene._start_token", side_effect=unavailable):
                with self.assertRaisesRegex(RuntimeError, "unresolved marker"):
                    require_clean_startup(root)

    def test_real_pdsc_parser_accepts_identifying_content_after_long_valid_prefix(self) -> None:
        prefix = b"<?xml version='1.0'?>\n<!--" + b"x" * 300 + b"-->\n  "
        pdsc = prefix + (
            b"<package><name>Vendor Device</name><devices><family Dvendor='Vendor' Dfamily='Family'>"
            b"<device Dname='TEST123'/></family></devices></package>"
        )
        payload = _pack_bytes(pdsc)
        selected = VerifiedPack(
            Path("prefix.pack"),
            PackSpec("id", "", "prefix.pack", "", sha256_bytes(payload)),
            payload,
        )
        self.assertEqual(DeviceSupportResolver._cmsis_device_names(selected), ("TEST123",))

    def test_long_diagnostics_keep_distinguishing_suffixes(self) -> None:
        from pyocd_debug_mcp import server
        from pyocd_debug_mcp.services.session_runtime import PolicyRefusal

        suffix = "SERVER-SUFFIX"
        self.assertIn(
            suffix,
            server._format_refusal(PolicyRefusal("code", "x" * 350 + suffix), session_id=None),
        )
        registry = ToolRegistry()
        registry.register("tool")
        self.assertIn(
            "HANDSHAKE-SUFFIX",
            build_initialization_guidance(registry, {"tool": "x" * 350 + "HANDSHAKE-SUFFIX"}),
        )

        recorded = Mock()
        services = FlashToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: None,
            duration_ms=lambda _started: 0,
            record_event=recorded,
            format_refusal=lambda refusal, **kwargs: str(refusal),
            action_context=lambda tool, board: ActionContext("test", tool, board),
            maybe_handle_for=lambda _board: None,
            handle_for=lambda _board: object(),
            resolve_request=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("x" * 350 + "FLASH-SUFFIX")
            ),
            flash_target=lambda *_args: (Path("unused"), "running"),
            error_code=lambda _exc: "flash/test",
        )
        with self.assertRaisesRegex(RuntimeError, "FLASH-SUFFIX"):
            build_flash_handlers(services)["flash_application"]("board", "artifact")
        self.assertIn("FLASH-SUFFIX", recorded.call_args.kwargs["details"]["message"])

    def test_pack_stability_parser_and_exact_binding_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = _pack_bytes(b"<package/>")
            changed = root / "changed.pack"
            changed.write_bytes(original)
            spec = PackSpec("id", "", changed.name, "url", sha256_bytes(original))
            selected = VerifiedPack(changed, spec, original)
            changed.write_bytes(_pack_bytes(b"<package><name>changed</name></package>"))
            with self.assertRaisesRegex(PackProvisionError, "changed or disappeared"):
                selected.verify_unchanged()

            malformed = VerifiedPack(Path("bad.pack"), spec, b"not-a-zip")
            with self.assertRaises(PackProvisionError):
                DeviceSupportResolver._validate_archive(malformed)
            unparseable = _pack_bytes(b"<package>")
            bad_pdsc = VerifiedPack(
                Path("bad-pdsc.pack"),
                PackSpec("id", "", "bad-pdsc.pack", "", sha256_bytes(unparseable)),
                unparseable,
            )
            with self.assertRaises(PackProvisionError):
                DeviceSupportResolver._cmsis_device_names(bad_pdsc)

            candidate = PackCandidate(
                "id",
                "1",
                "candidate.pack",
                "https://vendor.invalid",
                changed,
                sha256_bytes(changed.read_bytes()),
            )
            from pyocd_debug_mcp.firmstore.store import FirmStore

            pipeline = PackCandidatePipeline(
                FirmStore(root), enumerate_targets=lambda *_: (), live_connect=lambda *_: None
            )
            with self.assertRaises(PackCandidateError) as mismatch:
                pipeline.validate_device(
                    candidate,
                    required_target="target-b",
                    device_binding=DeviceBinding("PART", "PART", "target-a"),
                )
            self.assertEqual(mismatch.exception.code, "package/device-binding-target-mismatch")

            changed_before_promotion = PackCandidate(
                "changed",
                "1",
                "changed-before-promotion.pack",
                "https://vendor.invalid/changed",
                changed,
                sha256_bytes(original),
            )
            with self.assertRaises(PackCandidateError) as unstable:
                PackCandidatePipeline(
                    FirmStore(root), enumerate_targets=lambda *_: (), live_connect=lambda *_: None
                ).validate(changed_before_promotion, required_target="target")
            self.assertEqual(unstable.exception.code, "package/checksum-mismatch")

        resolver = DeviceSupportResolver(pack_loader=lambda _target: None)
        with patch.object(resolver, "candidates", return_value=(Mock(), Mock())):
            with self.assertRaisesRegex(PackProvisionError, "More than one"):
                resolver.resolve("PART", ("target",))

        incomplete = object.__new__(GenericSafetyMapDocument)
        object.__setattr__(
            incomplete,
            "geometry",
            GenericMapGeometry(
                (AddressRange(0x08000000, 0x08001000),),
                (AddressRange(0x20000000, 0x20001000),),
                erase_available=False,
            ),
        )
        object.__setattr__(incomplete, "identity", SimpleNamespace(pyocd_target="target"))
        policy = SafetyPolicy(Mock())
        with patch.object(policy, "load", return_value=LoadedSafetyMap(incomplete, Mock())):
            with self.assertRaises(SafetyPolicyError) as missing_flash_authority:
                policy.check_generic_application_candidate(
                    "board", Path("firmware.elf"), current_target="target"
                )
        self.assertEqual(missing_flash_authority.exception.code, "safety/geometry-incomplete")


if __name__ == "__main__":
    unittest.main()
