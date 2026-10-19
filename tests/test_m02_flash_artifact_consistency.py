"""M02-S-C01: a flash programs exactly the artifact bytes it checked and verifies.

A cooperative build rewrites the ORIGINAL artifact path while a flash is under
way; it cannot know any private copy. The rebuild keeps the checked bytes and
adds 79 bytes at 0x08004000 (or appends them to a raw BIN). Expected bytes come
from fixture source, and the controlled programmer parses whatever file it is
handed with its own reference parser. These are host-only tests.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from pyocd_debug_mcp.adapters import provider_worker_runtime, swd_process
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.flash_image import FlashResult, snapshot_flash_artifact
from pyocd_debug_mcp.safety.map_build import GenericSafetyMapDocument
from pyocd_debug_mcp.services import target_control
from pyocd_debug_mcp.target_errors import FlashArtifactChangedError, UnsupportedArtifactError
from tests.m02_independent_support import (
    BASE,
    BOARD,
    fixture_runs,
    plan,
    public_fixture,
    separate_loads,
    write_artifact,
)

APPROVED = fixture_runs()[0][1]  # 9001 bytes at BASE
EXTRA_START, EXTRA = fixture_runs()[1]  # the 79 bytes a rebuild adds
SECTOR_END = BASE + 0x2800  # the approved allocation's sector-aligned end


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rebuild(artifact: Path, runs: tuple[tuple[int, bytes], ...]) -> Callable[[], None]:
    """A build that rewrites the original path when it runs."""

    def rebuild() -> None:
        write_artifact(artifact, runs)

    return rebuild


def _rebuilt_runs(suffix: str) -> tuple[tuple[tuple[int, bytes], ...], tuple[int, bytes]]:
    """The rebuilt artifact's loads and the run it adds beyond the approved bytes."""

    if suffix == ".bin":
        return ((BASE, APPROVED + EXTRA),), (BASE + len(APPROVED), EXTRA)
    return fixture_runs(), (EXTRA_START, EXTRA)


def _only_approved_programmed(f: Any) -> bytearray:
    expected = bytearray(f.target.initial)
    expected[: len(APPROVED)] = APPROVED
    return expected


def _approved_reads() -> list[tuple[int, int]]:
    return [(BASE, 4096), (BASE + 4096, 4096), (BASE + 8192, len(APPROVED) - 8192)]


class RawFlashProgramsTheCheckedBytes(unittest.TestCase):
    def raw(self, f: Any, artifact: Path, suffix: str) -> dict[str, Any]:
        arguments = {"board_id": BOARD, "artifact": str(artifact)}
        if suffix == ".bin":
            arguments["base_address"] = hex(BASE)
        return json.loads(f.call("flash_raw", arguments))

    def test_rebuild_while_programming_cannot_add_unverified_bytes(self) -> None:
        for suffix in (".elf", ".axf", ".hex", ".bin"):
            with self.subTest(format=suffix), public_fixture(self) as f:
                artifact = write_artifact(f.root / ("app" + suffix), ((BASE, APPROVED),))
                rebuilt, (added_start, added) = _rebuilt_runs(suffix)
                f.target.before_program = _rebuild(artifact, rebuilt)

                reply = self.raw(f, artifact, suffix)

                self.assertEqual(reply["status"], "ok", reply)
                self.assertEqual(
                    reply["result"]["programming"]["verified_bytes"], len(APPROVED), reply
                )
                # The rebuild really landed on the original path...
                self.assertEqual(separate_loads(artifact, BASE), rebuilt)
                # ...but only the checked bytes reached flash; the added run is untouched.
                offset = added_start - BASE
                self.assertEqual(
                    bytes(f.target.memory[offset : offset + len(added)]),
                    f.target.initial[offset : offset + len(added)],
                )
                self.assertEqual(f.target.memory, _only_approved_programmed(f))
                self.assertEqual(f.target.reads, _approved_reads())
                (program,) = [call for call in f.target.calls if call[0] == "program"]
                programmed = Path(program[1])
                self.assertNotEqual(programmed, artifact)
                self.assertEqual(programmed.name, artifact.name)
                self.assertFalse(programmed.exists())

    def test_unchanged_full_artifacts_still_program_every_load(self) -> None:
        """Valid control: no concurrent build, every sparse or appended byte verified."""
        for suffix in (".elf", ".axf", ".hex", ".bin"):
            with self.subTest(format=suffix), public_fixture(self) as f:
                runs, _added = _rebuilt_runs(suffix)
                artifact = write_artifact(f.root / ("full" + suffix), runs)

                reply = self.raw(f, artifact, suffix)

                self.assertEqual(reply["status"], "ok", reply)
                self.assertEqual(reply["result"]["programming"]["verified_bytes"], 9080, reply)
                for address, data in runs:
                    offset = address - BASE
                    self.assertEqual(bytes(f.target.memory[offset : offset + len(data)]), data)


