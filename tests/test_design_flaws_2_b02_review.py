"""Independent B02 acceptance with raw OS holders and ordered process barriers.

No product changes, fabricated OS locks, hardware, or probabilistic write races.
"""
from __future__ import annotations

import errno
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from pyocd_debug_mcp import remote_probes
from pyocd_debug_mcp.capabilities.policy import (
    CapabilityPolicyConflict, CapabilityPolicyRepository, Tier,
)
from pyocd_debug_mcp.firmstore.cache import AttachmentCache, ProbeIdentity, SerialEndpoint
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.kernel import file_transactions as transactions


def wait(event):
    if not event.wait(35):
        raise AssertionError("process barrier deadline")


def raw_holder(path, ready, release):
    """Independent native lock: does not call the product helper."""
    sidecar = Path(path).with_name(f".{Path(path).name}.lock")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(sidecar, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if not os.fstat(descriptor).st_size:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ready.set()
        wait(release)
    finally:
        os.close(descriptor)


def identity(name):
    return ProbeIdentity("stlink", "probe-" + name), SerialEndpoint(
        "port-" + name, "serial-" + name, 0x1234, 0x5678
    )


def mutation(root, kind, ready, release):
    """A paused first read or pointer boundary is inside the actual transaction."""
    root = Path(root)
    firm = FirmStore(root)
    if kind == "registry":
        real_load = remote_probes.load_remote_probes

        def read(path):
            result = real_load(path)
            ready.set()
            wait(release)
            return result

        with patch.object(remote_probes, "load_remote_probes", read):
            remote_probes.register_entry(firm.layout.remote_probes, "first.example", 3333, "first")
    elif kind == "cache":
        cache = AttachmentCache(firm)
        real_load = cache.load_records

        def read():
            result = real_load()
            ready.set()
            wait(release)
            return result

        with patch.object(cache, "load_records", read):
            cache.confirm("first", *identity("first"), confirmed_at="2026-01-01T00:00:00+00:00")
    elif kind == "manifest":
        def merge(path):
            result = yaml.safe_load(path.read_text())
            ready.set()
            wait(release)
            result["packs"].append({"id": "first"})
            return result

        firm.update_pack_manifest(merge)
    elif kind == "policy":
        def fault(stage):
            if stage == "before_pointer_replace":
                ready.set()
                wait(release)

        CapabilityPolicyRepository(root, fault_hook=fault).downgrade_to_no_setup("board")
    else:
        raise AssertionError(kind)


def refresh_racer(root, ready, release, output):
    """Only expensive derivation/projection are isolated; policy IO remains real."""
    from tests.tiered_acceptance_support import known_good_full_policy, tiered_test_server
    from pyocd_debug_mcp.safety.map_build import SafetyMapDocument
    _, memory_map = known_good_full_policy()
    document = SafetyMapDocument.from_document(memory_map)

    def derive(*args, **kwargs):
        ready.set()
        wait(release)
        return document

    with tiered_test_server(Path(root)) as server:
        with (patch.object(server, "_active_full_document", return_value=document),
              patch.object(server, "_derive_safety_map", derive),
              patch.object(server._safety_repository, "commit"),
              patch.object(server, "_restamp_after_refresh")):
            result = server._run_board_safety_refresh("legacy_full")
        Path(output).write_text(json.dumps(result), encoding="utf-8")


class ProcessCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.context = multiprocessing.get_context("spawn")
        self.children = []
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.finish)

    def finish(self):
        # Tests release their own successful barriers. A terminated waiter can
        # corrupt an Event's condition, so teardown never signals those Events.
        for child in self.children:
            child.join(4)
            if child.is_alive():
                child.terminate()
                child.join(4)
            child.close()

    def start(self, target, *args):
        child = self.context.Process(target=target, args=args)
        self.children.append(child)
        child.start()
        return child

    def events(self):
        ready, release = self.context.Event(), self.context.Event()
        return ready, release

    def done(self, child):
        child.join(15)
        self.assertFalse(child.is_alive(), "child exceeded finite deadline")
        self.assertEqual(child.exitcode, 0)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(".lock")}


