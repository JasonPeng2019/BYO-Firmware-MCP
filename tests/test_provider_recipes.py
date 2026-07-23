"""Data-driven provider recipe regressions for the Slice 3A generic route."""

from __future__ import annotations

from pathlib import Path
import multiprocessing
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from collections.abc import Callable, Mapping
from typing import Protocol, cast
import unittest
from unittest.mock import Mock, patch

from firmware_mcp import server
from firmware_mcp.adapters.debug_interface import TargetSessionHandle, TargetSessionMetadata
from firmware_mcp.adapters.debug_process import ProcessIsolatedDebugInterface
from firmware_mcp.board_config import BoardConfig, ConfigError, make_board_config
from firmware_mcp.firmstore.providers import (
    ProviderRecipe,
    ProviderConnection,
    ProviderRecipeError,
    ProviderRecipeStore,
    run_inventory,
)
from firmware_mcp.firmstore.provider_lock import provider_recipe_publication_lock
from firmware_mcp.firmstore.profiles import ProfileError, ProfileRepository
from firmware_mcp.firmstore.store import FirmStore, FirmStoreError
from firmware_mcp.setup_flow.preflight import PreflightDecision, ProbeCandidate, SetupUserInput
from firmware_mcp.services.connections import ConnectionManager
from firmware_mcp.setup_flow.setup import SetupPhaseContext
from firmware_mcp.setup_flow.setup import SetupPhaseOutcome
from firmware_mcp.target_errors import TargetConnectionError


_WORKER = r"""import json
import os
import sys

mode = sys.argv[1]
token = sys.argv[2]
support = os.environ["TEST_PROVIDER_SUPPORT"]

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

send({"version": 4, "ready": True})
for line in sys.stdin:
    request = json.loads(line)
    request_id = request["request_id"]
    operation = request["operation"]
    if mode == "stale":
        send({"version": 4, "request_id": request_id + 1, "ok": True, "result": None})
        continue
    if operation in {"open", "connect_under_reset"}:
        identity = {
            "capability": "exact",
            "part_number": "Example-9000",
            "provenance": "provider-observed exact chip id",
            "support_identity": support,
            "evidence": {"chip_id": "9000"},
        }
        if mode == "malformed_identity":
            identity.pop("provenance")
        result = {
            "board_name": "Example board",
            "probe_description": "Example probe",
            "probe_family": "novel-probe",
            "probe_uid": token,
            "live_part_number": "Example-9000",
            "route_used": "generic-worker",
            "target_override": "example-target",
            "runtime_token": token,
            "live_identity": identity,
        }
    elif operation == "get_state":
        result = "HALTED"
    elif operation == "read_memory":
        result = 0x12
    elif operation == "physical_memory_regions":
        if mode == "malformed_regions":
            result = [{
                "start": 0x20000000, "end": 0x20000000,
                "readable": True, "writable": True, "executable": False,
                "kind": "ram", "name": "generic ram",
                "provenance": "provider live map", "session_token": token,
            }]
        else:
            result = [{
                "start": 0x20000000, "end": 0x20000100,
                "readable": True, "writable": True, "executable": False,
                "kind": "ram", "name": "generic ram",
                "provenance": "provider live map", "session_token": token,
            }]
    elif operation == "flash":
        digest = "0" * 64 if mode != "false_flash" else "1" * 64
        result = {
            "firmware_path": request["arguments"]["path"],
            "byte_count": 1,
            "verified_ranges": [[0x08000000, 0x08000001]],
            "expected_sha256": "0" * 64,
            "observed_sha256": digest,
            "final_reset_postcondition": "RUNNING",
            "session_token": token,
            "support_identity": support,
        }
    else:
        result = None
    send({"version": 4, "request_id": request_id, "ok": True, "result": result})
"""


_INVENTORY = r"""import json
import sys

mode = sys.argv[1]
if mode == "nonzero":
    print("inventory failed", file=sys.stderr)
    raise SystemExit(7)
if mode == "invalid":
    print("not json")
elif mode == "duplicate":
    print(json.dumps({"connections": [
        {"connection_id": "one", "description": "first", "probe_uid": None, "probe_family": "novel"},
        {"connection_id": "one", "description": "second", "probe_uid": None, "probe_family": "novel"}
    ]}))
else:
    print(json.dumps({"connections": [{
        "connection_id": "one", "description": "Unknown Probe", "probe_uid": "UID-1", "probe_family": "novel"
    }]}))
"""