class LegacyFlashKeepsItsResolvedIdentity(unittest.TestCase):
    """The retained internal helper is not a registered public MCP flash tool."""

    def test_rebuild_after_resolution_refuses_before_any_effect(self) -> None:
        with public_fixture(self) as f:
            artifact = write_artifact(f.root / "legacy.elf", ((BASE, APPROVED),))
            digest = _sha256(artifact)
            flash = target_control.flash_firmware

            def rebuild_then_flash(
                handle: TargetSessionHandle, firmware: Path, **kwargs: Any
            ) -> tuple[Path, FlashResult]:
                write_artifact(artifact, fixture_runs())
                return flash(handle, firmware, **kwargs)

            with (
                patch.object(target_control, "flash_firmware", rebuild_then_flash),
                self.assertRaises(FlashArtifactChangedError) as refused,
            ):
                f.server.flash_firmware(BOARD, str(artifact))

            self.assertIn(digest, str(refused.exception))
            self.assertEqual(f.target.calls, [])
            self.assertEqual(bytes(f.target.memory), f.target.initial)
            rows = [
                json.loads(row)
                for path in f.root.glob(".firm/runs/*/logs/events.jsonl")
                for row in path.read_text(encoding="utf-8").splitlines()
                if json.loads(row).get("tool_name") == "flash_firmware"
            ]
            self.assertEqual(rows[-1]["outcome_kind"], "failed", rows)
            self.assertEqual(rows[-1]["normalized_args"]["artifact_sha256"], digest)
            self.assertFalse(any(row["outcome_kind"] == "success" for row in rows), rows)

    def test_unchanged_artifact_is_programmed_and_fully_verified(self) -> None:
        with public_fixture(self) as f:
            artifact = write_artifact(f.root / "legacy.elf", ((BASE, APPROVED),))

            reply = f.server.flash_firmware(BOARD, str(artifact))

            self.assertIn(str(artifact), reply)
            self.assertIn(str(len(APPROVED)), reply)
            self.assertEqual(f.target.memory, _only_approved_programmed(f))
            self.assertEqual(f.target.reads, _approved_reads())