class IndependentCore(ProcessCase):
    def test_raw_native_lock_blocks_complete_transactions_and_preserves_bytes(self):
        firm = FirmStore(self.root)
        cache = AttachmentCache(firm)
        policy = CapabilityPolicyRepository(self.root)
        remote_probes.register_entry(firm.layout.remote_probes, "seed", 3333, "")
        cache.confirm("seed", *identity("seed"))
        firm.atomic_write_pack_manifest({"packs": []})
        policy.resolve("board")
        cases = (
            (firm.layout.remote_probes, lambda: remote_probes.register_entry(firm.layout.remote_probes, "other", 3333, "")),
            (cache.path, lambda: cache.revoke("seed", *identity("seed"))),
            (firm.layout.pack_manifest, lambda: firm.update_pack_manifest(lambda _: self.fail("callback ran before acquiring lock"))),
            (policy.pointer_path("board"), lambda: policy.mark_setup_incomplete("board")),
        )
        for path, operation in cases:
            with self.subTest(path=path.name):
                ready, release = self.events()
                child = self.start(raw_holder, str(path), ready, release)
                self.assertTrue(ready.wait(15))
                before = self.snapshot()
                start = time.monotonic()
                with self.assertRaisesRegex(transactions.FileTransactionTimeout, "busy.*retry"):
                    operation()
                self.assertLess(time.monotonic() - start, 7.0)
                self.assertEqual(self.snapshot(), before)
                release.set()
                self.done(child)

    def test_registry_direct_replacement_participates_in_same_transaction(self):
        path = FirmStore(self.root).layout.remote_probes
        remote_probes.register_entry(path, "seed", 3333, "")
        ready, release = self.events()
        child = self.start(mutation, str(self.root), "registry", ready, release)
        self.assertTrue(ready.wait(15))
        before = path.read_bytes()
        with self.assertRaises(transactions.FileTransactionTimeout):
            remote_probes.save_remote_probes(path, ())
        self.assertEqual(path.read_bytes(), before)
        release.set()
        self.done(child)
        self.assertEqual({p.host for p in remote_probes.load_remote_probes(path)}, {"seed", "first.example"})

    def test_manifest_direct_replacement_participates_and_callback_can_nest(self):
        firm = FirmStore(self.root)
        firm.atomic_write_pack_manifest({"packs": []})
        ready, release = self.events()
        child = self.start(mutation, str(self.root), "manifest", ready, release)
        self.assertTrue(ready.wait(15))
        before = firm.layout.pack_manifest.read_bytes()
        with self.assertRaises(transactions.FileTransactionTimeout):
            FirmStore(self.root).atomic_write_pack_manifest({"packs": [{"id": "replacement"}]})
        self.assertEqual(firm.layout.pack_manifest.read_bytes(), before)
        release.set()
        self.done(child)
        other = FirmStore(self.root)

        def merge(path):
            doc = yaml.safe_load(path.read_text())
            other.atomic_write_pack_manifest(doc)
            doc["packs"].append({"id": "nested"})
            return doc

        firm.update_pack_manifest(merge)
        self.assertEqual([p["id"] for p in yaml.safe_load(firm.layout.pack_manifest.read_text())["packs"]], ["first", "nested"])

    def test_sidecar_inode_survives_atomic_data_replacements_and_process_kill(self):
        path = self.root / "registry.json"
        remote_probes.register_entry(path, "one", 3333, "")
        sidecar = path.with_name(".registry.json.lock")
        inode = sidecar.stat().st_ino
        ready, release = self.events()
        child = self.start(raw_holder, str(path), ready, release)
        self.assertTrue(ready.wait(15))
        child.terminate()
        child.join(10)
        self.assertFalse(child.is_alive())
        remote_probes.register_entry(path, "two", 3333, "")
        remote_probes.unregister_entry(path, "one", 3333)
        self.assertEqual(sidecar.stat().st_ino, inode)
        self.assertEqual([p.host for p in remote_probes.load_remote_probes(path)], ["two"])

    def test_unrelated_files_boards_and_readers_do_not_wait_on_other_transaction(self):
        firm = FirmStore(self.root)
        cache = AttachmentCache(firm)
        cache.confirm("seed", *identity("seed"))
        ready, release = self.events()
        child = self.start(mutation, str(self.root), "cache", ready, release)
        self.assertTrue(ready.wait(15))
        start = time.monotonic()
        self.assertEqual([r.board_id for r in cache.load_records()], ["seed"])
        remote_probes.register_entry(firm.layout.remote_probes, "independent", 3333, "")
        repo = CapabilityPolicyRepository(self.root)
        with repo.lock_for("other"), CapabilityPolicyRepository(self.root).lock_for("other"):
            repo.resolve("other")
        self.assertLess(time.monotonic() - start, 2)
        release.set()
        self.done(child)
        self.assertEqual({r.board_id for r in cache.load_records()}, {"seed", "first"})

    def test_real_os_open_error_propagates_and_later_retry_works(self):
        path = self.root / "state.json"
        sidecar = path.with_name(".state.json.lock")
        sidecar.mkdir()
        with self.assertRaises(OSError) as result:
            with transactions.file_transaction(path):
                self.fail("failed OS open entered transaction")
        self.assertNotIsInstance(result.exception, transactions.FileTransactionTimeout)
        self.assertIn(result.exception.errno, {errno.EISDIR, errno.EACCES, errno.EPERM})
        sidecar.rmdir()
        with transactions.file_transaction(path, timeout_seconds=0):
            path.write_text("retry")
        self.assertEqual(path.read_text(), "retry")

    def test_noncontention_lock_error_is_not_translated_to_timeout(self):
        path = self.root / "state.json"
        error = OSError(errno.EBADF, "independent injected invalid descriptor")
        with patch.object(transactions, "_try_lock", side_effect=error):
            with self.assertRaises(OSError) as result:
                with transactions.file_transaction(path):
                    self.fail("invalid lock operation entered body")
        self.assertIs(result.exception, error)
        with transactions.file_transaction(path, timeout_seconds=0):
            pass

    def test_thread_refusal_and_same_thread_reentry_remain_bounded(self):
        path = self.root / "state.json"
        results = []

        def thread():
            try:
                with transactions.file_transaction(path, timeout_seconds=0.05):
                    results.append("incorrect")
            except transactions.FileTransactionTimeout:
                results.append("busy")

        with transactions.file_transaction(path), transactions.file_transaction(path, timeout_seconds=0):
            child = threading.Thread(target=thread)
            child.start()
            child.join(2)
            self.assertFalse(child.is_alive())
            self.assertEqual(results, ["busy"])

    def test_policy_unpublished_generation_is_invisible_and_crash_releases_lock(self):
        repo = CapabilityPolicyRepository(self.root)
        initial = repo.resolve("board")
        pointer_bytes = repo.pointer_path("board").read_bytes()
        ready, release = self.events()
        child = self.start(mutation, str(self.root), "policy", ready, release)
        self.assertTrue(ready.wait(15))
        self.assertEqual(repo.pointer_path("board").read_bytes(), pointer_bytes)
        self.assertEqual(len(list((repo.board_root("board") / "generations").glob("*.json"))), 2)
        self.assertEqual(repo.resolve("independent").board_id, "independent")
        child.terminate()
        child.join(10)
        self.assertFalse(child.is_alive())
        self.assertEqual(repo.resolve("board"), initial)
        changed = repo.downgrade_to_no_setup("board")
        self.assertIn(initial.generation_id, changed.retained_generations)


