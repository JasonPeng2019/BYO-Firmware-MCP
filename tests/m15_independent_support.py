"""Independent M15 corpus; ordinary public setup/deployment creates all authority."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from tests.b02_public_support import elf_fixture
from tests.m05_public_support import NULL_PLAN, PublicSetup
from tests.m14_independent_support import ordinary_full
from tests.test_m05_lite_validation import advertised_validation
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import public_text, tiered_test_server


RAM = 0x20000000
VALUE = 0x6D143A72


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")


def selected(root, board):
    pointer = root / ".firm/capabilities" / board / "current.json"
    # The layout is observed from files, never authored as setup authority.
    if not pointer.exists():
        matches = list((root / ".firm").rglob("current.json"))
        matches = [p for p in matches if p.parent.name == board]
        assert len(matches) == 1, matches
        pointer = matches[0]
    payload = pointer.read_bytes()
    reference = json.loads(payload)
    generation = pointer.parent / "generations" / (reference["generation_id"] + ".json")
    data = generation.read_bytes()
    assert digest(data) == reference["generation_digest"]
    return SimpleNamespace(
        pointer=pointer,
        pointer_bytes=payload,
        path=generation,
        data=data,
        document=json.loads(data),
        reference=reference,
    )


def assert_committed(test, f):
    test.assertEqual(f.saved.pointer.read_bytes(), f.saved.pointer_bytes)
    test.assertEqual(f.saved.path.read_bytes(), f.saved.data)
    test.assertEqual(f.backup.read_bytes(), f.saved.data)
    state = f.server._capability_policies.resolve(f.board)
    test.assertEqual(state.profile_snapshot, f.saved.document["profile_snapshot"])
    test.assertEqual(state.map_snapshot, f.saved.document["map_snapshot"])
    test.assertEqual(state.evidence, f.saved.document["evidence"])
    test.assertEqual(list(state.retained_generations), f.saved.document["retained_generations"])


def planned_read(f):
    f.flow.call("read_memory_address-plan", NULL_PLAN)
    plan = f.flow.plan("read_memory_address", f.board, dict(address=RAM, width=32, length=None))
    assert plan["status"] == "plan_accepted", plan
    return plan, f.flow.execute(plan["stable_client_fallback"])


def assert_read(test, f):
    _, result = planned_read(f)
    test.assertEqual(result["status"], "batch_completed", result)
    text = result["completed"][0]["result"]
    test.assertEqual(int(text.splitlines()[0], 16), VALUE)


def assert_closed(test, f):
    before = len(f.backend.calls)
    for tool, arguments in (
        ("read_memory_address", dict(board_id=f.board, address=RAM, width=32, length=None)),
        ("read_memory_raw", dict(board_id=f.board, address=RAM, width=32, length=None)),
    ):
        try:
            reply = f.flow.call(tool, arguments)
        except ToolError as exc:
            reply = str(exc)
        test.assertIn("corrupt", reply.lower(), reply)
    test.assertEqual(len(f.backend.calls), before, f.backend.calls[before:])


def restore_route(test, f, source):
    before = len(f.backend.calls)
    caps = f.flow.json("get_capabilities", dict(board_id=f.board))
    recovery = caps.get("recovery")
    test.assertIsInstance(recovery, dict, caps)
    test.assertEqual(recovery["generation_id"], f.saved.reference["generation_id"])
    test.assertEqual(recovery["generation_digest"], digest(f.saved.data))
    load = recovery["load_call"]
    loaded = f.flow.json(load["tool"], load["arguments"])
    test.assertEqual(loaded["recovery"], recovery)
    result = f.flow.json(
        "board_policy_restore", dict(board_id=f.board, generation_backup_path=source)
    )
    test.assertEqual(len(f.backend.calls), before, f.backend.calls[before:])
    return result


@contextmanager
def corpus(test):
    """Reusable on later partition-bearing generations; compares complete records."""
    with ordinary_full(test) as f:
        f.events = []
        f.stale = selected(f.root, f.board).data
        f.artifact = elf_fixture(f.root / "recovery-workload.elf", base=0)
        f.flow.call("flash_application-plan", NULL_PLAN)
        accepted = f.flow.plan("flash_application", f.board, dict(artifact=str(f.artifact)))
        test.assertEqual(accepted["status"], "plan_accepted", accepted)
        reply = f.flow.call("flash_application", dict(board_id=f.board, artifact=str(f.artifact)))
        test.assertIn("read back 256 matching addressed bytes", reply, reply)
        f.saved = selected(f.root, f.board)
        test.assertNotEqual(f.saved.data, f.stale)
        test.assertNotEqual(f.saved.document["map_snapshot"]["deployment_policy"]["kind"], "none")
        test.assertEqual(advertised_validation(f.flow, f.board)["status"], "validation_passed")
        assert_read(test, f)
        f.target_before = dict(f.backend.programmed)
        f.backup = f.root / "owned-backups/selected-generation.json"
        f.backup.parent.mkdir()
        f.backup.write_bytes(f.saved.data)
        evidence = Path(os.environ["BYO_MANAGED_TEST_EVIDENCE"]) / test.id() / "independent"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "original-pointer.json").write_bytes(f.saved.pointer_bytes)
        (evidence / "original-generation.json").write_bytes(f.saved.data)
        try:
            yield f
        finally:
            from tests.m15_independent_evidence import capture_imports

            capture_imports()
            save_json(evidence / "events.json", f.events)
            save_json(
                evidence / "target.json", dict(before=f.target_before, after=f.backend.programmed)
            )
            save_json(evidence / "probe.json", f.backend.calls)
            if f.backup.exists():
                shutil.copyfile(f.backup, evidence / "exact-backup.json")
            for artifact in (f.artifact, f.root / "alternate.elf"):
                if artifact.exists():
                    shutil.copyfile(artifact, evidence / artifact.name)
            if (f.root / ".firm").exists():
                shutil.copytree(f.root / ".firm", evidence / ".firm", dirs_exist_ok=True)
            # Failure cleanup only; never credited as the public recovery proof.
            # Retain the failed state above before making old fixture cleanup usable.
            f.saved.pointer.write_bytes(f.saved.pointer_bytes)
            f.saved.path.write_bytes(f.saved.data)
            # Nested fresh imports must hand the outer generic fixture its
            # original module for that fixture's own import/backend cleanup.
            sys.modules["pyocd_debug_mcp.server"] = f.server
            setattr(sys.modules["pyocd_debug_mcp"], "server", f.server)


def lose(f):
    assert f.backup.read_bytes() == f.saved.data
    if f.server.connection_manager.maybe_connection(f.board):
        f.flow.call("disconnect", dict(board_id=f.board))
    f.saved.path.unlink()  # Only this fixture-owned selected generation.


def abrupt(test, f, point, *, action="restore"):
    from pyocd_debug_mcp.kernel.processes import _start_token, identity_matches

    proof = f.root / (point + ".json")
    argv = [
        sys.executable,
        "-B",
        "-m",
        "tests.m15_independent_fault",
        "--project",
        str(f.root),
        "--board",
        f.board,
        "--backup",
        str(f.backup),
        "--point",
        point,
        "--proof",
        str(proof),
        "--action",
        action,
    ]
    # The fixture working directory does not expose this checkout's test package.
    checkout = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(checkout), str(checkout / "src"))),
    }
    process = subprocess.Popen(
        argv, cwd=f.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    token = _start_token(process.pid)
    stdout, stderr = process.communicate()
    f.events.append(
        dict(
            abrupt=point,
            argv=argv,
            cwd=str(f.root),
            pid=process.pid,
            token=token,
            raw_exit=process.returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            exact_identity_absent=bool(token and not identity_matches(process.pid, token)),
            observed=json.loads(proof.read_text()) if proof.exists() else None,
        )
    )
    test.assertEqual(process.returncode, 86, stderr.decode(errors="replace"))
    test.assertTrue(token and not identity_matches(process.pid, token))
    test.assertTrue(proof.exists())


@contextmanager
def restart(f):
    if f.server.connection_manager.maybe_connection(f.board):
        f.flow.call("disconnect", dict(board_id=f.board))
    with tiered_test_server(f.root) as server:
        server.configure_tiered_test_seams(backend=f.backend)
        g = SimpleNamespace(**vars(f))
        g.server = server
        f.events.append(
            dict(
                restart="fresh imported server instance",
                run_id=server.server_run.run_id,
                host_pid=os.getpid(),
            )
        )

        def call(name, arguments):
            row = dict(tool=name, arguments=arguments, run_id=server.server_run.run_id)
            f.events.append(row)
            try:
                row["reply"] = public_text(server, name, arguments)
                return row["reply"]
            except Exception as exc:
                row["error"] = str(exc)
                raise

        g.flow = PublicSetup(server, call=call)
        with patch.object(server, "_hardware_inventory", _setup_inventory()):
            try:
                yield g
            finally:
                if server.connection_manager.maybe_connection(g.board):
                    g.flow.call("disconnect", dict(board_id=g.board))
                from tests.m15_independent_evidence import capture_imports

                capture_imports()