class GuardedFlashProgramsTheApprovedContent(unittest.TestCase):
    def state(self, f: Any) -> GenericSafetyMapDocument:
        return GenericSafetyMapDocument.from_document(
            f.server._capability_policies.resolve(BOARD).map_snapshot
        )

    def flash_rows(self, f: Any) -> list[dict[str, Any]]:
        rows = [
            json.loads(row)
            for path in f.root.glob(".firm/runs/*/logs/events.jsonl")
            for row in path.read_text(encoding="utf-8").splitlines()
        ]
        return [row for row in rows if row.get("tool_name") == "flash_application"]

    def approved(self, f: Any) -> tuple[Path, str]:
        artifact = write_artifact(f.root / "guarded.elf", ((BASE, APPROVED),))
        self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
        return artifact, _sha256(artifact)

    def flash(self, f: Any, artifact: Path) -> str:
        return f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})

    def assert_refused_before_any_effect(self, f: Any, reply: str, digest: str) -> None:
        self.assertIn("flash/artifact-changed", reply)
        self.assertIn("nothing was reset, erased or programmed", reply)
        self.assertIn(digest, reply)
        self.assertEqual(f.target.calls, [])
        self.assertEqual(bytes(f.target.memory), f.target.initial)
        self.assertNotIn(BOARD, f.server._pending_generic_allocations)
        self.assertNotIn(BOARD, f.server._current_symbol_artifacts)
        rows = self.flash_rows(f)
        self.assertEqual(rows[-1]["outcome_kind"], "refused", rows)
        self.assertEqual(rows[-1]["error_code"], "flash/artifact-changed", rows)
        self.assertFalse(any(row["outcome_kind"] == "success" for row in rows), rows)

    def test_rebuild_while_programming_stays_inside_the_approved_allocation(self) -> None:
        with public_fixture(self, guarded=True) as f:
            artifact, digest = self.approved(f)
            f.target.before_program = _rebuild(artifact, fixture_runs())

            reply = self.flash(f, artifact)

            self.assertIn(f"read back {len(APPROVED)} matching addressed bytes", reply)
            self.assertEqual(separate_loads(artifact), fixture_runs())
            application = self.state(f).partitions.application
            self.assertIsNotNone(application)
            assert application is not None
            self.assertEqual((application.start, application.end), (BASE, SECTOR_END))
            # Nothing reached the rebuilt run outside the accepted allocation.
            self.assertEqual(f.target.memory, _only_approved_programmed(f))
            self.assertEqual(f.target.reads, _approved_reads())
            # Result, event and symbol identity all name the approved content.
            self.assertEqual(f.server._current_symbol_artifacts[BOARD], (artifact, digest))
            success = self.flash_rows(f)[-1]
            self.assertEqual(success["outcome_kind"], "success", success)
            self.assertEqual(success["normalized_args"]["artifact_sha256"], digest)
            self.assertEqual(success["details"]["programming"]["verified_bytes"], len(APPROVED))

    def test_rebuild_after_containment_is_refused_before_allocation_or_effects(self) -> None:
        with public_fixture(self, guarded=True) as f:
            artifact, digest = self.approved(f)
            resolve = f.server.resolve_flash_request
            calls: list[int] = []

            def resolve_after_rebuild(*args: object, **kwargs: object) -> object:
                calls.append(1)
                if len(calls) == 2:  # containment approved; execution resolves again
                    write_artifact(artifact, fixture_runs())
                return resolve(*args, **kwargs)

            with patch.object(f.server, "resolve_flash_request", resolve_after_rebuild):
                reply = self.flash(f, artifact)

            self.assertEqual(len(calls), 2)
            self.assert_refused_before_any_effect(f, reply, digest)
            self.assertEqual(self.state(f).deployment_policy, {"kind": "none"})

    def test_rebuild_before_the_backend_reads_the_artifact_is_refused(self) -> None:
        cases = {
            # Before the live support check: no allocation is committed.
            "support check": ("check_flash_support", False),
            # Before programming: the approved-content allocation is already
            # durable, as for any failure after commit, but nothing is programmed.
            "programming": ("flash_firmware", True),
        }
        for name, (stage, committed) in cases.items():
            with self.subTest(name), public_fixture(self, guarded=True) as f:
                artifact, digest = self.approved(f)
                backend_stage = getattr(target_control, stage)

                def rebuild_then_call(*args: object, **kwargs: object) -> object:
                    write_artifact(artifact, fixture_runs())
                    return backend_stage(*args, **kwargs)

                with patch.object(target_control, stage, rebuild_then_call):
                    reply = self.flash(f, artifact)

                self.assert_refused_before_any_effect(f, reply, digest)
                document = self.state(f)
                if committed:
                    application = document.partitions.application
                    assert application is not None
                    self.assertEqual((application.start, application.end), (BASE, SECTOR_END))
                else:
                    self.assertEqual(document.deployment_policy, {"kind": "none"})


