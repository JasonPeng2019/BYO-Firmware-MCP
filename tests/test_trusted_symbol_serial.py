"""Symbol service/public replies and finite serial/finalizer contracts."""

from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from elftools.common.exceptions import ELFError
from pyocd_debug_mcp.kernel.operations import operation_timeout_seconds
from pyocd_debug_mcp.native_build import _validate_declared_artifacts, _validate_paths
from pyocd_debug_mcp.probe_families import ProbeFamilyRegistryError, load_probe_family_registry, provider_qualified_family
from pyocd_debug_mcp.safety.linker import BuildArtifactSelection, BuildRole, LinkerEvidenceError
from pyocd_debug_mcp.services.symbols import SymbolLookupError, find_symbols
from pyocd_debug_mcp.setup_flow.targets import TargetResolutionError, TargetResolver
from tests.elf_symbol_fixture import FUNC, TEXT, ElfSymbol, elf32_symbols
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
from pyocd_debug_mcp.artifact_collector import ArtifactRole, MANIFEST_NAME, _validate_destination, collect_artifacts
from pyocd_debug_mcp.hardware_inventory import EMPTY_INVENTORY_SNAPSHOT
from pyocd_debug_mcp.kernel.finalizers import FinalizerValidationError, parse_finalizer
from pyocd_debug_mcp.kernel.hygiene import cleanup_stale_owned_processes, require_clean_startup
from pyocd_debug_mcp.kernel.operations import operation_timeout_seconds
from pyocd_debug_mcp.kernel.processes import ProcessIdentityUnavailable
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.pack_provision import DeviceBinding, PackProvisionError, PackSpec, VerifiedPack, sha256_bytes
from pyocd_debug_mcp.services.connections import probe_connection_id
from pyocd_debug_mcp.services.symbols import ResolvedSymbol, find_symbols
from pyocd_debug_mcp.services.session_runtime import ActionContext
from pyocd_debug_mcp.services.uart_exchange_schema import validate_serial_exchange_parameters
from pyocd_debug_mcp.setup_flow.device_support import DeviceSupportResolver
from pyocd_debug_mcp.setup_flow.packs import PackCandidate, PackCandidateError, PackCandidatePipeline
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


class SymbolAndSerialContracts(unittest.TestCase):
    def test_default_uart_finalizer_timeout_matches_explicit_one_second(self) -> None:
        base = {"timeout_seconds": 31.0}
        self.assertEqual(
            operation_timeout_seconds(
                "write_serial",
                {**base, "on_exit": {"action": "uart_write", "text": "exit"}},
            ),
            operation_timeout_seconds(
                "write_serial",
                {
                    **base,
                    "on_exit": {
                        "action": "uart_write",
                        "text": "exit",
                        "timeout_seconds": 1.0,
                    },
                },
            ),
        )

    def test_symbol_search_returns_every_sorted_match_without_a_hidden_limit(self) -> None:
        symbols = [
            ElfSymbol(f"Match_{index:02d}", 0x08000000 + (30 - index) * 4, type=FUNC, section=TEXT)
            for index in range(25)
        ]
        with tempfile.TemporaryDirectory() as directory:
            elf_path = Path(directory) / "symbols.elf"
            elf_path.write_bytes(elf32_symbols(symbols))
            matches = find_symbols(elf_path, "match")

            self.assertEqual(len(matches), 25)
            self.assertEqual([item.name for item in matches], sorted(item.name for item in symbols))

        with self.assertRaises(SymbolLookupError):
            find_symbols("unused.elf", " ")
        with self.assertRaises(SymbolLookupError):
            find_symbols("missing.elf", "match")

    def test_symbol_search_reports_real_malformed_elf_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.elf"
            malformed.write_bytes(b"not an ELF")

            with self.assertRaisesRegex(ELFError, "Magic number does not match"):
                find_symbols(malformed, "match")

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
                    read_seconds=86_401.0,
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