class CallerTransactions(ProcessCase):
    def test_safety_refresh_cannot_overwrite_a_completed_other_process_downgrade(self):
        """An earlier refresh must not resurrect full policy after a downgrade."""
        from tests.tiered_acceptance_support import known_good_full_policy
        profile, memory_map = known_good_full_policy()
        repo = CapabilityPolicyRepository(self.root)
        repo.commit("legacy_full", Tier.SETUP_FULL, profile_snapshot=profile, map_snapshot=memory_map)
        ready, release = self.events()
        output = self.root / "refresh-result.json"
        child = self.start(refresh_racer, str(self.root), ready, release, str(output))
        self.assertTrue(ready.wait(20), "refresh never reached derivation after reading full policy")
        downgraded = repo.downgrade_to_no_setup("legacy_full")
        self.assertEqual(repo.resolve("legacy_full"), downgraded)
        release.set()
        self.done(child)
        result = json.loads(output.read_text())
        final = repo.resolve("legacy_full")
        self.assertEqual(final, downgraded,
                         f"refresh changed completed downgrade; result={result}; final={final}")
        self.assertEqual(result["status"], "safety_refresh_blocked")


class RepairAcceptance(ProcessCase):
    def assert_native_reacquisition(self, path):
        ready, release = self.events()
        child = self.start(raw_holder, str(path), ready, release)
        self.assertTrue(ready.wait(15), "another process could not reacquire the native lock")
        release.set()
        self.done(child)

    def test_frozen_and_base_exceptions_preserve_identity_and_release_for_other_process(self):
        from pyocd_debug_mcp.capabilities.routing import TierRouteRefusal
        path = self.root / "exceptions.json"
        for error in (TierRouteRefusal("tier/wrong-route", "already raw"),
                      OSError(errno.EIO, "body IO failure"), SystemExit(17), StopIteration("stop")):
            observed = original_traceback = None
            try:
                with transactions.file_transaction(path), transactions.file_transaction(path):
                    try:
                        raise error
                    except BaseException:
                        original_traceback = error.__traceback__
                        raise
            except BaseException as caught:
                observed = caught
            self.assertIs(observed, error)
            self.assertIs(error.__traceback__, original_traceback)
            self.assert_native_reacquisition(path)

    def test_identical_policy_digest_is_not_permission_to_publish_from_old_generation(self):
        repo = CapabilityPolicyRepository(self.root)
        first = repo.commit("board", Tier.NO_SETUP)
        second = repo.commit("board", Tier.NO_SETUP)
        self.assertEqual(first.policy_digest, second.policy_digest)
        self.assertNotEqual(first.generation_id, second.generation_id)
        before = self.snapshot()
        for operation in (
            lambda: repo.commit("board", Tier.NO_SETUP, expected_state=first),
            lambda: repo.downgrade_to_no_setup("board", expected_state=first),
            lambda: repo.mark_setup_incomplete("board", expected_state=first),
        ):
            with self.assertRaises(CapabilityPolicyConflict):
                operation()
            self.assertEqual(self.snapshot(), before)
        repo.pointer_path("board").write_text("{damaged pointer", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(CapabilityPolicyConflict):
            repo.commit("board", Tier.NO_SETUP, expected_state=second)
        self.assertEqual(self.snapshot(), before, "conditional refusal rewrote damaged authority")
        self.assert_native_reacquisition(repo.pointer_path("board"))

    def test_pack_live_work_is_unlocked_and_tamper_and_rollback_preserve_other_attempt(self):
        from pyocd_debug_mcp.setup_flow.packs import (
            PackCandidate, PackCandidateError, PackCandidatePipeline,
        )
        from pyocd_debug_mcp.pack_provision import load_manifest
        firm = FirmStore(self.root)
        source = self.root / "source.pack"
        source.write_bytes(b"independent exact pack fixture")
        candidate = PackCandidate("independent", "1", "independent.pack",
                                  "https://example.invalid/independent.pack", source)

        def live(*args):
            # A native external holder can acquire the manifest while live work runs.
            self.assert_native_reacquisition(firm.layout.pack_manifest)

        def pipeline():
            return PackCandidatePipeline(firm, enumerate_targets=lambda *_: ("fixturetarget",),
                                         live_connect=live)

        first, pending = pipeline(), pipeline()
        one = first.validate(candidate, required_target="fixturetarget")
        two = pending.validate(candidate, required_target="fixturetarget")
        self.assertNotEqual(one.staged_path, two.staged_path)
        one.staged_path.write_bytes(b"changed after live validation")
        with self.assertRaises(PackCandidateError) as refused:
            first.promote(one, board_id="first")
        self.assertEqual(refused.exception.code, "package/staged-bytes-changed")
        self.assertFalse(one.staged_path.exists())
        self.assertEqual(two.staged_path.read_bytes(), source.read_bytes())
        destination = firm.layout.pack_files / candidate.filename
        self.assertFalse(destination.exists())
        with patch.object(firm, "atomic_write_pack_manifest", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                pending.promote(two, board_id="second")
        self.assertFalse(destination.exists(), "failed publisher left owned unreferenced final bytes")
        self.assertFalse(two.staged_path.exists())
        good = pipeline()
        three = good.validate(candidate, required_target="fixturetarget")
        good.promote(three, board_id="third")
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(load_manifest(firm.layout.pack_manifest)[0].needed_by_boards, ("third",))
        before = firm.layout.pack_manifest.read_bytes()
        last = pipeline()
        four = last.validate(candidate, required_target="fixturetarget")
        with patch.object(firm, "atomic_write_pack_manifest", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                last.promote(four, board_id="fourth")
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(firm.layout.pack_manifest.read_bytes(), before)

    def test_setup_live_connect_is_unlocked_then_stale_profile_commit_is_refused(self):
        from tests.tiered_acceptance_support import tiered_test_server
        with tiered_test_server(self.root) as server:
            board = "independent_setup"
            context = SimpleNamespace(user_input=SimpleNamespace(board_id=board),
                                      continuation_id="independent-continuation")
            expected = server._setup_policy_base(context)
            fields = {"board_id": board, "display_name": "Independent fixture",
                      "mcu_part_number": "STM32L476RGT6", "pyocd_target": "stm32l476rg",
                      "mcu_family": "stm32", "probe_family": "stlink"}
            newer = []

            def live(*args):
                self.assert_native_reacquisition(server._capability_policies.pointer_path(board))
                newer.append(server._capability_policies.mark_setup_incomplete(board))

            with self.assertRaises(CapabilityPolicyConflict):
                server._commit_setup_core(context, fields, live)
            self.assertNotEqual(expected.generation_id, newer[0].generation_id)
            self.assertEqual(server._capability_policies.resolve(board), newer[0])
            self.assertFalse(server._firm_store.layout.board_profile(board).exists())


if __name__ == "__main__":
    unittest.main()
