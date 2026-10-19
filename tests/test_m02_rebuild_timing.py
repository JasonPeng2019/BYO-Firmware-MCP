"""Independent public C01 regressions for a cooperative rebuild of its own file.

Expected bytes come from fixture source and the programmer's separate parser.
Rebuilds touch only the original build output. No assertion requires a particular
snapshot implementation: safe captured programming or pre-effect refusal is valid.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from unittest import TestCase
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.safety.map_build import GenericSafetyMapDocument
from pyocd_debug_mcp.services import target_control
from tests.m02_independent_support import (
    BASE,
    BOARD,
    fixture_runs,
    plan,
    public_fixture,
    separate_loads,
    write_artifact,
)

ORIGINAL = fixture_runs()[:1]
REBUILT = fixture_runs()
ALLOCATION_END = BASE + 0x2800


def events(f, tool):
    return [
        row
        for path in f.root.glob(".firm/runs/*/logs/events.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line)).get("tool_name") == tool
    ]


def document(f):
    return GenericSafetyMapDocument.from_document(
        f.server._capability_policies.resolve(BOARD).map_snapshot
    )


@contextmanager
def rebuild_at(f, artifact, boundary, *, guarded):
    """Pause ordinary execution at a boundary, rebuild its ORIGINAL output, resume."""
    changed = []

    def rebuild():
        if not changed:
            write_artifact(artifact, REBUILT)
            changed.append(True)

    if boundary == "programmer-read":
        f.target.before_program = rebuild
        yield changed
    elif boundary == "reset-after-support":
        reset = f.target.reset_and_halt

        def reset_then_rebuild():
            reset()
            rebuild()

        with patch.object(f.target, "reset_and_halt", reset_then_rebuild):
            yield changed
    elif boundary == "support-derived":
        check = swd_pyocd._require_flash_support
        calls = []

        def checked_then_rebuild(*args, **kwargs):
            result = check(*args, **kwargs)
            calls.append(True)
            # Guarded execution has a separate support preflight before the
            # final backend check. Rebuild only after the final check here.
            if len(calls) == (2 if guarded else 1):
                rebuild()
            return result

        with patch.object(swd_pyocd, "_require_flash_support", checked_then_rebuild):
            yield changed
    elif boundary == "preflight-complete":
        check = target_control.check_flash_support

        def preflight_then_rebuild(*args, **kwargs):
            result = check(*args, **kwargs)
            rebuild()
            return result

        with patch.object(target_control, "check_flash_support", preflight_then_rebuild):
            yield changed
    else:
        resolve = f.server.resolve_flash_request
        calls = []

        def resolved_then_rebuild(*args, **kwargs):
            result = resolve(*args, **kwargs)
            calls.append(True)
            if len(calls) == (1 if boundary == "admitted" else 2):
                rebuild()
            return result

        with patch.object(f.server, "resolve_flash_request", resolved_then_rebuild):
            yield changed


class IndependentRebuildTiming(TestCase):
    def make(self, f, suffix=".elf", *, full=False):
        artifact = write_artifact(
            f.root / ("cooperative-build" + suffix), REBUILT if full else ORIGINAL
        )
        self.assertEqual(separate_loads(artifact), REBUILT if full else ORIGINAL)
        return artifact

    def assert_captured_or_refused(self, f, reply, artifact, digest, *, guarded):
        tool = "flash_application" if guarded else "flash_raw"
        rows = events(f, tool)
        if guarded:
            self.assertTrue(rows)
        if (
            "read back 9001 matching addressed bytes" in reply
            if guarded
            else reply["status"] == "ok"
        ):
            expected = bytearray(f.target.initial)
            for address, data in ORIGINAL:
                expected[address - BASE : address - BASE + len(data)] = data
            # Includes every hole and byte outside allocation, especially all
            # 79 bytes the cooperative rebuild added at 0x08004000.
            self.assertEqual(
                f.target.memory, expected, "programmed content exceeded the admitted image"
            )
            self.assertEqual(
                f.target.reads, [(BASE, 4096), (BASE + 4096, 4096), (BASE + 8192, 809)]
            )
            if guarded:
                self.assertEqual(rows[-1]["outcome_kind"], "success", rows)
                self.assertEqual(f.server._current_symbol_artifacts[BOARD], (artifact, digest))
                self.assertEqual(rows[-1]["normalized_args"]["artifact_sha256"], digest)
                proof = rows[-1]["details"]["programming"]
            else:
                self.assertEqual(reply["result"]["artifact"], str(artifact))
                proof = reply["result"]["programming"]
            self.assertEqual(proof["verified_bytes"], 9001)
            self.assertEqual(proof["ranges"], [{"start": "0x08000000", "end": "0x08002329"}])
        else:
            self.assertIn("flash/artifact-changed", str(reply))
            self.assertEqual(f.target.calls, [], "a refusal must precede reset/program effects")
            self.assertEqual(bytes(f.target.memory), f.target.initial)
            self.assertNotIn(BOARD, f.server._current_symbol_artifacts)
            self.assertFalse(any(r["outcome_kind"] == "success" for r in rows), rows)
        self.assertNotIn(BOARD, f.server._pending_generic_allocations)
        if guarded and (allocation := document(f).partitions.application) is not None:
            self.assertEqual((allocation.start, allocation.end), (BASE, ALLOCATION_END))

    def raw_timing(self, boundary):
        # ELF/AXF and HEX carry the separate out-of-allocation second load.
        for suffix in (".elf", ".axf", ".hex"):
            with self.subTest(format=suffix, boundary=boundary), public_fixture(self) as f:
                artifact = self.make(f, suffix)
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                with rebuild_at(f, artifact, boundary, guarded=False) as changed:
                    reply = json.loads(
                        f.call("flash_raw", {"board_id": BOARD, "artifact": str(artifact)})
                    )
                self.assertEqual(changed, [True])
                self.assertEqual(separate_loads(artifact), REBUILT)
                self.assert_captured_or_refused(f, reply, artifact, digest, guarded=False)

    def guarded_timing(self, boundary):
        with public_fixture(self, guarded=True) as f:
            artifact = self.make(f)
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
            with rebuild_at(f, artifact, boundary, guarded=True) as changed:
                if boundary in {"admitted", "resolved"}:
                    # These earlier drifts are rejected by the retained plan or
                    # allocation guard before the backend snapshot is reached.
                    with self.assertRaisesRegex(ToolError, "changed|replaced"):
                        f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
                    self.assertEqual(f.target.calls, [])
                    self.assertEqual(bytes(f.target.memory), f.target.initial)
                    self.assertEqual(document(f).deployment_policy, {"kind": "none"})
                    self.assertNotIn(BOARD, f.server._current_symbol_artifacts)
                    self.assertFalse(
                        any(
                            row["outcome_kind"] == "success"
                            for row in events(f, "flash_application")
                        )
                    )
                    self.assertEqual(changed, [True])
                    self.assertEqual(separate_loads(artifact), REBUILT)
                    return
                reply = f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertEqual(changed, [True])
            self.assertEqual(separate_loads(artifact), REBUILT)
            self.assert_captured_or_refused(f, reply, artifact, digest, guarded=True)

    def test_raw_rebuild_after_support_derivation(self):
        self.raw_timing("support-derived")

    def test_raw_rebuild_after_reset_before_programmer(self):
        self.raw_timing("reset-after-support")

    def test_raw_rebuild_immediately_before_programmer_reads(self):
        self.raw_timing("programmer-read")

    def test_guarded_rebuild_after_admission(self):
        self.guarded_timing("admitted")

    def test_guarded_rebuild_after_execution_resolution(self):
        self.guarded_timing("resolved")

    def test_guarded_rebuild_after_support_preflight(self):
        self.guarded_timing("preflight-complete")

    def test_guarded_rebuild_after_final_support_derivation(self):
        self.guarded_timing("support-derived")

    def test_guarded_rebuild_after_reset_before_programmer(self):
        self.guarded_timing("reset-after-support")

    def test_guarded_rebuild_immediately_before_programmer_reads(self):
        self.guarded_timing("programmer-read")

    def test_unchanged_raw_sparse_image_programs_all_9080_bytes(self):
        with public_fixture(self) as f:
            artifact = self.make(f, full=True)
            reply = json.loads(f.call("flash_raw", {"board_id": BOARD, "artifact": str(artifact)}))
            self.assertEqual(reply["status"], "ok", reply)
            self.assertEqual(reply["result"]["programming"]["verified_bytes"], 9080)
            for address, data in REBUILT:
                self.assertEqual(
                    bytes(f.target.memory[address - BASE : address - BASE + len(data)]), data
                )

    def test_unchanged_guarded_original_programs_approved_content(self):
        with public_fixture(self, guarded=True) as f:
            artifact = self.make(f)
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
            reply = f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertIn("read back 9001 matching addressed bytes", reply)
            self.assert_captured_or_refused(f, reply, artifact, digest, guarded=True)

    def test_guarded_earlier_drift_before_admission_refuses_without_effects(self):
        with public_fixture(self, guarded=True) as f:
            artifact = self.make(f)
            self.assertEqual(plan(f, "flash_application", artifact)["status"], "plan_accepted")
            write_artifact(artifact, REBUILT)
            with self.assertRaisesRegex(Exception, "changed|replaced|artifact"):
                f.call("flash_application", {"board_id": BOARD, "artifact": str(artifact)})
            self.assertEqual(f.target.calls, [])
            self.assertEqual(bytes(f.target.memory), f.target.initial)
            self.assertEqual(document(f).deployment_policy, {"kind": "none"})
            self.assertNotIn(BOARD, f.server._current_symbol_artifacts)
