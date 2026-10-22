"""Real-process regression coverage for L4's complete state transactions."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import tempfile
import threading
import types
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from pyocd_debug_mcp import remote_probes
from pyocd_debug_mcp.adapters import uart_pyserial
from pyocd_debug_mcp.capabilities import policy
from pyocd_debug_mcp.capabilities.policy import (
    CapabilityPolicyConflict, CapabilityPolicyRepository, Tier,
)
from pyocd_debug_mcp.capabilities.routing import TierRouteRefusal
from pyocd_debug_mcp.firmstore import cache, store
from pyocd_debug_mcp.firmstore.cache import AttachmentCache, ProbeIdentity, SerialEndpoint
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.kernel import file_transactions
from pyocd_debug_mcp.kernel.file_transactions import FileTransactionTimeout, file_transaction


def _identity(label):
    return ProbeIdentity("stlink", f"probe-{label}"), SerialEndpoint(
        f"port-{label}", f"uart-{label}", 0x1234, 0x5678
    )


def _wait(event):
    if not event.wait(15):
        raise AssertionError("coordinator did not release transaction barrier")


def _mutation(kind, root, label, *, paused=None, release=None, contended=None, results=None):
    """Pause the first *after* its actual read, detect the other's actual OS refusal.

    Parent releases the first only when the second has hit the locked sidecar.
    There are no timing sleeps or probabilistic simultaneous-write assertions.
    """
    root = Path(root)
    repo = CapabilityPolicyRepository(root)
    firm = FirmStore(root)
    attachments = AttachmentCache(firm)

    def after_read(value):
        if paused is not None:
            paused.set()
            _wait(release)
        return value

    original_try = file_transactions._try_lock

    def observe_contention(descriptor):
        try:
            original_try(descriptor)
        except OSError as exc:
            if contended is not None and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                contended.set()
            raise

    with ExitStack() as stack:
        stack.enter_context(patch.object(file_transactions, "_try_lock", observe_contention))
        if kind.startswith("registry"):
            original = remote_probes.load_remote_probes
            stack.enter_context(patch.object(
                remote_probes, "load_remote_probes", lambda path: after_read(original(path))
            ))
            if kind == "registry-remove" and label == "second":
                remote_probes.unregister_entry(firm.layout.remote_probes, "seed", 5555)
            else:
                remote_probes.register_entry(firm.layout.remote_probes, label, 5555, label)
        elif kind.startswith("cache"):
            original = attachments.load_records
            stack.enter_context(patch.object(
                attachments, "load_records", lambda: after_read(original())
            ))
            if kind == "cache-revoke" and label == "second":
                attachments.revoke("seed", *_identity("seed"))
            else:
                attachments.confirm(label, *_identity(label))
        elif kind == "manifest":
            def merge(path):
                document = yaml.safe_load(path.read_text()) if path.exists() else {"packs": []}
                after_read(document)
                document["packs"].append({"id": label})
                return document

            firm.update_pack_manifest(merge)
        elif kind == "policy":
            original = repo.resolve
            stack.enter_context(patch.object(
                repo, "resolve", lambda board: after_read(original(board))
            ))
            result = repo.downgrade_to_no_setup("controller")
            if results is not None:
                results.put((label, result.generation_id))
        else:
            raise AssertionError(kind)


def _hold(path, ready, release, crash=False):
    with file_transaction(Path(path)):
        ready.set()
        _wait(release)
        if crash:
            os._exit(79)


def _policy_crash(root, ready, release):
    def fault(stage):
        if stage == "before_pointer_replace":
            ready.set()
            _wait(release)
            os._exit(79)

    CapabilityPolicyRepository(Path(root), fault_hook=fault).downgrade_to_no_setup("controller")


def _fork_contender(path, result):
    try:
        with file_transaction(Path(path), timeout_seconds=0.1):
            result.send("incorrectly acquired inherited lock")
    except FileTransactionTimeout:
        result.send("busy")
    finally:
        result.close()


def _other_policy_change(root, board, output=None):
    repo = CapabilityPolicyRepository(Path(root))
    if output is None:
        repo.downgrade_to_no_setup(board)
        return
    try:
        with file_transaction(repo.pointer_path(board), timeout_seconds=0.1):
            repo.mark_setup_incomplete(board)
        result = "changed"
    except FileTransactionTimeout:
        result = "busy"
    Path(output).write_text(result)


def _pack_attempt(root, source, ready, release, output, fail):
    from pyocd_debug_mcp.setup_flow.packs import PackCandidateError, PackCandidatePipeline

    def live(_target, path, *_args):
        Path(output).write_text(json.dumps({"staged_path": str(path)}))
        ready.set()
        _wait(release)
        if fail:
            raise RuntimeError("independent validation failure")

    pipeline = PackCandidatePipeline(
        FirmStore(Path(root)), enumerate_targets=lambda *_: ("testtarget",), live_connect=live
    )
    try:
        validated = pipeline.validate(_pack_candidate(Path(source)), required_target="testtarget")
        pipeline.promote(validated, board_id="first")
    except PackCandidateError as exc:
        if not fail or exc.code != "package/live-connect-failed":
            raise


def _pack_candidate(source):
    from pyocd_debug_mcp.setup_flow.packs import PackCandidate
    return PackCandidate("shared", "1", "shared.pack", "https://example.invalid/shared.pack", source)


def _seed_full(root):
    from tests.tiered_acceptance_support import known_good_full_policy
    profile, memory_map = known_good_full_policy()
    repo = CapabilityPolicyRepository(root)
    initial = repo.commit(
        "legacy_full", Tier.SETUP_FULL, profile_snapshot=profile, map_snapshot=memory_map
    )
    for path, document in (
        (root / ".firm/boards/legacy_full.json", profile),
        (root / ".firm/safety/legacy_full/memory_map.yaml", memory_map),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    return initial, profile, memory_map


class CrossProcessStateTransactions(unittest.TestCase):
    def setUp(self):
        self.context = multiprocessing.get_context("spawn")
        self.children = []
        self.releases = []
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self._finish_children)

    def _finish_children(self):
        for event in self.releases:
            event.set()
        for child in self.children:
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join(5)
            child.close()

    def _start(self, target, *args, **kwargs):
        child = self.context.Process(target=target, args=args, kwargs=kwargs)
        child.start()
        self.children.append(child)
        return child

    def _event(self):
        event = self.context.Event()
        self.releases.append(event)
        return event

    def _pair(self, kind):
        paused = self.context.Event()
        release = self._event()
        contended = self.context.Event()
        results = self.context.Queue()
        self.addCleanup(results.close)
        first = self._start(
            _mutation, kind, str(self.root), "first", paused=paused, release=release, results=results
        )
        self.assertTrue(paused.wait(15), "first process never completed its in-transaction read")
        second = self._start(
            _mutation, kind, str(self.root), "second", contended=contended, results=results
        )
        self.assertTrue(contended.wait(15), "second writer never encountered the held OS lock")
        release.set()
        for child in (first, second):
            child.join(15)
            self.assertEqual(child.exitcode, 0, f"writer {child.pid} failed or remained running")
        return results

    def test_registry_registrations_preserve_both_updates(self):
        self._pair("registry")
        self.assertEqual(
            {row.host for row in remote_probes.load_remote_probes(FirmStore(self.root).layout.remote_probes)},
            {"first", "second"},
        )

    def test_registry_removal_merges_with_other_process_registration(self):
        path = FirmStore(self.root).layout.remote_probes
        remote_probes.register_entry(path, "seed", 5555, "")
        self._pair("registry-remove")
        self.assertEqual({row.host for row in remote_probes.load_remote_probes(path)}, {"first"})

    def test_attachment_confirmations_preserve_both_updates(self):
        self._pair("cache")
        rows = AttachmentCache(FirmStore(self.root)).load_records()
        self.assertEqual({row.board_id for row in rows}, {"first", "second"})

    def test_attachment_revocation_merges_with_other_process_confirmation(self):
        attachments = AttachmentCache(FirmStore(self.root))
        attachments.confirm("seed", *_identity("seed"))
        self._pair("cache-revoke")
        rows = {row.board_id: row for row in attachments.load_records()}
        self.assertEqual(set(rows), {"seed", "first"})
        self.assertFalse(rows["seed"].confirmed)
        self.assertTrue(rows["first"].confirmed)

    def test_manifest_merges_preserve_both_updates(self):
        self._pair("manifest")
        document = yaml.safe_load(FirmStore(self.root).layout.pack_manifest.read_text())
        self.assertEqual({entry["id"] for entry in document["packs"]}, {"first", "second"})

    def test_policy_mutations_reread_and_preserve_retained_generation_chain(self):
        repo = CapabilityPolicyRepository(self.root)
        initial = repo.resolve("controller")
        results = self._pair("policy")
        generations = dict(results.get(timeout=5) for _ in range(2))
        final = repo.resolve("controller")
        self.assertEqual(final.generation_id, generations["second"])
        self.assertEqual(final.retained_generations, (initial.generation_id, generations["first"]))
        self.assertTrue(final.setup_incomplete)

    def test_each_product_transaction_times_out_without_changing_data(self):
        firm = FirmStore(self.root)
        attachments = AttachmentCache(firm)
        repo = CapabilityPolicyRepository(self.root)
        remote_probes.register_entry(firm.layout.remote_probes, "seed", 5555, "")
        attachments.confirm("seed", *_identity("seed"))
        firm.atomic_write_pack_manifest({"packs": []})
        repo.resolve("controller")
        cases = (
            (remote_probes, firm.layout.remote_probes,
             lambda: remote_probes.register_entry(firm.layout.remote_probes, "new", 5555, "")),
            (cache, attachments.path, lambda: attachments.confirm("new", *_identity("new"))),
            (store, firm.layout.pack_manifest, lambda: firm.update_pack_manifest(
                lambda _path: self.fail("merge callback must not run before acquisition")
            )),
            (policy, repo.pointer_path("controller"), lambda: repo.downgrade_to_no_setup("controller")),
        )
        for module, path, operation in cases:
            with self.subTest(module=module.__name__):
                ready, release = self.context.Event(), self._event()
                holder = self._start(_hold, str(path), ready, release)
                self.assertTrue(ready.wait(15))
                before = self._data_snapshot()
                with patch.object(module, "file_transaction", lambda path: file_transaction(
                    path, timeout_seconds=0.1
                )):
                    with self.assertRaisesRegex(FileTransactionTimeout, "busy.*retry"):
                        operation()
                self.assertEqual(self._data_snapshot(), before)
                release.set()
                holder.join(10)
                self.assertEqual(holder.exitcode, 0)

    def _data_snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.endswith(".lock")
        }

    def test_process_death_releases_the_lock_and_persistent_sidecar_is_reusable(self):
        path = self.root / "state.json"
        ready, crash = self.context.Event(), self._event()
        child = self._start(_hold, str(path), ready, crash, True)
        self.assertTrue(ready.wait(15))
        crash.set()
        child.join(10)
        self.assertEqual(child.exitcode, 79)
        with file_transaction(path, timeout_seconds=0.5):
            path.write_text("recovered")
        self.assertEqual(path.read_text(), "recovered")
        self.assertTrue(path.with_name(".state.json.lock").exists())

    def test_policy_crash_before_pointer_releases_lock_and_keeps_previous_policy(self):
        repo = CapabilityPolicyRepository(self.root)
        initial = repo.resolve("controller")
        ready, crash = self.context.Event(), self._event()
        child = self._start(_policy_crash, str(self.root), ready, crash)
        self.assertTrue(ready.wait(15))
        crash.set()
        child.join(10)
        self.assertEqual(child.exitcode, 79)
        self.assertEqual(repo.resolve("controller"), initial)
        self.assertIn(initial.generation_id, repo.downgrade_to_no_setup("controller").retained_generations)

    def test_other_files_and_board_policies_remain_independent(self):
        firm = FirmStore(self.root)
        repo = CapabilityPolicyRepository(self.root)
        cases = (
            (firm.layout.remote_probes, lambda: remote_probes.register_entry(
                self.root / "other-registry.json", "other", 5555, ""
            )),
            (AttachmentCache(firm).path, lambda: firm.update_pack_manifest(lambda _p: {"packs": []})),
            (repo.pointer_path("controller"), lambda: repo.commit("other_board", Tier.NO_SETUP)),
        )
        for held, operation in cases:
            with self.subTest(held=held):
                ready, release = self.context.Event(), self._event()
                holder = self._start(_hold, str(held), ready, release)
                self.assertTrue(ready.wait(15))
                operation()
                release.set()
                holder.join(10)
                self.assertEqual(holder.exitcode, 0)

    def test_conditional_policy_mutations_refuse_stale_state_without_writes(self):
        repo = CapabilityPolicyRepository(self.root)
        initial = repo.resolve("controller")
        changed = repo.mark_setup_incomplete("controller")
        before = self._data_snapshot()
        operations = (
            lambda: repo.commit("controller", Tier.NO_SETUP, expected_state=initial),
            lambda: repo.downgrade_to_no_setup("controller", expected_state=initial),
            lambda: repo.mark_setup_incomplete("controller", expected_state=initial),
        )
        for operation in operations:
            with self.assertRaisesRegex(CapabilityPolicyConflict, "policy changed.*restart"):
                operation()
            self.assertEqual(self._data_snapshot(), before)
        self.assertEqual(repo.resolve("controller"), changed)
        repo.pointer_path("controller").unlink()
        before = self._data_snapshot()
        with self.assertRaises(CapabilityPolicyConflict):
            repo.commit("controller", Tier.NO_SETUP, expected_state=changed)
        self.assertEqual(self._data_snapshot(), before, "conditional refusal must not initialize a pointer")

    def test_pack_attempts_keep_private_bytes_while_another_server_publishes(self):
        from pyocd_debug_mcp.pack_provision import load_manifest
        from pyocd_debug_mcp.setup_flow.packs import PackCandidatePipeline

        source = self.root / "source.pack"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("shared.pdsc", "<package><name>shared</name></package>")
        for fail in (True, False):
            with self.subTest(first_validation_fails=fail):
                project = self.root / str(fail)
                ready, release = self.context.Event(), self._event()
                output = self.root / f"staged-{fail}.json"
                child = self._start(
                    _pack_attempt, str(project), str(source), ready, release, str(output), fail
                )
                self.assertTrue(ready.wait(15))
                first_path = Path(json.loads(output.read_text())["staged_path"])
                firm = FirmStore(project)
                second = PackCandidatePipeline(
                    firm, enumerate_targets=lambda *_: ("testtarget",), live_connect=lambda *_: None
                )
                validated = second.validate(_pack_candidate(source), required_target="testtarget")
                self.assertNotEqual(first_path, validated.staged_path)
                second.promote(validated, board_id="second")
                self.assertTrue(first_path.exists(), "publishing must not consume another pending attempt")
                release.set()
                child.join(15)
                self.assertEqual(child.exitcode, 0)
                final = firm.layout.pack_files / "shared.pack"
                self.assertEqual(final.read_bytes(), source.read_bytes())
                specs = load_manifest(firm.layout.pack_manifest)
                self.assertEqual(len(specs), 1)
                self.assertEqual(set(specs[0].needed_by_boards), {"second"} if fail else {"first", "second"})
                self.assertFalse(first_path.exists())
                self.assertFalse(validated.staged_path.exists())

    def test_failed_pack_publication_rolls_back_only_owned_bytes(self):
        from pyocd_debug_mcp.setup_flow.packs import PackCandidatePipeline

        source = self.root / "source.pack"
        source.write_bytes(b"bounded fixture pack bytes")
        firm = FirmStore(self.root)

        def pipeline():
            return PackCandidatePipeline(
                firm, enumerate_targets=lambda *_: ("testtarget",), live_connect=lambda *_: None
            )

        first, pending = pipeline(), pipeline()
        one = first.validate(_pack_candidate(source), required_target="testtarget")
        two = pending.validate(_pack_candidate(source), required_target="testtarget")
        with patch.object(firm, "atomic_write_pack_manifest", side_effect=OSError("manifest write failed")):
            with self.assertRaisesRegex(OSError, "manifest write failed"):
                first.promote(one, board_id="first")
        final = firm.layout.pack_files / "shared.pack"
        self.assertFalse(final.exists())
        self.assertFalse(one.staged_path.exists())
        self.assertTrue(two.staged_path.exists())
        pending.promote(two, board_id="second")
        third = pipeline()
        three = third.validate(_pack_candidate(source), required_target="testtarget")
        before = firm.layout.pack_manifest.read_bytes()
        with patch.object(firm, "atomic_write_pack_manifest", side_effect=OSError("manifest write failed")):
            with self.assertRaises(OSError):
                third.promote(three, board_id="third")
        self.assertEqual(final.read_bytes(), source.read_bytes())
        self.assertEqual(firm.layout.pack_manifest.read_bytes(), before)

    def test_allocation_cannot_publish_or_restamp_after_other_process_downgrade(self):
        from tests.tiered_acceptance_support import tiered_test_server
        from pyocd_debug_mcp.safety.map_build import GenericSafetyMapDocument

        _initial, _profile, memory_map = _seed_full(self.root)
        with tiered_test_server(self.root) as server:
            active = Mock(spec=GenericSafetyMapDocument)
            active.canonical_digest = "a" * 64
            selected = Mock()
            selected.to_document.return_value = memory_map
            pending = server._PendingGenericAllocation("a" * 64, "c" * 64, "connection", selected)

            def projected(_board):
                child = self._start(_other_policy_change, str(self.root), "legacy_full")
                child.join(15)
                self.assertEqual(child.exitcode, 0)
                return active

            with (
                patch.object(server, "_active_full_document", return_value=active),
                patch.object(server._safety_repository, "load_current", side_effect=projected),
                patch.object(server._safety_repository, "commit_if_current") as projection,
                patch.object(server.gate_manager, "refresh_map_stamp") as stamp,
            ):
                with self.assertRaises(server.SafetyPolicyError) as refused:
                    server._commit_generic_allocation("legacy_full", pending)
                self.assertEqual(refused.exception.code, "safety/allocation-proof-stale")
                projection.assert_not_called()
                stamp.assert_not_called()
            self.assertEqual(server._capability_policies.resolve("legacy_full").tier, Tier.NO_SETUP)

    def test_setup_validation_cannot_restore_other_process_downgrade(self):
        from tests.tiered_acceptance_support import tiered_test_server
        from pyocd_debug_mcp.setup_flow.preflight import PreflightDecision, SetupUserInput
        from pyocd_debug_mcp.setup_flow.setup import SetupPhaseContext

        _seed_full(self.root)
        with tiered_test_server(self.root) as server:
            board = "legacy_full"
            server._setup_tiers[board] = Tier.SETUP_FULL
            context = SetupPhaseContext(
                "continuation", "attempt", "repair",
                SetupUserInput(board, "connection", "Fixture", "STM32L476RGT6", None, requires_uart=False),
                PreflightDecision("preflight_ready", "setup/preflight-ready", "ready"), {},
            )
            downgraded = []

            def validate(_request):
                child = self._start(_other_policy_change, str(self.root), board)
                child.join(15)
                self.assertEqual(child.exitcode, 0)
                downgraded.append(server._capability_policies.resolve(board))
                return types.SimpleNamespace(status="validation_passed")

            with (
                patch.object(server, "_validation_safety_snapshot", return_value=object()),
                patch.object(server._board_validator, "validate", side_effect=validate),
                patch.object(server._safety_repository, "commit") as projection,
            ):
                outcome = server._tiered_setup_phase(server._setup_commit_phase)(context)
            self.assertFalse(outcome.verified)
            self.assertEqual(outcome.code, "setup/policy-changed")
            self.assertEqual(server._capability_policies.resolve(board), downgraded[0])
            projection.assert_not_called()

    def test_public_downgrade_consumes_digest_and_publishes_in_one_transaction(self):
        from tests.tiered_acceptance_support import tiered_test_server

        initial, _profile, _memory_map = _seed_full(self.root)
        with tiered_test_server(self.root) as server:
            permissions = server._manual_permissions
            permissions.write_unlocked_grant(
                action="downgrade", grant_id="grant", board_id="legacy_full",
                policy_digest=initial.policy_digest,
            )
            token = permissions.issue_downgrade_token("legacy_full", "grant", initial.policy_digest)
            consume = permissions.consume_downgrade
            output = self.root / "contender-result"

            def consumed(*args):
                consume(*args)
                child = self._start(_other_policy_change, str(self.root), "legacy_full", str(output))
                child.join(15)
                self.assertEqual(child.exitcode, 0)

            with patch.object(permissions, "consume_downgrade", side_effect=consumed):
                result = json.loads(server.downgrade("legacy_full", token))
            self.assertEqual(result["status"], "downgraded")
            self.assertEqual(output.read_text(), "busy")
            self.assertEqual(permissions.status("downgrade").state, "consumed")

    @unittest.skipUnless(hasattr(os, "fork"), "requires native POSIX fork/flock")
    def test_fork_does_not_inherit_reentrant_ownership(self):
        context = multiprocessing.get_context("fork")
        read, write = context.Pipe(duplex=False)
        self.addCleanup(read.close)
        self.addCleanup(write.close)
        path = self.root / "state.json"
        with file_transaction(path):
            child = context.Process(target=_fork_contender, args=(str(path), write))
            child.start()
            self.children.append(child)
            self.assertTrue(read.poll(5))
            self.assertEqual(read.recv(), "busy")
            child.join(5)
            self.assertEqual(child.exitcode, 0)
        with file_transaction(path, timeout_seconds=0.1):
            pass


class LocalTransactionsAndUART(unittest.TestCase):
    def test_nested_contexts_and_repository_instances_share_reentrant_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one, two = CapabilityPolicyRepository(root), CapabilityPolicyRepository(root)
            with one.lock_for("controller"), two.lock_for("controller"):
                initial = one.resolve("controller")
                changed = two.downgrade_to_no_setup("controller")
                marked = one.mark_setup_incomplete("controller")
                self.assertEqual(changed, marked)
                self.assertIn(initial.generation_id, changed.retained_generations)
            self.assertEqual(two.resolve("controller"), changed)
            firm = FirmStore(root)
            other_firm = FirmStore(root)
            with file_transaction(firm.layout.pack_manifest):
                other_firm.update_pack_manifest(lambda _p: {"packs": []})
            self.assertEqual(yaml.safe_load(firm.layout.pack_manifest.read_text()), {"packs": []})
            attachments = AttachmentCache(firm)
            with file_transaction(attachments.path):
                AttachmentCache(other_firm).confirm("controller", *_identity("controller"))
            self.assertEqual(attachments.load_records()[0].board_id, "controller")

    def test_body_refusals_preserve_exact_exception_and_release_nested_locks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            errors = (
                TierRouteRefusal("tier/wrong-route", "already no-setup"),
                ValueError("body failure"),
                StopIteration("body stop"),
                SystemExit("body exit"),
            )
            for error in errors:
                with self.subTest(error=type(error).__name__):
                    caught = None
                    original_traceback = None
                    try:
                        with file_transaction(path), file_transaction(path):
                            try:
                                raise error
                            except BaseException:
                                original_traceback = error.__traceback__
                                raise
                    except BaseException as observed:
                        caught = observed
                    self.assertIs(caught, error)
                    self.assertIsNotNone(original_traceback)
                    self.assertIs(error.__traceback__, original_traceback)
                    with file_transaction(path, timeout_seconds=0):
                        pass

    def test_local_thread_contention_is_bounded_and_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state"
            result = []

            def contender():
                try:
                    with file_transaction(path, timeout_seconds=0.1):
                        result.append("unexpected acquisition")
                except FileTransactionTimeout:
                    result.append("busy")

            with self.assertRaisesRegex(ValueError, "operation failed"):
                with file_transaction(path):
                    thread = threading.Thread(target=contender)
                    thread.start()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result, ["busy"])
                    raise ValueError("operation failed")
            with file_transaction(path, timeout_seconds=0.1):
                pass

    def test_uart_requests_exclusive_only_for_native_posix_backend(self):
        for platform, backend, exclusive in (
            ("posix", "serial.serialposix", True),
            ("nt", "serial.serialwin32", False),
            ("posix", "serial.serialjava", False),
        ):
            with self.subTest(platform=platform, backend=backend):
                calls = []

                def initialize(instance, *args, **kwargs):
                    calls.append((args, kwargs))

                serial_class = type("Serial", (), {"__module__": backend, "__init__": initialize})
                with patch.dict("sys.modules", {"serial": types.SimpleNamespace(Serial=serial_class)}):
                    with patch.object(uart_pyserial, "os", types.SimpleNamespace(name=platform)):
                        handle = uart_pyserial.PySerialUARTInterface().open(
                            "COM7" if platform == "nt" else "/dev/ttyUSB0",
                            baudrate=115200, timeout_seconds=0.25,
                        )
                args, options = calls.pop()
                self.assertEqual(args, (handle.device,))
                self.assertEqual(options.pop("baudrate"), 115200)
                self.assertEqual(options.pop("timeout"), 0.25)
                self.assertEqual(options.pop("write_timeout"), 0.25)
                self.assertEqual(options, {"exclusive": True} if exclusive else {})

    @unittest.skipUnless(os.name == "posix", "requires native POSIX pseudo-terminal/pyserial")
    def test_native_posix_uart_exclusivity_and_release(self):
        import pty
        import serial

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        device = os.ttyname(slave)
        adapter = uart_pyserial.PySerialUARTInterface()
        handle = adapter.open(device, baudrate=115200, timeout_seconds=0.1)
        try:
            with self.assertRaises(serial.SerialException):
                adapter.open(device, baudrate=115200, timeout_seconds=0.1)
        finally:
            adapter.close(handle)
        reopened = adapter.open(device, baudrate=115200, timeout_seconds=0.1)
        adapter.close(reopened)


if __name__ == "__main__":
    unittest.main()
