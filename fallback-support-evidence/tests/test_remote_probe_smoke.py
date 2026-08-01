"""Reviewer-authored smoke and practical tests for the remote-probe feature.

`tests/test_remote_probes.py` (implementer-owned, not edited here) covers the module
and isolated `HardwareInventoryService` instances thoroughly. What it does not cover:

1. **The real production wiring.** Every test in that file constructs its own fresh
   `HardwareInventoryService` / `RemoteProbeToolServices` and never touches the
   module-level singletons `server.mcp`, `server.tool_registry`,
   `server._hardware_inventory`, or `server._remote_probes_registry_path` that a real
   MCP client actually calls through. This repo has previously shipped a feature that
   was fully built, unit-tested, and green while never wired to production (D15 in
   `reviews/ledger.md`) -- these tests import the real `server` module and exercise the
   real objects it constructs at import time, the same way `tests/test_setup_overview_
   no_probe.py` does, so a wiring gap of that shape would fail here even if every
   isolated unit test passed.
2. **Concurrent registration.** Nothing in the implementer's suite calls
   `register_remote_probe` from two threads. `kernel/operations.py`'s `dispatch()` only
   serializes sync tool calls that carry a `board_id` (`worker_lock(None)` is a
   `nullcontext()`); `register_remote_probe` and `unregister_remote_probe` take no
   `board_id`, so two calls to either tool run concurrently on independent threads in
   production. This was R1 in round 1 of this review and has since been fixed with
   `remote_probes._registry_lock`, which wraps the whole load -> modify -> save cycle
   in `register_entry`/`unregister_entry`; `check_endpoint` deliberately runs *before*
   the lock is taken, so it can never serialize two unrelated registrations behind
   each other's multi-second TCP timeout. `ConcurrentRegistrationTests` now proves the
   lock holds with a `threading.Barrier`-synchronised hammer against the real
   production path rather than an interleave test -- see that class's docstring for
   why an interleave test cannot be built against the fixed code (round 1's version of
   this test blocked on a hook that is no longer inside the critical section, which
   made it pass regardless of whether the lock was present; see the note appended to
   `reviews/remote-probe-review.md`).

Every test here was written by breaking the guarded behavior, watching it fail, and
reverting the break exactly -- see the review writeup in
`reviews/remote-probe-review.md` for what was broken and how.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyocd_debug_mcp import server
from pyocd_debug_mcp.remote_probes import load_remote_probes
from pyocd_debug_mcp.tools.remote_probes import RemoteProbeToolServices, build_remote_probe_handlers


# ------------------------------------------------------------------------------------
# The real production wiring -- server.py's module-level objects, not a fresh instance
# ------------------------------------------------------------------------------------


class ProductionWiringTests(unittest.TestCase):
    """Exercise the objects a real MCP client actually calls through.

    `tests/test_remote_probes.py` never imports `server` and never touches
    `server._hardware_inventory`, `server.tool_registry`, or
    `server._remote_probes_registry_path`. A bug where the two new tools were built but
    never registered, or where the production `HardwareInventoryService` singleton was
    left wired to the default no-op `remote_probes=lambda: ()` instead of the real
    loader, would be invisible to every test in that file while being exactly the D15
    failure mode this codebase has already shipped once.
    """

    def test_both_tools_are_registered_visible_and_unlocked_in_the_real_registry(self) -> None:
        for name in ("register_remote_probe", "unregister_remote_probe"):
            with self.subTest(tool=name):
                self.assertTrue(
                    server.tool_registry.is_registered(name),
                    f"{name} is not registered in the real server.tool_registry -- "
                    "it was built but never wired in, same shape as D15.",
                )
                definition = server.tool_registry.definition(name)
                self.assertFalse(
                    definition.hidden_by_default,
                    f"{name} must be visible without unlocking anything, per the plan.",
                )
                self.assertFalse(
                    definition.locked_by_default,
                    f"{name} must not require a prerequisite plan call, per the plan.",
                )

    def test_the_real_hardware_inventory_singleton_reads_through_the_real_path_resolver(
        self,
    ) -> None:
        """`server._hardware_inventory.remote_probes` must be the live production
        closure (`lambda: load_remote_probes(_remote_probes_registry_path())`), not a
        stub or a path captured once at import time.

        Patches `server._firm_store.layout` (what `_remote_probes_registry_path()`
        reads on every call) rather than touching the real `.firm/remote_probes.json`
        under the repo root that `Path.cwd()` would otherwise resolve to.
        """

        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "remote_probes.json"
            handlers = build_remote_probe_handlers(
                RemoteProbeToolServices(
                    registry_path=lambda: registry_path, check_endpoint=lambda h, p: True
                )
            )
            handlers["register_remote_probe"]("wired.example", 6001, "wiring probe")

            with patch.object(
                server._firm_store, "layout", SimpleNamespace(remote_probes=registry_path)
            ):
                rows = server._hardware_inventory.remote_probes()
                self.assertEqual(len(rows), 1, "the real singleton did not read the registry")
                self.assertEqual(rows[0].selector, "remote:wired.example:6001")

                snapshot = server._hardware_inventory.snapshot()
                remote_rows = [row for row in snapshot.probes if row.provider == "remote"]
                self.assertEqual(
                    len(remote_rows),
                    1,
                    "a real snapshot() through the production singleton did not surface "
                    "the registered remote row",
                )
                self.assertEqual(remote_rows[0].unique_id, "remote:wired.example:6001")

    def test_the_real_registered_tool_handler_writes_through_the_real_path_resolver(self) -> None:
        """Calls `server.remote_probe_tool_handlers["register_remote_probe"]` -- the
        exact callable object registered into `server.mcp` -- and confirms it writes to
        wherever `_firm_store.layout.remote_probes` resolves at call time.

        Note on mechanism, recorded because it is easy to get wrong when writing this
        kind of test: `RemoteProbeToolServices(registry_path=_remote_probes_registry_path)`
        in server.py binds the *function object* into the dataclass field once, at
        server-import time, not a late-bound lookup of the module-level name. Patching
        `server._remote_probes_registry_path` itself after import does **not** redirect
        this handler (verified -- that patch left the temp file empty). What must be
        patched is what `_remote_probes_registry_path()` reads on every call:
        `_firm_store.layout`. This is not a defect -- `_firm_store` is never
        reassigned in production after import, so both binding styles resolve
        identically for every real caller -- but it is worth documenting for the next
        person who tries to redirect this in a test.
        """

        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "remote_probes.json"
            with patch.object(
                server._firm_store, "layout", SimpleNamespace(remote_probes=registry_path)
            ):
                result = server.remote_probe_tool_handlers["register_remote_probe"](
                    "wired-write.example", 6002, "write-through check"
                )
                self.assertIn("remote_probe_registered", result)

            on_disk = load_remote_probes(registry_path)
            self.assertEqual(len(on_disk), 1, "the real tool handler did not write to disk")
            self.assertEqual(on_disk[0].host, "wired-write.example")

    def test_no_registration_invariant_holds_through_the_real_singleton_too(self) -> None:
        """The implementer's no-registration-invariant test uses a freshly constructed
        `HardwareInventoryService`. This repeats it against the actual module-level
        `server._hardware_inventory`, pointed at a registry path that has never been
        written to, to prove the production object -- not a stand-in -- is unaffected.
        """

        with tempfile.TemporaryDirectory() as tmp:
            never_written = Path(tmp) / "remote_probes.json"
            with patch.object(
                server._firm_store, "layout", SimpleNamespace(remote_probes=never_written)
            ):
                snapshot = server._hardware_inventory.snapshot()  # must not raise
                self.assertNotIn("remote", {row.provider for row in snapshot.probes})


# ------------------------------------------------------------------------------------
# Concurrent registration -- barrier-synchronised invariant, not an interleave test
# ------------------------------------------------------------------------------------


class ConcurrentRegistrationTests(unittest.TestCase):
    """Fire many independent register/unregister calls at the real production path at
    once and assert every one of them survives.

    This is deliberately NOT an interleave test (round 1's version was, and it was
    proven vacuous -- see `reviews/remote-probe-review.md`). An interleave test needs a
    hook to block one thread strictly *between* its own registry load and its own save,
    so a second thread can run its full cycle in the gap. After the R1 fix that gap no
    longer exists reachably from outside the module: `check_endpoint` -- the only hook
    `RemoteProbeToolServices` exposes to a test double -- now runs entirely *before*
    `register_entry` takes `_registry_lock`, by design (holding the lock across a
    multi-second TCP connect would serialize unrelated registrations behind each
    other's network timeout). The only point strictly inside the critical section is
    inside `_registry_lock` itself, and blocking there from a test would require
    acquiring the lock first -- which just deadlocks against the correct code. That
    deadlock is real proof the lock is doing its job, but it is not a usable test.

    The honest substitute is the invariant the lock actually guarantees: throw many
    concurrent callers at it and confirm none of their writes are lost. This is not
    flaky in the failing direction -- with the lock held, every run below is 24/24
    survivors; the coordinator's own hammer at the same N lost 23/24 on every run
    without it (100% failure rate), so there is no meaningful chance of this test
    passing by accident when the lock is absent.
    """

    THREAD_COUNT = 24

    def _handlers(self, registry_path: Path):
        return build_remote_probe_handlers(
            RemoteProbeToolServices(
                registry_path=lambda: registry_path, check_endpoint=lambda h, p: True
            )
        )

    def test_many_concurrent_registrations_all_survive(self) -> None:
        """`THREAD_COUNT` threads each register a distinct endpoint, released together
        by a `threading.Barrier` so they hit `register_entry` as close to simultaneously
        as the platform allows. Every registration must still be on disk afterward.

        Not a contrived input: an agent registering several probes it just learned
        about in the same turn is exactly the shape of call a tool-calling agent makes,
        and nothing in `kernel/operations.py`'s dispatch serializes two sync tool calls
        that both lack a `board_id` (`worker_lock(None)` is a no-op `nullcontext()`),
        so this many real, concurrent `register_remote_probe` calls is a realistic
        production scenario, not a stress-test fiction.
        """

        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "remote_probes.json"
            handlers = self._handlers(registry_path)
            barrier = threading.Barrier(self.THREAD_COUNT)
            errors: list[BaseException] = []

            def register(index: int) -> None:
                try:
                    barrier.wait(timeout=10.0)
                    result = handlers["register_remote_probe"](
                        f"host-{index}", 6000 + index, f"probe {index}"
                    )
                    if "remote_probe_registered" not in result:
                        raise AssertionError(f"unexpected response for host-{index}: {result}")
                except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`, not lost
                    errors.append(exc)

            threads = [
                threading.Thread(target=register, args=(index,))
                for index in range(self.THREAD_COUNT)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15.0)

            self.assertEqual(errors, [], f"registration threads raised: {errors}")
            hosts = {entry.host for entry in load_remote_probes(registry_path)}
            expected = {f"host-{index}" for index in range(self.THREAD_COUNT)}
            missing = expected - hosts
            self.assertEqual(
                missing,
                set(),
                f"{len(missing)} of {self.THREAD_COUNT} concurrent registrations were "
                "lost despite each one's own tool call reporting "
                f"'remote_probe_registered': {sorted(missing)}",
            )

    def test_concurrent_register_and_unregister_do_not_corrupt_the_registry(self) -> None:
        """Pre-seed 12 entries sequentially, then concurrently unregister the even-
        numbered half while registering 12 brand-new endpoints, all released together
        by one barrier -- register and unregister sharing `_registry_lock` correctly is
        exactly what's under test.

        Every operation targets a distinct `(host, port)` key (no two threads ever race
        for the *same* row), so the only way the final state can be wrong is a lost
        write anywhere in the shared file -- not two threads disagreeing about one key.
        """

        preseed_count = 12
        new_count = 12
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "remote_probes.json"
            handlers = self._handlers(registry_path)

            # Sequential setup -- not part of what's under test.
            for index in range(preseed_count):
                handlers["register_remote_probe"](f"seed-{index}", 7000 + index, f"seed {index}")

            to_unregister = [(f"seed-{index}", 7000 + index) for index in range(0, preseed_count, 2)]
            to_keep = {f"seed-{index}" for index in range(1, preseed_count, 2)}

            barrier = threading.Barrier(len(to_unregister) + new_count)
            errors: list[BaseException] = []

            def do_unregister(host: str, port: int) -> None:
                try:
                    barrier.wait(timeout=10.0)
                    handlers["unregister_remote_probe"](host, port)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def do_register(index: int) -> None:
                try:
                    barrier.wait(timeout=10.0)
                    handlers["register_remote_probe"](
                        f"new-{index}", 8000 + index, f"new {index}"
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=do_unregister, args=(host, port))
                for host, port in to_unregister
            ]
            threads += [
                threading.Thread(target=do_register, args=(index,)) for index in range(new_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15.0)

            self.assertEqual(errors, [], f"threads raised: {errors}")
            hosts = {entry.host for entry in load_remote_probes(registry_path)}
            expected = to_keep | {f"new-{index}" for index in range(new_count)}
            self.assertEqual(
                hosts,
                expected,
                "concurrent register/unregister lost or duplicated an entry -- missing: "
                f"{sorted(expected - hosts)}, unexpected: {sorted(hosts - expected)}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
