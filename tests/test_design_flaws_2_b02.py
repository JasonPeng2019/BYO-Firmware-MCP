"""Real-process regression coverage for L4's complete state transactions."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from pyocd_debug_mcp import remote_probes
from pyocd_debug_mcp.capabilities import policy
from pyocd_debug_mcp.capabilities.policy import CapabilityPolicyRepository, Tier
from pyocd_debug_mcp.firmstore import cache
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
            stack.enter_context(
                patch.object(
                    remote_probes, "load_remote_probes", lambda path: after_read(original(path))
                )
            )
            if kind == "registry-remove" and label == "second":
                remote_probes.unregister_entry(firm.layout.remote_probes, "seed", 5555)
            else:
                remote_probes.register_entry(firm.layout.remote_probes, label, 5555, label)
        elif kind.startswith("cache"):
            original = attachments.load_records
            stack.enter_context(
                patch.object(attachments, "load_records", lambda: after_read(original()))
            )
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
            stack.enter_context(
                patch.object(repo, "resolve", lambda board: after_read(original(board)))
            )
            result = repo.downgrade_to_no_setup("controller")
            if results is not None:
                results.put((label, result.generation_id))
        else:
            raise AssertionError(kind)


def _policy_crash(root, ready, release):
    def fault(stage):
        if stage == "before_pointer_replace":
            ready.set()
            _wait(release)
            os._exit(79)

    CapabilityPolicyRepository(Path(root), fault_hook=fault).downgrade_to_no_setup("controller")


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


def _pack_candidate(source):
    from pyocd_debug_mcp.setup_flow.packs import PackCandidate

    return PackCandidate(
        "shared", "1", "shared.pack", "https://example.invalid/shared.pack", source
    )


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
            _mutation,
            kind,
            str(self.root),
            "first",
            paused=paused,
            release=release,
            results=results,
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
            {
                row.host
                for row in remote_probes.load_remote_probes(
                    FirmStore(self.root).layout.remote_probes
                )
            },
            {"first", "second"},
        )


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
        self.assertIn(
            initial.generation_id, repo.downgrade_to_no_setup("controller").retained_generations
        )


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
        with patch.object(
            firm, "atomic_write_pack_manifest", side_effect=OSError("manifest write failed")
        ):
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
        with patch.object(
            firm, "atomic_write_pack_manifest", side_effect=OSError("manifest write failed")
        ):
            with self.assertRaises(OSError):
                third.promote(three, board_id="third")
        self.assertEqual(final.read_bytes(), source.read_bytes())
        self.assertEqual(firm.layout.pack_manifest.read_bytes(), before)


    def test_public_downgrade_consumes_digest_and_publishes_in_one_transaction(self):
        from tests.tiered_acceptance_support import tiered_test_server

        initial, _profile, _memory_map = _seed_full(self.root)
        with tiered_test_server(self.root) as server:
            permissions = server._manual_permissions
            permissions.write_unlocked_grant(
                action="downgrade",
                grant_id="grant",
                board_id="legacy_full",
                policy_digest=initial.policy_digest,
            )
            token = permissions.issue_downgrade_token("legacy_full", "grant", initial.policy_digest)
            consume = permissions.consume_downgrade
            output = self.root / "contender-result"

            def consumed(*args):
                consume(*args)
                child = self._start(
                    _other_policy_change, str(self.root), "legacy_full", str(output)
                )
                child.join(15)
                self.assertEqual(child.exitcode, 0)

            with patch.object(permissions, "consume_downgrade", side_effect=consumed):
                result = json.loads(server.downgrade("legacy_full", token))
            self.assertEqual(result["status"], "downgraded")
            self.assertEqual(output.read_text(), "busy")
            self.assertEqual(permissions.status("downgrade").state, "consumed")


if __name__ == "__main__":
    unittest.main()