class WorkerBoundaryCarriesTheApprovedDigest(unittest.TestCase):
    DIGEST = "ab" * 32

    def test_proxy_worker_and_adapter_pass_the_digest_through(self) -> None:
        sent: list[tuple[str, dict[str, Any]]] = []

        def call(
            _self: object, _handle: object, operation: str, arguments: Any, **_: object
        ) -> None:
            sent.append((operation, arguments))

        interface = swd_process.ProcessIsolatedSWDInterface()
        handle: Any = object()
        with patch.object(swd_process.ProcessIsolatedSWDInterface, "_call", new=call):
            interface.check_flash_support(handle, Path("app.elf"), artifact_sha256=self.DIGEST)
            interface.flash(
                handle, Path("app.elf"), halt_after_reset=False, artifact_sha256=self.DIGEST
            )

        adapter = Mock()
        adapter.flash.return_value = FlashResult(((BASE, BASE + 1),), "running")
        for operation, arguments in sent:
            provider_worker_runtime._validate_arguments(operation, arguments)
            provider_worker_runtime._dispatch(adapter, handle, operation, arguments)
        adapter.check_flash_support.assert_called_once_with(
            handle, Path("app.elf"), base_address=None, artifact_sha256=self.DIGEST
        )
        adapter.flash.assert_called_once_with(
            handle,
            Path("app.elf"),
            halt_after_reset=False,
            base_address=None,
            artifact_sha256=self.DIGEST,
        )

    def test_worker_refuses_a_missing_or_non_text_digest(self) -> None:
        valid = {"path": "app.elf", "base_address": None, "artifact_sha256": None}
        provider_worker_runtime._validate_arguments("check_flash_support", valid)
        for arguments in (
            {"path": "app.elf", "base_address": None},
            {**valid, "artifact_sha256": 5},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                provider_worker_runtime._validate_arguments("check_flash_support", arguments)

    def test_changed_artifact_keeps_its_type_across_the_worker(self) -> None:
        kind = provider_worker_runtime._error_kind(FlashArtifactChangedError("changed"))
        self.assertEqual(kind, "flash_artifact_changed")
        self.assertIs(swd_process._ERROR_TYPES[kind], FlashArtifactChangedError)
        self.assertEqual(
            provider_worker_runtime._error_kind(UnsupportedArtifactError("bad")),
            "unsupported_artifact",
        )


class FlashArtifactSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.artifact = Path(temporary.name) / "app.hex"
        self.artifact.write_bytes(b"approved")

    def test_snapshot_is_a_private_copy_removed_afterwards(self) -> None:
        for expected in (None, hashlib.sha256(b"approved").hexdigest()):
            with self.subTest(expected=expected):
                with snapshot_flash_artifact(self.artifact, expected_sha256=expected) as snapshot:
                    self.artifact.write_bytes(b"rebuilt")
                    self.assertNotEqual(snapshot, self.artifact)
                    self.assertEqual(snapshot.name, self.artifact.name)
                    self.assertEqual(snapshot.read_bytes(), b"approved")
                self.assertFalse(snapshot.exists())
                self.artifact.write_bytes(b"approved")

    def test_other_content_is_refused_before_the_snapshot_is_used(self) -> None:
        used: list[Path] = []
        with self.assertRaisesRegex(
            FlashArtifactChangedError, "changed after it was approved.*nothing was reset"
        ):
            with snapshot_flash_artifact(self.artifact, expected_sha256="0" * 64) as snapshot:
                used.append(snapshot)
        self.assertEqual(used, [])

    def test_unreadable_artifact_is_an_unsupported_artifact(self) -> None:
        with self.assertRaisesRegex(UnsupportedArtifactError, "Cannot read flash artifact"):
            with snapshot_flash_artifact(self.artifact.with_name("missing.hex")):
                pass


if __name__ == "__main__":
    unittest.main()