class _Signal(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def _hold_project_recipe_lock(root: str, entered: _Signal, release: _Signal) -> None:
    """Spawn-safe process helper for durable provider-publication serialization."""

    with provider_recipe_publication_lock(Path(root)):
        entered.set()
        release.wait()


def _save_recipe_after_lock(
    root: str,
    record: dict[str, object],
    entered: _Signal,
    completed: _Signal,
) -> None:
    """Attempt a separate-process publication after announcing the interleaving."""

    entered.set()
    ProviderRecipeStore(FirmStore(Path(root))).save(ProviderRecipe.from_record(record))
    completed.set()


class ProviderRecipeTests(unittest.TestCase):
    def _files(self, root: Path) -> tuple[Path, Path]:
        inventory = root / "inventory.py"
        worker = root / "worker.py"
        inventory.write_text(_INVENTORY, encoding="utf-8")
        worker.write_text(_WORKER, encoding="utf-8")
        os.environ.setdefault("TEST_PROVIDER_SUPPORT", "test-worker-support")
        return inventory, worker

    def _recipe(
        self, inventory: Path, worker: Path, *, inventory_mode: str = "ok"
    ) -> ProviderRecipe:
        recipe = ProviderRecipe.from_record(
            {
                "provider_id": "example-provider",
                "inventory_argv": [sys.executable, str(inventory), inventory_mode],
                "worker_argv": [sys.executable, str(worker), "ok", "worker-a"],
            }
        )
        os.environ["TEST_PROVIDER_SUPPORT"] = recipe.support_identity("example-target")
        return recipe

    def test_project_wide_recipe_publication_lock_serializes_spawned_writer(self) -> None:
        """A board-A rollback snapshot cannot race a board-B recipe publication."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = multiprocessing.get_context("spawn")
            held, release = context.Event(), context.Event()
            writer_entered, writer_completed = context.Event(), context.Event()
            record = {
                "provider_id": "cross-process-provider",
                "inventory_argv": [sys.executable, "-c", "pass"],
                "worker_argv": [sys.executable, "-c", "pass"],
            }
            holder = context.Process(
                target=_hold_project_recipe_lock,
                args=(str(root), held, release),
            )
            writer = context.Process(
                target=_save_recipe_after_lock,
                args=(str(root), record, writer_entered, writer_completed),
            )
            try:
                holder.start()
                self.assertTrue(held.wait(10))
                writer.start()
                self.assertTrue(writer_entered.wait(10))
                self.assertFalse(writer_completed.wait(0.2))
                release.set()
                holder.join(10)
                writer.join(10)
                self.assertEqual(holder.exitcode, 0)
                self.assertEqual(writer.exitcode, 0)
            finally:
                release.set()
                for process in (holder, writer):
                    if process.is_alive():
                        process.terminate()
                    process.join(10)
            recipes = ProviderRecipeStore(FirmStore(root)).load_all()
            self.assertEqual(set(recipes), {"cross-process-provider"})

    @staticmethod
    def _setup_context(recipe: ProviderRecipe) -> SetupPhaseContext:
        user_input = SetupUserInput(
            board_id="example_board",
            connection_id="provider:example-provider:one",
            display_name="Example board",
            mcu_part_number="Example-9000",
            requires_uart=False,
            serial_baudrate=None,
            provider_recipe=recipe.to_record(),
        )
        return SetupPhaseContext(
            continuation_id="continuation",
            attempt_id="attempt",
            mode="setup",
            user_input=user_input,
            preflight=PreflightDecision(
                "preflight_ready",
                "setup/complete",
                "",
                selected_probe=ProbeCandidate(
                    user_input.connection_id, "Unknown Probe", "novel", "UID-1"
                ),
                selected_target="example-target",
            ),
            phase_records={},
        )

    @staticmethod
    def _generic_handle(
        board: BoardConfig,
        recipe: ProviderRecipe,
        *,
        support_identity: str | None = None,
    ) -> TargetSessionHandle:
        return TargetSessionHandle(
            session=object(),
            board=board,
            probe_uid="one",
            route_used="generic-worker",
            target_override=board.target,
            metadata=TargetSessionMetadata(
                board_name=board.display_name,
                probe_description="Unknown Probe",
                probe_family="novel",
                probe_uid="one",
                live_part_number="Example-9000",
                route_used="generic-worker",
                target_override=board.target,
                runtime_token="returning-session",
                live_identity={
                    "capability": "exact",
                    "part_number": "Example-9000",
                    "provenance": "provider-observed exact chip id",
                    "support_identity": support_identity
                    or recipe.support_identity(board.target or ""),
                    "evidence": {"chip_id": "9000"},
                },
            ),
        )

    @staticmethod
    def _unconnected_manager() -> object:
        import threading

        lock = threading.RLock()
        return SimpleNamespace(
            lock_for=lambda _board_id: lock,
            maybe_connection=lambda _board_id: None,
        )

    def test_unknown_recipe_is_inventoried_persisted_and_reloaded(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = ProviderRecipeStore(FirmStore(root))

            observed = run_inventory(recipe)
            self.assertEqual(observed[0].namespaced_id, "provider:example-provider:one")
            store.save(recipe)

            reloaded = ProviderRecipeStore(FirmStore(root)).load("example-provider")
            self.assertEqual(reloaded, recipe)
            self.assertTrue((root / ".firm" / "providers.json").exists())

    def test_inventory_uses_closed_stdin_and_preserves_exact_json_validation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            completed = subprocess.CompletedProcess(
                recipe.inventory_argv,
                0,
                stdout=(
                    '{"connections":[{"connection_id":"one","description":"Unknown Probe",'
                    '"probe_uid":"UID-1","probe_family":"novel"}]}'
                ),
                stderr="",
            )
            with patch(
                "firmware_mcp.firmstore.providers.subprocess.run", return_value=completed
            ) as run:
                observed = run_inventory(recipe)

        self.assertEqual(observed[0].namespaced_id, "provider:example-provider:one")
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_overview_exposes_a_recipe_connection_without_a_provider_branch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = ProviderRecipeStore(FirmStore(root))
            with (
                patch.object(server, "_provider_recipe_store", store),
                patch.object(
                    server, "_validation_inventory", return_value=server.ValidationInventory()
                ),
                patch.object(server._profile_repository, "load_all", return_value=[]),
            ):
                overview = server._setup_overview(None, provider_recipe=recipe.to_record())
            self.assertIn(
                {
                    "connection_id": "provider:example-provider:one",
                    "provider_id": "example-provider",
                    "provider_connection_id": "one",
                    "description": "Unknown Probe",
                    "probe_uid": "UID-1",
                    "probe_family": "novel",
                },
                cast(list[dict[str, object]], overview["connections"]),
            )
            self.assertEqual(store.load("example-provider"), recipe)

    def test_provider_route_case_is_distinct_through_overview_assignment(self) -> None:
        """Generic routes are opaque; only built-in probe IDs normalize case."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            routes = (
                ProviderConnection("Provider", "one", "Upper", None, "novel"),
                ProviderConnection("provider", "one", "Lower", None, "novel"),
            )
            replace_assignments = Mock()
            with (
                patch.object(
                    server, "_provider_recipe_store", ProviderRecipeStore(FirmStore(root))
                ),
                patch.object(server, "run_provider_inventory", return_value=routes),
                patch.object(
                    server, "_validation_inventory", return_value=server.ValidationInventory()
                ),
                patch.object(server._profile_repository, "load_all", return_value=[]),
                patch.object(server, "_replace_setup_assignments", replace_assignments),
            ):
                overview = server._setup_overview(
                    ["Upper board", "Lower board"],
                    {
                        "Upper board": "provider:Provider:one",
                        "Lower board": "provider:provider:one",
                    },
                    recipe.to_record(),
                )

            self.assertFalse(
                server._same_setup_connection("provider:Provider:one", "provider:provider:one")
            )
            self.assertNotEqual(
                server._setup_connection_key("provider:Provider:one"),
                server._setup_connection_key("provider:provider:one"),
            )
            self.assertEqual(
                {
                    row["connection_id"]
                    for row in cast(list[dict[str, object]], overview["connections"])
                },
                {"provider:Provider:one", "provider:provider:one"},
            )
            bindings = replace_assignments.call_args.args[0]
            self.assertEqual(bindings["provider:Provider:one"], "upper_board")
            self.assertEqual(bindings["provider:provider:one"], "lower_board")

    def test_recipe_setup_commits_only_current_exact_live_provider_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            context = self._setup_context(recipe)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", ProviderRecipeStore(store)),
            ):
                outcome = server._setup_provider_recipe_connection_phase(context)

            self.assertTrue(outcome.verified)
            self.assertEqual(outcome.details["provider_id"], "example-provider")
            profile = ProfileRepository(FirmStore(root)).load("example_board")
            self.assertEqual(profile.board.provider_id, "example-provider")
            self.assertEqual(profile.board.target, "example-target")
            self.assertEqual(
                profile.to_document()["provider_live_identity"]["support_identity"],
                recipe.support_identity("example-target"),
            )
            self.assertEqual(ProviderRecipeStore(FirmStore(root)).load("example-provider"), recipe)

    def test_returning_recipe_board_reconnects_and_under_reset_use_fresh_inventory_route(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
            ):
                self.assertTrue(
                    server._setup_provider_recipe_connection_phase(
                        self._setup_context(recipe)
                    ).verified
                )
            profile = repository.load("example_board")
            handle = self._generic_handle(profile.board, recipe)
            assignment_store = SimpleNamespace(
                connection_for=Mock(return_value="provider:example-provider:one")
            )
            promotion = Mock(
                return_value=SimpleNamespace(
                    runtime_session=SimpleNamespace(session_id="returning-session")
                )
            )
            for route, function, opener in (
                ("normal", server._connect_impl, "open_session"),
                ("under-reset", server._connect_with_wired_reset_impl, "connect_under_reset"),
            ):
                with self.subTest(route=route):
                    open_worker = Mock(return_value=handle)
                    with (
                        patch.object(server, "_profile_repository", repository),
                        patch.object(server, "_provider_recipe_store", recipes),
                        patch.object(server, "assignment_store", assignment_store),
                        patch.object(server, "connection_manager", self._unconnected_manager()),
                        patch.object(server, "resolve_board_config", return_value=profile.board),
                        patch.object(server, "_verified_pack_for_profile", return_value=None),
                        patch.object(server, "_promote_open_session", promotion),
                        patch.object(server.target_control, opener, open_worker),
                        patch.object(server, "_record_event", Mock()),
                    ):
                        if route == "normal":
                            cast(Callable[..., object], function)("example_board")
                        else:
                            function("example_board", None, None, None)
                    self.assertEqual(open_worker.call_args.kwargs["unique_id"], "one")
                    self.assertEqual(
                        open_worker.call_args.kwargs["worker_argv"], recipe.worker_argv
                    )

    def test_replacing_recipe_bytes_denies_returning_reconnect_and_flash_authority(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
            ):
                self.assertTrue(
                    server._setup_provider_recipe_connection_phase(
                        self._setup_context(recipe)
                    ).verified
                )
            profile = repository.load("example_board")
            replacement = ProviderRecipe.from_record(
                {
                    **recipe.to_record(),
                    "worker_argv": [sys.executable, str(worker), "ok", "replaced-worker"],
                }
            )
            recipes.save(replacement)
            handle = self._generic_handle(profile.board, recipe)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
                patch.object(
                    server,
                    "assignment_store",
                    SimpleNamespace(
                        connection_for=lambda _board_id: "provider:example-provider:one"
                    ),
                ),
                patch.object(server, "connection_manager", self._unconnected_manager()),
                patch.object(server, "resolve_board_config", return_value=profile.board),
                patch.object(server, "_verified_pack_for_profile", return_value=None),
                patch.object(server.target_control, "open_session") as open_worker,
                patch.object(server, "_record_event", Mock()),
            ):
                with self.assertRaisesRegex(TargetConnectionError, "rerun setup_board"):
                    server._connect_impl("example_board")
                with self.assertRaisesRegex(TargetConnectionError, "changed after setup"):
                    server._require_current_provider_identity(profile, handle)
            open_worker.assert_not_called()

    def test_each_connect_route_closes_identity_mismatch_workers_and_keeps_close_failure_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
            ):
                self.assertTrue(
                    server._setup_provider_recipe_connection_phase(
                        self._setup_context(recipe)
                    ).verified
                )
            profile = repository.load("example_board")
            assignment_store = SimpleNamespace(
                connection_for=lambda _board_id: "provider:example-provider:one"
            )
            for route, function, opener in (
                ("normal", server._connect_impl, "open_session"),
                ("under-reset", server._connect_with_wired_reset_impl, "connect_under_reset"),
            ):
                with self.subTest(route=route):
                    handle = self._generic_handle(
                        profile.board,
                        recipe,
                        support_identity="wrong-live-support",
                    )
                    close = Mock(side_effect=RuntimeError("worker close failed distinctly"))
                    with (
                        patch.object(server, "_profile_repository", repository),
                        patch.object(server, "_provider_recipe_store", recipes),
                        patch.object(server, "assignment_store", assignment_store),
                        patch.object(server, "connection_manager", self._unconnected_manager()),
                        patch.object(server, "resolve_board_config", return_value=profile.board),
                        patch.object(server, "_verified_pack_for_profile", return_value=None),
                        patch.object(server.target_control, opener, return_value=handle),
                        patch.object(server.target_control, "close_session", close),
                        patch.object(server, "_record_event", Mock()),
                    ):
                        with self.assertRaisesRegex(
                            TargetConnectionError, "support identity"
                        ) as caught:
                            if route == "normal":
                                cast(Callable[..., object], function)("example_board")
                            else:
                                function("example_board", None, None, None)
                    close.assert_called_once_with(handle)
                    self.assertIsNotNone(caught.exception.__cause__)
                    self.assertIn("worker close failed distinctly", str(caught.exception.__cause__))

    def test_each_connect_route_delegates_promotion_cleanup_once_and_preserves_secondary_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
            ):
                self.assertTrue(
                    server._setup_provider_recipe_connection_phase(
                        self._setup_context(recipe)
                    ).verified
                )
            profile = repository.load("example_board")
            assignment_store = SimpleNamespace(
                connection_for=lambda _board_id: "provider:example-provider:one"
            )
            for route, function, opener in (
                ("normal", server._connect_impl, "open_session"),
                ("under-reset", server._connect_with_wired_reset_impl, "connect_under_reset"),
            ):
                with self.subTest(route=route):
                    handle = self._generic_handle(profile.board, recipe)
                    close = Mock(side_effect=RuntimeError("promotion worker close failed"))
                    session_store = SimpleNamespace(
                        start_session=Mock(side_effect=RuntimeError("runtime publish failed")),
                        close_session=Mock(),
                    )
                    with (
                        patch.object(server, "_profile_repository", repository),
                        patch.object(server, "_provider_recipe_store", recipes),
                        patch.object(server, "assignment_store", assignment_store),
                        patch.object(server, "connection_manager", ConnectionManager()),
                        patch.object(server, "_session_store", session_store),
                        patch.object(server, "resolve_board_config", return_value=profile.board),
                        patch.object(server, "_verified_pack_for_profile", return_value=None),
                        patch.object(server.target_control, opener, return_value=handle),
                        patch.object(server.target_control, "close_session", close),
                        patch.object(server, "_record_event", Mock()),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "runtime publish failed"
                        ) as caught:
                            if route == "normal":
                                cast(Callable[..., object], function)("example_board")
                            else:
                                function("example_board", None, None, None)
                    close.assert_called_once_with(handle)
                    self.assertIsNotNone(caught.exception.__cause__)
                    self.assertIn("promotion worker close failed", str(caught.exception.__cause__))

    def test_recipe_profile_publication_rolls_back_both_stores(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
                patch.object(
                    repository, "commit_optional", side_effect=ProfileError("optional boom")
                ),
            ):
                outcome = server._setup_provider_recipe_connection_phase(
                    self._setup_context(recipe)
                )
            self.assertFalse(outcome.verified)
            self.assertIn("rollback completed", outcome.agent_prompt)
            self.assertFalse(store.layout.board_profile("example_board").exists())
            self.assertEqual(recipes.load_all(), {})
            self.assertFalse(store.layout.providers.exists())

    def test_failed_setup_rollback_cannot_erase_concurrent_overview_recipe_publication(
        self,
    ) -> None:
        """Publication is serialized, while B's inventory still runs outside the lock."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe_a = self._recipe(inventory, worker)
            recipe_b = ProviderRecipe.from_record(
                {
                    "provider_id": "second-provider",
                    "inventory_argv": [sys.executable, str(inventory), "ok"],
                    "worker_argv": [sys.executable, str(worker), "ok", "worker-b"],
                }
            )
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            repository.commit_core(
                repository.stage_core(
                    {
                        "board_id": "unrelated_board",
                        "display_name": "Unrelated board",
                        "mcu_part_number": "Example-9000",
                        "mcu_family": "example",
                        "probe_family": "novel",
                        "provider_id": "unrelated-provider",
                        "target": "unrelated-target",
                    }
                )
            )
            unrelated_profile = store.layout.board_profile("unrelated_board").read_text(
                encoding="utf-8"
            )
            a_at_profile_commit = threading.Event()
            b_inventory_complete = threading.Event()
            release_a = threading.Event()
            outcomes: dict[str, SetupPhaseOutcome | Mapping[str, object]] = {}
            original_inventory = run_inventory

            def inventory_with_b_marker(recipe: ProviderRecipe) -> tuple[ProviderConnection, ...]:
                result = original_inventory(recipe)
                if recipe.provider_id == recipe_b.provider_id:
                    b_inventory_complete.set()
                return result

            def fail_a_optional_commit(_staged: object) -> object:
                a_at_profile_commit.set()
                if not release_a.wait(5):
                    raise RuntimeError("test did not release setup A")
                raise ProfileError("A optional commit failed")

            def run_a() -> None:
                outcomes["a"] = server._setup_provider_recipe_connection_phase(
                    self._setup_context(recipe_a)
                )

            def run_b() -> None:
                outcomes["b"] = server._setup_overview(None, provider_recipe=recipe_b.to_record())

            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
                patch.object(
                    server, "_validation_inventory", return_value=server.ValidationInventory()
                ),
                patch.object(server, "run_provider_inventory", side_effect=inventory_with_b_marker),
                patch.object(repository, "commit_optional", side_effect=fail_a_optional_commit),
            ):
                thread_a = threading.Thread(target=run_a)
                thread_a.start()
                self.assertTrue(a_at_profile_commit.wait(5))

                thread_b = threading.Thread(target=run_b)
                thread_b.start()
                self.assertTrue(b_inventory_complete.wait(5))
                self.assertTrue(thread_b.is_alive())
                release_a.set()
                thread_a.join(5)
                thread_b.join(5)

            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            outcome_a = cast(SetupPhaseOutcome, outcomes["a"])
            self.assertFalse(outcome_a.verified)
            outcome_b = cast(Mapping[str, object], outcomes["b"])
            self.assertEqual(
                cast(list[dict[str, object]], outcome_b["connections"])[0]["connection_id"],
                "provider:second-provider:one",
            )
            self.assertEqual(recipes.load_all(), {recipe_b.provider_id: recipe_b})
            self.assertFalse(store.layout.board_profile("example_board").exists())
            self.assertEqual(
                store.layout.board_profile("unrelated_board").read_text(encoding="utf-8"),
                unrelated_profile,
            )

    def test_recipe_core_write_failure_rolls_back_the_recipe_before_any_profile_exists(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
                patch.object(repository, "commit_core", side_effect=OSError("core disk full")),
            ):
                outcome = server._setup_provider_recipe_connection_phase(
                    self._setup_context(recipe)
                )
            self.assertFalse(outcome.verified)
            self.assertIn("core disk full", outcome.agent_prompt)
            self.assertIn("rollback completed", outcome.agent_prompt)
            self.assertFalse(store.layout.board_profile("example_board").exists())
            self.assertEqual(recipes.load_all(), {})
            self.assertFalse(store.layout.providers.exists())

    def test_existing_profile_and_recipe_are_restored_after_optional_write_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            repository.commit_core(
                repository.stage_core(
                    {
                        "board_id": "example_board",
                        "display_name": "Example board",
                        "mcu_part_number": "Example-9000",
                        "mcu_family": "example",
                        "probe_family": "novel",
                        "provider_id": "example-provider",
                        "target": "example-target",
                    }
                )
            )
            previous_profile = store.layout.board_profile("example_board").read_text(
                encoding="utf-8"
            )
            old_recipe = ProviderRecipe.from_record(
                {
                    "provider_id": "example-provider",
                    "inventory_argv": [sys.executable, str(inventory), "ok"],
                    "worker_argv": [sys.executable, str(worker), "ok", "old-worker"],
                }
            )
            recipes.save(old_recipe)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
                patch.object(
                    repository, "commit_optional", side_effect=ProfileError("optional disk full")
                ),
            ):
                outcome = server._setup_provider_recipe_connection_phase(
                    self._setup_context(recipe)
                )
            self.assertFalse(outcome.verified)
            self.assertIn("rollback completed", outcome.agent_prompt)
            self.assertEqual(
                store.layout.board_profile("example_board").read_text(encoding="utf-8"),
                previous_profile,
            )
            self.assertEqual(recipes.load_all(), {"example-provider": old_recipe})

    def test_recipe_write_failure_does_not_publish_a_profile(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
                patch.object(recipes, "save", side_effect=ProviderRecipeError("disk full")),
            ):
                outcome = server._setup_provider_recipe_connection_phase(
                    self._setup_context(recipe)
                )
            self.assertFalse(outcome.verified)
            self.assertIn("before profile mutation", outcome.agent_prompt)
            self.assertFalse(store.layout.board_profile("example_board").exists())
            self.assertEqual(recipes.load_all(), {})
            self.assertFalse(store.layout.providers.exists())

    def test_recipe_rollback_failure_is_returned_with_primary_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            store = FirmStore(root)
            repository = ProfileRepository(store)
            recipes = ProviderRecipeStore(store)
            with (
                patch.object(server, "_profile_repository", repository),
                patch.object(server, "_provider_recipe_store", recipes),
                patch.object(
                    repository, "commit_optional", side_effect=ProfileError("optional boom")
                ),
                patch.object(
                    recipes,
                    "restore_snapshot",
                    side_effect=ProviderRecipeError("rollback disk full"),
                ),
            ):
                outcome = server._setup_provider_recipe_connection_phase(
                    self._setup_context(recipe)
                )
            self.assertFalse(outcome.verified)
            self.assertIn("optional boom", outcome.agent_prompt)
            self.assertIn(
                "recipe rollback: ProviderRecipeError: rollback disk full",
                outcome.agent_prompt,
            )

    def test_inventory_failures_are_explicit_and_not_persisted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            store = ProviderRecipeStore(FirmStore(root))
            for mode, expected in (
                ("nonzero", "exited 7"),
                ("invalid", "invalid JSON"),
                ("duplicate", "duplicated connection_id"),
            ):
                with self.subTest(mode=mode):
                    with self.assertRaisesRegex(ProviderRecipeError, expected):
                        run_inventory(self._recipe(inventory, worker, inventory_mode=mode))
                    self.assertEqual(store.load_all(), {})

    def test_namespaced_provider_routes_reject_colon_components(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            with self.assertRaisesRegex(ProviderRecipeError, "provider_id must not contain ':'"):
                ProviderRecipe.from_record(
                    {
                        "provider_id": "a:b",
                        "inventory_argv": [sys.executable, str(inventory), "ok"],
                        "worker_argv": [sys.executable, str(worker), "ok", "one"],
                    }
                )
            recipe = self._recipe(inventory, worker)
            second_recipe = ProviderRecipe.from_record(
                {
                    **recipe.to_record(),
                    "provider_id": "another-provider",
                }
            )
            self.assertNotEqual(
                run_inventory(recipe)[0].namespaced_id,
                run_inventory(second_recipe)[0].namespaced_id,
            )
            changed = inventory.read_text(encoding="utf-8").replace('"one"', '"a:b"')
            inventory.write_text(changed, encoding="utf-8")
            with self.assertRaisesRegex(ProviderRecipeError, "connection_id must not contain ':'"):
                run_inventory(recipe)

    def test_generic_worker_controls_memory_regions_and_verified_flash(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory, worker = self._files(root)
            recipe = self._recipe(inventory, worker)
            board = BoardConfig(
                board_id="example_board",
                display_name="Example board",
                mcu_family="example",
                probe_family="novel",
                provider_id="example-provider",
                target="example-target",
            )
            firmware = root / "firmware.bin"
            firmware.write_bytes(b"x")
            interface = ProcessIsolatedDebugInterface()
            handle = interface.open(
                board=board,
                unique_id="one",
                target=board.target,
                worker_argv=recipe.worker_argv,
            )
            try:
                self.assertEqual(interface.get_state(handle), "HALTED")
                self.assertEqual(interface.read_memory(handle, 0x20000000, 8), 0x12)
                interface.write_memory(handle, 0x20000000, 0x12, 8)
                self.assertEqual(
                    interface.physical_memory_regions(handle)[0].session_token, "worker-a"
                )
                verification = interface.flash(handle, firmware, halt_after_reset=False)
                self.assertEqual(
                    verification.support_identity, recipe.support_identity("example-target")
                )
                self.assertEqual(verification.session_token, "worker-a")
            finally:
                interface.close(handle)

    def test_malformed_stale_and_false_worker_evidence_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _inventory, worker = self._files(root)
            board = BoardConfig(
                "example",
                "Example",
                "example",
                "novel",
                "example-provider",
                "example-target",
            )
            interface = ProcessIsolatedDebugInterface()
            for mode, fragment in (
                ("malformed_identity", "live identity schema"),
                ("stale", "stale or mismatched"),
            ):
                with self.subTest(mode=mode):
                    with self.assertRaisesRegex(TargetConnectionError, fragment):
                        interface.open(
                            board=board,
                            unique_id="one",
                            target=board.target,
                            worker_argv=(
                                sys.executable,
                                str(worker),
                                mode,
                                "worker-a",
                                "example-provider:example-target",
                            ),
                        )
            firmware = root / "firmware.bin"
            firmware.write_bytes(b"x")
            handle = interface.open(
                board=board,
                unique_id="one",
                target=board.target,
                worker_argv=(
                    sys.executable,
                    str(worker),
                    "false_flash",
                    "worker-a",
                    "example-provider:example-target",
                ),
            )
            try:
                with self.assertRaisesRegex(TargetConnectionError, "flash verification"):
                    interface.flash(handle, firmware, halt_after_reset=False)
            finally:
                interface.close(handle)

    def test_malformed_live_regions_are_rejected_before_they_become_evidence(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _inventory, worker = self._files(root)
            board = BoardConfig(
                "example",
                "Example",
                "example",
                "novel",
                "example-provider",
                "example-target",
            )
            interface = ProcessIsolatedDebugInterface()
            handle = interface.open(
                board=board,
                unique_id="one",
                target=board.target,
                worker_argv=(
                    sys.executable,
                    str(worker),
                    "malformed_regions",
                    "worker-a",
                    "example-provider:example-target",
                ),
            )
            try:
                with self.assertRaisesRegex(TargetConnectionError, "physical_memory_regions"):
                    interface.physical_memory_regions(handle)
            finally:
                interface.close(handle)

    def test_two_recipe_boards_keep_distinct_workers(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _inventory, worker = self._files(root)
            interface = ProcessIsolatedDebugInterface()
            first = BoardConfig("one", "One", "example", "novel", "recipe-one", "target-one")
            second = BoardConfig("two", "Two", "example", "novel", "recipe-two", "target-two")
            first_handle = interface.open(
                board=first,
                unique_id="one",
                target=first.target,
                worker_argv=(
                    sys.executable,
                    str(worker),
                    "ok",
                    "worker-one",
                    "recipe-one:target-one",
                ),
            )
            second_handle = interface.open(
                board=second,
                unique_id="two",
                target=second.target,
                worker_argv=(
                    sys.executable,
                    str(worker),
                    "ok",
                    "worker-two",
                    "recipe-two:target-two",
                ),
            )
            try:
                assert first_handle.metadata is not None
                assert second_handle.metadata is not None
                self.assertNotEqual(
                    first_handle.metadata.runtime_token,
                    second_handle.metadata.runtime_token,
                )
                self.assertNotEqual(
                    first_handle.metadata.probe_uid,
                    second_handle.metadata.probe_uid,
                )
            finally:
                interface.close(first_handle)
                interface.close(second_handle)

    def test_missing_recipe_has_actionable_recovery(self) -> None:
        with (
            TemporaryDirectory() as temporary,
            patch.object(
                server, "_provider_recipe_store", ProviderRecipeStore(FirmStore(Path(temporary)))
            ),
        ):
            board = BoardConfig("missing", "Missing", "x", "novel", "missing-provider", "x")
            with self.assertRaisesRegex(ProviderRecipeError, "get_setup_overview or setup_board"):
                server._worker_argv_for_board(board)

    def test_long_board_ids_round_trip_through_profile_store_but_bad_components_fail(self) -> None:
        # This is intentionally well beyond the former source-defined 64-character
        # cap while staying below this host's filename limit.  A longer actual
        # filesystem component is reported by the OS, not rejected by server policy.
        board_id = "board_" + "x" * 128
        board = make_board_config(
            {
                "board_id": board_id,
                "display_name": "Long board",
                "mcu_family": "example",
                "probe_family": "novel",
                "target": "example-target",
            },
            None,
        )
        self.assertEqual(board.board_id, board_id)
        with TemporaryDirectory() as temporary:
            repository = ProfileRepository(FirmStore(Path(temporary)))
            repository.commit_core(
                repository.stage_core(
                    {
                        "board_id": board_id,
                        "display_name": "Long board",
                        "mcu_part_number": "EXAMPLE-9000",
                        "mcu_family": "example",
                        "probe_family": "novel",
                        "provider_id": "example-provider",
                        "target": "example-target",
                    }
                )
            )
            self.assertEqual(repository.load(board_id).board_id, board_id)
        with self.assertRaises(ConfigError):
            make_board_config(
                {
                    "board_id": "bad/name",
                    "display_name": "Bad",
                    "mcu_family": "example",
                    "probe_family": "novel",
                    "target": "example-target",
                },
                None,
            )
        with self.assertRaises(FirmStoreError):
            FirmStore(Path.cwd()).layout.board_profile("../bad")

    def test_extended_setup_descriptions_explain_recipe_recovery(self) -> None:
        for name in ("get_setup_overview", "setup_board"):
            with self.subTest(tool=name):
                tool = server.mcp._tool_manager.get_tool(name)
                assert tool is not None
                description = tool.description.casefold()
                self.assertIn("provider_recipe", description)
                self.assertIn("example", description)
                self.assertIn("failures and recovery", description)


if __name__ == "__main__":
    unittest.main()
