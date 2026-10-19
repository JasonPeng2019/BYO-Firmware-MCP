"""M02: a flash succeeds only after supported programming and matching artifact readback.

Every addressed artifact byte must lie in live flash with a usable algorithm before
any reset or programming, and must read back unchanged before success. The final
reset state is reported apart from that proof. These tests are host-only.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from intelhex import IntelHex
from pyocd.core.memory_map import FlashRegion, MemoryMap
from pyocd.coresight.coresight_target import CoreSightTarget
from pyocd.flash.flash import Flash

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.flash_image import (
    FlashImage,
    FlashResult,
    check_flash_request,
    derive_flash_image,
    verify_flash_image,
)
from pyocd_debug_mcp.monitor.classify import error_code
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.services.session_runtime import ActionContext, ToolOutcome
from pyocd_debug_mcp.target_errors import (
    FlashArtifactChangedError,
    FlashTargetSupportError,
    FlashVerificationError,
    UnsupportedArtifactError,
)
from pyocd_debug_mcp.tools.flash import FlashToolServices, build_flash_handlers
from tests.flash_test_support import (
    FLASH_SIZE,
    FLASH_START,
    SRAM_START,
    ElfSegment,
    FakeFlashTarget,
    file_programmer,
    flash_handle,
    flash_memory_map,
    programmed_files,
    write_elf32,
    write_intel_hex,
)


HARDWARE_RUNNER_PATH = Path(__file__).with_name("manual") / "manual_tiered_hardware_check.py"


def _pattern(size: int, seed: int) -> bytes:
    return bytes((index * 7 + seed) & 0xFF for index in range(size))


def _hardware_runner() -> Any:
    spec = importlib.util.spec_from_file_location(
        "flash_hardware_runner_unit", HARDWARE_RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class FlashImageDerivationTests(_TemporaryDirectoryTest):
    def test_sparse_elf_uses_load_addresses_and_leaves_holes_unpadded(self) -> None:
        text = _pattern(0x40, 1)
        data_init = _pattern(0x10, 2)
        firmware = write_elf32(
            self.root / "app.elf",
            [
                ElfSegment(FLASH_START, text),
                # .data: runs from SRAM but is loaded from flash after .text's gap.
                ElfSegment(FLASH_START + 0x800, data_init, vaddr=SRAM_START),
                # .bss: memory-only tail and a zero-file-size segment add no bytes.
                ElfSegment(FLASH_START + 0x1000, b"\xaa\xbb", memsz=0x100),
                ElfSegment(SRAM_START + 0x400, b"", memsz=0x200),
            ],
        )

        image = derive_flash_image(firmware)

        self.assertEqual(
            image.runs,
            (
                (FLASH_START, text),
                (FLASH_START + 0x800, data_init),
                (FLASH_START + 0x1000, b"\xaa\xbb"),
            ),
        )
        self.assertEqual(image.byte_count, 0x40 + 0x10 + 2)

    def test_hex_runs_match_pyocds_intel_hex_reader(self) -> None:
        runs = [
            (FLASH_START + 0xFFF0, _pattern(0x30, 3)),  # crosses a 64 KiB record boundary
            (FLASH_START + 0x1_8000, _pattern(5, 4)),
        ]
        firmware = write_intel_hex(self.root / "app.hex", runs)

        reference = IntelHex(str(firmware))
        expected = tuple(
            (start, bytes(reference.tobinarray(start=start, end=end - 1)))
            for start, end in reference.segments()
        )
        self.assertEqual(derive_flash_image(firmware).runs, expected)
        self.assertEqual(expected, tuple(runs))

    def test_raw_bin_is_addressed_only_at_an_explicit_base(self) -> None:
        firmware = self.root / "app.bin"
        firmware.write_bytes(b"\x10\x20\x30")

        self.assertEqual(
            derive_flash_image(firmware, base_address=FLASH_START + 0x4000).runs,
            ((FLASH_START + 0x4000, b"\x10\x20\x30"),),
        )
        with self.assertRaisesRegex(UnsupportedArtifactError, "explicit base_address"):
            derive_flash_image(firmware)
        for invalid in (-1, 1 << 32, True, cast(Any, "0x08000000")):
            with self.subTest(base=invalid), self.assertRaises(UnsupportedArtifactError):
                derive_flash_image(firmware, base_address=invalid)
        with self.assertRaisesRegex(UnsupportedArtifactError, "32-bit"):
            derive_flash_image(firmware, base_address=0xFFFF_FFFE)

        firmware.write_bytes(b"")
        with self.assertRaisesRegex(UnsupportedArtifactError, "addresses no bytes"):
            derive_flash_image(firmware, base_address=FLASH_START)

    def test_addressed_artifacts_refuse_a_base_and_unknown_suffixes_refuse(self) -> None:
        for name in ("app.elf", "app.AXF", "app.hex"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(UnsupportedArtifactError, "only to .bin"),
            ):
                check_flash_request(Path(name), FLASH_START)
        for name in ("app.s19", "app", "app.py"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(UnsupportedArtifactError, "Unsupported artifact type"),
            ):
                check_flash_request(Path(name), None)

    def test_malformed_and_inconsistent_artifacts_report_their_build_code(self) -> None:
        broken = self.root / "broken.elf"
        broken.write_bytes(b"\x7fELF-not-really")
        with self.assertRaisesRegex(UnsupportedArtifactError, r"\[build/elf-malformed\]"):
            derive_flash_image(broken)

        overlapping_hex = write_intel_hex(
            self.root / "overlap.hex", [(FLASH_START, b"\x01\x02"), (FLASH_START + 1, b"\x02")]
        )
        with self.assertRaisesRegex(UnsupportedArtifactError, r"\[build/hex-overlap\]"):
            derive_flash_image(overlapping_hex)

        identical = write_elf32(
            self.root / "identical.elf",
            [
                ElfSegment(FLASH_START, b"\x01\x02\x03\x04"),
                ElfSegment(FLASH_START + 2, b"\x03\x04\x05"),
            ],
        )
        self.assertEqual(
            derive_flash_image(identical).runs, ((FLASH_START, b"\x01\x02\x03\x04\x05"),)
        )
        conflicting = write_elf32(
            self.root / "conflict.elf",
            [ElfSegment(FLASH_START, b"\x01\x02\x03\x04"), ElfSegment(FLASH_START + 2, b"\x09")],
        )
        with self.assertRaisesRegex(UnsupportedArtifactError, r"\[build/segment-overlap\]"):
            derive_flash_image(conflicting)


class ReadbackOracleTests(unittest.TestCase):
    def test_readback_is_bounded_and_never_reads_holes(self) -> None:
        target = FakeFlashTarget()
        image = FlashImage(((FLASH_START, _pattern(10_000, 5)), (FLASH_START + 0x4000, b"\x42")))
        for start, data in image.runs:
            target.write(start, data)

        verify_flash_image(image, target.read_memory_block8)

        self.assertEqual(
            target.reads,
            [
                (FLASH_START, 4096),
                (FLASH_START + 4096, 4096),
                (FLASH_START + 8192, 10_000 - 8192),
                (FLASH_START + 0x4000, 1),
            ],
        )

    def test_unchanged_mismatched_short_and_failed_reads_are_unverified(self) -> None:
        image = FlashImage(((FLASH_START, b"\x01\x02\x03\x04"),))
        erased = FakeFlashTarget()
        with self.assertRaisesRegex(
            FlashVerificationError, "at 0x08000000: expected 0x01, read 0xFF"
        ):
            verify_flash_image(image, erased.read_memory_block8)

        partial = FakeFlashTarget()
        partial.write(FLASH_START, b"\x01\x02")
        with self.assertRaisesRegex(FlashVerificationError, r"at 0x08000002.*2 of 4 bytes differ"):
            verify_flash_image(image, partial.read_memory_block8)

        with self.assertRaisesRegex(FlashVerificationError, "returned 3 of 4 bytes"):
            verify_flash_image(image, lambda _address, _size: [1, 2, 3])

        unreadable = FakeFlashTarget()
        unreadable.read_error = OSError("SWD fault")
        with self.assertRaisesRegex(FlashVerificationError, "could not be read back.*SWD fault"):
            verify_flash_image(image, unreadable.read_memory_block8)

        with self.assertRaises(ValueError):
            verify_flash_image(image, erased.read_memory_block8, chunk_bytes=0)


class FlashResultDocumentTests(unittest.TestCase):
    def test_document_round_trip_and_public_summary(self) -> None:
        result = FlashResult(
            ((FLASH_START, FLASH_START + 4), (SRAM_START, SRAM_START + 1)), "halted"
        )

        self.assertEqual(FlashResult.from_document(result.to_document()), result)
        self.assertEqual(result.verified_bytes, 5)
        self.assertEqual(
            result.programming_summary(),
            {
                "status": "verified",
                "verified_bytes": 5,
                "ranges": [
                    {"start": "0x08000000", "end": "0x08000004"},
                    {"start": "0x20000000", "end": "0x20000001"},
                ],
            },
        )

    def test_documents_that_do_not_prove_programming_are_rejected(self) -> None:
        valid = FlashResult(((FLASH_START, FLASH_START + 4),), "running").to_document()
        variants: dict[str, object] = {
            "not verified": {**valid, "programming": "skipped"},
            "count mismatch": {**valid, "verified_bytes": 5},
            "boolean count": {
                **FlashResult(((0, 1),), "running").to_document(),
                "verified_bytes": True,
            },
            "state": {**valid, "target_state": "RUNNING"},
            "no ranges": {**valid, "verified_ranges": [], "verified_bytes": 0},
            "overlap": {
                **valid,
                "verified_ranges": [
                    [FLASH_START, FLASH_START + 4],
                    [FLASH_START + 2, FLASH_START + 6],
                ],
                "verified_bytes": 8,
            },
            "extra field": {**valid, "success": True},
            "bare state": "running",
        }
        for name, document in variants.items():
            with self.subTest(name), self.assertRaises(ValueError):
                FlashResult.from_document(document)


class LiveFlashSupportTests(_TemporaryDirectoryTest):
    def _check(self, target: FakeFlashTarget, runs: list[tuple[int, bytes]]) -> None:
        firmware = write_intel_hex(self.root / "app.hex", runs)
        swd_pyocd.PyOCDSWDInterface().check_flash_support(flash_handle(target), firmware)

    def test_supported_flash_spanning_adjacent_regions_passes_without_target_effects(self) -> None:
        first = FlashRegion(start=FLASH_START, length=0x1000, blocksize=0x400, name="bank0")
        second = FlashRegion(
            start=FLASH_START + 0x1000, length=0x1000, blocksize=0x400, name="bank1"
        )
        first.flash = SimpleNamespace(is_valid=True)
        second.flash = SimpleNamespace(is_valid=True)
        target = FakeFlashTarget(MemoryMap(first, second))

        self._check(target, [(FLASH_START + 0xFF0, _pattern(0x20, 6))])

        self.assertEqual(target.calls, [])

    def test_addressed_bytes_without_a_live_flash_algorithm_are_refused(self) -> None:
        without_flash = FlashRegion(start=FLASH_START, length=FLASH_SIZE, blocksize=0x400)
        bank_without_algorithm = FlashRegion(
            start=FLASH_START + FLASH_SIZE, length=0x1000, blocksize=0x400, name="bank1"
        )
        bank_without_algorithm.flash = Flash(cast(Any, None), None)
        two_banks = flash_memory_map()
        two_banks.add_region(bank_without_algorithm)
        cases: dict[str, tuple[MemoryMap, int, int]] = {
            "pyOCD default cortex_m map": (
                CoreSightTarget._create_default_cortex_m_memory_map(cast(Any, object())),
                FLASH_START,
                FLASH_START,
            ),
            "flash region without a Flash object": (
                MemoryMap(without_flash),
                FLASH_START,
                FLASH_START,
            ),
            "missing sequences": (
                flash_memory_map(
                    flash=SimpleNamespace(is_valid=True, has_required_sequences=False)
                ),
                FLASH_START,
                FLASH_START,
            ),
            "RAM-addressed bytes": (flash_memory_map(), SRAM_START, SRAM_START),
            "unmapped bytes": (flash_memory_map(), 0x9000_0000, 0x9000_0000),
            "range runs past flash end": (
                flash_memory_map(),
                FLASH_START + FLASH_SIZE - 2,
                FLASH_START + FLASH_SIZE,
            ),
            "second bank lacks an algorithm": (
                two_banks,
                FLASH_START + FLASH_SIZE - 2,
                FLASH_START + FLASH_SIZE,
            ),
        }
        for name, (memory_map, start, refused_at) in cases.items():
            target = FakeFlashTarget(memory_map, part_number="cortex_m")
            with (
                self.subTest(name),
                self.assertRaisesRegex(
                    FlashTargetSupportError,
                    rf"'cortex_m' has no programmable flash algorithm .* at 0x{refused_at:08X}"
                    r".*nothing was reset, erased or programmed",
                ),
            ):
                self._check(target, [(start, b"\x01\x02\x03\x04")])
            self.assertEqual(target.calls, [])

    def test_unprovable_support_fails_closed(self) -> None:
        broken_map = SimpleNamespace(
            get_region_for_address=Mock(side_effect=RuntimeError("map unavailable"))
        )
        target = FakeFlashTarget(cast(Any, broken_map))
        with self.assertRaisesRegex(
            FlashTargetSupportError, "could not be confirmed.*map unavailable"
        ):
            self._check(target, [(FLASH_START, b"\x01")])


class AdapterFlashVerificationTests(_TemporaryDirectoryTest):
    # Mirrors the M11 corroboration: a different 5680-addressed-byte sparse ELF.
    OLD = ((FLASH_START, _pattern(0x1000, 11)), (FLASH_START + 0x2000, _pattern(1584, 12)))
    NEW = ((FLASH_START, _pattern(0x1000, 21)), (FLASH_START + 0x2000, _pattern(1584, 22)))

    def _elf(self) -> Path:
        return write_elf32(
            self.root / "different.elf", [ElfSegment(start, data) for start, data in self.NEW]
        )

    def _flash(
        self,
        target: FakeFlashTarget,
        firmware: Path,
        programmer: Mock,
        *,
        base_address: int | None = None,
    ) -> FlashResult:
        for start, data in self.OLD:
            target.write(start, data)
        with patch.object(swd_pyocd, "FileProgrammer", programmer):
            return swd_pyocd.PyOCDSWDInterface().flash(
                flash_handle(target), firmware, halt_after_reset=False, base_address=base_address
            )

    def test_generic_cortex_m_target_is_refused_before_reset_or_programming(self) -> None:
        target = FakeFlashTarget(
            CoreSightTarget._create_default_cortex_m_memory_map(cast(Any, object())),
            part_number="cortex_m",
        )
        programmer = file_programmer(target, self.NEW)

        with self.assertRaisesRegex(FlashTargetSupportError, "generic target such as cortex_m"):
            self._flash(target, self._elf(), programmer)

        programmer.assert_not_called()
        self.assertEqual(target.calls, [])
        self.assertEqual(target.memory[FLASH_START], self.OLD[0][1][0])

    def test_programmed_sparse_elf_is_verified_before_the_separately_reported_reset(self) -> None:
        target = FakeFlashTarget()
        result = self._flash(target, self._elf(), file_programmer(target, self.NEW))

        self.assertEqual(
            result,
            FlashResult(
                ((FLASH_START, FLASH_START + 0x1000), (FLASH_START + 0x2000, FLASH_START + 0x2630)),
                "running",
            ),
        )
        self.assertEqual(result.verified_bytes, 5680)
        self.assertEqual(target.calls, ["reset_and_halt", "program", "read", "read", "reset"])
        self.assertNotIn(FLASH_START + 0x1000, [address for address, _size in target.reads])

    def test_no_op_partial_and_unreadable_programming_fail_without_final_reset(self) -> None:
        new_first = self.NEW[0]
        cases: dict[str, tuple[tuple[tuple[int, bytes], ...], BaseException | None, str]] = {
            "no-op programmer": ((), None, "does not match the artifact at 0x08000000"),
            "partial programming": (
                (new_first,),
                None,
                "does not match the artifact at 0x08002000",
            ),
            "readback failure": (self.NEW, OSError("SWD fault"), "could not be read back"),
        }
        for name, (writes, read_error, message) in cases.items():
            target = FakeFlashTarget()
            target.read_error = read_error
            with self.subTest(name), self.assertRaisesRegex(FlashVerificationError, message):
                self._flash(target, self._elf(), file_programmer(target, writes))
            self.assertNotIn("reset", target.calls[2:])
            self.assertEqual(target.calls[:2], ["reset_and_halt", "program"])

    def test_raw_bin_is_programmed_and_verified_at_its_explicit_base(self) -> None:
        base = FLASH_START + 0x4000
        data = _pattern(0x30, 31)
        firmware = self.root / "app.bin"
        firmware.write_bytes(data)
        target = FakeFlashTarget()
        programmer = file_programmer(target, [(base, data)])
        programmed = programmed_files(programmer)

        result = self._flash(target, firmware, programmer, base_address=base)

        # pyOCD programs a private copy of the bytes that were checked, never the
        # mutable original path; the copy keeps the name its format is taken from.
        ((snapshot, content),) = programmed
        programmer.return_value.program.assert_called_once_with(str(snapshot), base_address=base)
        self.assertNotEqual(snapshot, firmware)
        self.assertEqual((snapshot.name, content), (firmware.name, data))
        self.assertFalse(snapshot.exists())
        self.assertEqual(result.verified_ranges, ((base, base + 0x30),))

        missing_base = FakeFlashTarget()
        with self.assertRaisesRegex(UnsupportedArtifactError, "explicit base_address"):
            self._flash(missing_base, firmware, file_programmer(missing_base))
        self.assertEqual(missing_base.calls, [])


class _RecordingBackend:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def check_flash_support(
        self,
        handle: object,
        firmware: Path,
        *,
        base_address: int | None = None,
        artifact_sha256: str | None = None,
    ) -> None:
        self.calls.append(("check_flash_support", firmware, base_address, artifact_sha256))

    def flash(
        self,
        handle: object,
        firmware: Path,
        *,
        halt_after_reset: bool,
        base_address: int | None = None,
        artifact_sha256: str | None = None,
    ) -> object:
        self.calls.append(("flash", firmware, halt_after_reset, base_address, artifact_sha256))
        return self.result


class TargetControlFlashTests(_TemporaryDirectoryTest):
    def _backend(self, result: object) -> _RecordingBackend:
        self.addCleanup(target_control.configure_backend_for_tests, target_control._BACKEND)
        backend = _RecordingBackend(result)
        target_control.configure_backend_for_tests(backend)
        return backend

    def test_only_a_flash_result_counts_as_programming(self) -> None:
        firmware = self.root / "app.bin"
        firmware.write_bytes(b"\x01")
        verified = FlashResult(((FLASH_START, FLASH_START + 1),), "running")
        backend = self._backend(verified)

        path = target_control.check_flash_support(
            cast(Any, "handle"), firmware, base_address=FLASH_START
        )
        self.assertEqual(
            target_control.flash_firmware(cast(Any, "handle"), firmware, base_address=FLASH_START),
            (path, verified),
        )
        target_control.check_flash_support(cast(Any, "handle"), firmware, artifact_sha256="digest")
        target_control.flash_firmware(cast(Any, "handle"), firmware, artifact_sha256="digest")
        self.assertEqual(
            backend.calls,
            [
                ("check_flash_support", path, FLASH_START, None),
                ("flash", path, False, FLASH_START, None),
                ("check_flash_support", path, None, "digest"),
                ("flash", path, False, None, "digest"),
            ],
        )

        self._backend("running")
        with self.assertRaisesRegex(FlashVerificationError, "no readback proof"):
            target_control.flash_firmware(cast(Any, "handle"), firmware, base_address=FLASH_START)


class GuardedFlashToolTests(unittest.TestCase):
    def _run(
        self,
        *,
        support_error: Exception | None = None,
        flash_error: Exception | None = None,
        result: object = None,
    ) -> tuple[list[str], list[dict[str, Any]], Any]:
        order: list[str] = []
        events: list[dict[str, Any]] = []
        request = SimpleNamespace(
            artifact_path=Path("firmware.elf"),
            identity=SimpleNamespace(
                sha256="digest", as_log_fields=lambda: {"artifact_sha256": "digest"}
            ),
        )

        def check(_handle: object, _path: Path, artifact_sha256: str) -> None:
            order.append("check")
            self.assertEqual(artifact_sha256, "digest")
            if support_error is not None:
                raise support_error

        def flash(_handle: object, path: Path, artifact_sha256: str) -> tuple[Path, object]:
            order.append("flash")
            self.assertEqual(artifact_sha256, "digest")
            if flash_error is not None:
                raise flash_error
            return path, result

        services = FlashToolServices(
            runtime_for=lambda _board: None,
            active_session_id=lambda _board: "session",
            duration_ms=lambda _started: 1,
            record_event=lambda *_args, **kwargs: events.append(kwargs),
            format_refusal=lambda refusal, **_kwargs: (
                f"Refused [{refusal.code}]: {refusal.message}"
            ),
            action_context=lambda tool, board: ActionContext("test", tool, board),
            maybe_handle_for=lambda _board: None,
            handle_for=lambda _board: "handle",
            resolve_request=lambda *_args: request,
            flash_target=cast(Any, flash),
            error_code=error_code,
            check_flash_support=check,
            prepare_symbol_artifact=lambda *_args: order.append("prepare_symbols") or "symbols",
            bind_symbol_artifact=lambda _board, binding: order.append(f"bind:{binding}"),
            prepare_generic_allocation=lambda *_args: order.append("prepare") or "allocation",
            commit_generic_allocation=lambda _board, item: order.append(f"commit:{item}"),
            clear_generic_allocation=lambda _board: order.append("clear"),
        )
        handler = build_flash_handlers(services)["flash_application"]
        try:
            reply: Any = handler("board", "firmware.elf")
        except Exception as exc:  # noqa: BLE001 - returned for the assertions
            reply = exc
        return order, events, reply

    def test_unsupported_target_is_refused_before_allocation_commit_or_flash(self) -> None:
        order, events, reply = self._run(
            support_error=FlashTargetSupportError("no programmable flash algorithm")
        )

        self.assertIn("Refused [flash/unsupported-target]", reply)
        self.assertEqual(order, ["prepare", "prepare_symbols", "check", "clear"])
        self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.REFUSED)
        self.assertEqual(events[-1]["error_code"], "flash/unsupported-target")

    def test_artifact_rebuilt_after_approval_is_refused_before_any_effect_or_binding(self) -> None:
        changed = FlashArtifactChangedError("changed after it was approved")
        cases: dict[str, tuple[dict[str, Any], list[str]]] = {
            "support check": (
                {"support_error": changed},
                ["prepare", "prepare_symbols", "check", "clear"],
            ),
            # The backend refuses before reset or programming; the allocation was
            # already committed for the approved content, as for any later failure.
            "flash backend": (
                {"flash_error": changed},
                ["prepare", "prepare_symbols", "check", "commit:allocation", "flash", "clear"],
            ),
        }
        for name, (case, expected_order) in cases.items():
            with self.subTest(name):
                order, events, reply = self._run(**case)
                self.assertIn("Refused [flash/artifact-changed]", reply)
                self.assertIn("changed after it was approved", reply)
                self.assertEqual(order, expected_order)
                self.assertNotIn("bind:symbols", order)
                self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.REFUSED)
                self.assertEqual(events[-1]["error_code"], "flash/artifact-changed")

    def test_unverified_programming_fails_without_binding_the_artifact(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "readback mismatch": {"flash_error": FlashVerificationError("does not match")},
            "backend without proof": {"result": "running"},
        }
        for name, case in cases.items():
            with self.subTest(name):
                order, events, reply = self._run(**case)
                self.assertIsInstance(reply, FlashVerificationError)
                self.assertEqual(
                    order,
                    ["prepare", "prepare_symbols", "check", "commit:allocation", "flash", "clear"],
                )
                self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.FAILED)
                self.assertEqual(events[-1]["error_code"], "flash/verification-failed")

    def test_verified_programming_reports_proof_and_reset_state_separately(self) -> None:
        result = FlashResult(((FLASH_START, FLASH_START + 8),), "reset_state_unconfirmed")
        order, events, reply = self._run(result=result)

        self.assertEqual(
            order,
            ["prepare", "prepare_symbols", "check", "commit:allocation", "flash", "bind:symbols"],
        )
        self.assertIn("read back 8 matching addressed bytes", reply)
        self.assertIn("final reset state is unconfirmed", reply)
        # The physical hardware runner still recognizes the public success text.
        runner = _hardware_runner()
        for state in ("reset_state_unconfirmed", "running"):
            with self.subTest(state=state):
                _order, _events, text = self._run(
                    result=FlashResult(((FLASH_START, FLASH_START + 8),), state)
                )
                self.assertIsNotNone(
                    runner._strict_public_result_first_line(text, runner._PUBLIC_FLASH_RESULT)
                )
        self.assertEqual(events[-1]["outcome_kind"], ToolOutcome.SUCCESS)
        self.assertEqual(
            events[-1]["details"],
            {
                "programming": result.programming_summary(),
                "target_state": "reset_state_unconfirmed",
                "safety_map_checked": True,
            },
        )

    def test_flash_failures_have_stable_event_codes(self) -> None:
        self.assertEqual(error_code(FlashTargetSupportError("x")), "flash/unsupported-target")
        self.assertEqual(error_code(FlashVerificationError("x")), "flash/verification-failed")


if __name__ == "__main__":
    unittest.main()
