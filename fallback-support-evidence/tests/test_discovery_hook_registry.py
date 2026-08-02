"""Step 1: manifest loading, strict validation, path containment, and hash drift."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.discovery_hooks import (
    DISCOVERY_HOOK_REGISTRY_ENV,
    DiscoveryHookError,
    hook_source_digest,
    load_hook_snapshot,
    parse_manifest_document,
)
from pyocd_debug_mcp.firmstore.store import FirmStore
from tests.discovery_hook_fixtures import (
    hook_entry,
    snapshot_for,
    write_manifest,
    write_raw_manifest,
)


class _TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)


class LayoutTests(_TempRoot):
    def test_firmstore_names_and_creates_the_hook_directory(self) -> None:
        store = FirmStore(self.root)

        layout = store.ensure_layout()

        self.assertEqual(layout.discovery_hooks, layout.root / "discovery_hooks")
        self.assertTrue(layout.discovery_hooks.is_dir())

    def test_firmstore_exposes_no_hook_writer(self) -> None:
        """Hooks are agent-authored; a write_hook() would make the server an author."""

        self.assertFalse(hasattr(FirmStore(self.root), "write_hook"))


class NoManifestTests(_TempRoot):
    def test_absent_manifest_yields_an_empty_snapshot(self) -> None:
        snapshot = load_hook_snapshot(self.root, environ={})

        self.assertEqual(snapshot.hooks, ())
        self.assertEqual(snapshot.manifest_sha256, "")
        self.assertEqual(snapshot.eligible_counts(), {"probe": 0, "uart": 0})
        self.assertFalse(snapshot.has_hooks_for("probe"))
        self.assertFalse(snapshot.has_hooks_for("uart"))

    def test_absent_hook_root_directory_yields_an_empty_snapshot(self) -> None:
        snapshot = load_hook_snapshot(self.root / "missing", environ={})

        self.assertEqual(snapshot.hooks, ())

    def test_environment_variable_is_not_read_at_import_time(self) -> None:
        """Trap 6: serial_resolver reads its registry at import; hooks must not."""

        source = Path(discovery_hooks.__file__).read_text(encoding="utf-8")
        marker = f'os.environ.get({DISCOVERY_HOOK_REGISTRY_ENV!r}'
        self.assertNotIn(marker, source)
        # The name appears only as a constant and inside the load function.
        module_level_env_reads = [
            line
            for line in source.splitlines()
            if line and not line[0].isspace() and "os.environ" in line
        ]
        self.assertEqual(module_level_env_reads, [])


class ProjectManifestTests(_TempRoot):
    def test_valid_project_manifest_resolves_both_kinds(self) -> None:
        snapshot = snapshot_for(
            self.root,
            [
                hook_entry("probe-one", "probe", argv=["probe"]),
                hook_entry("uart-one", "uart", argv=["uart"]),
            ],
        )

        self.assertEqual([hook.hook_id for hook in snapshot.hooks], ["probe-one", "uart-one"])
        self.assertEqual({hook.source for hook in snapshot.hooks}, {"project"})
        self.assertEqual(snapshot.eligible_counts(), {"probe": 1, "uart": 1})
        self.assertNotEqual(snapshot.manifest_sha256, "")
        for hook in snapshot.hooks:
            self.assertTrue(hook.entrypoint.is_file())
            self.assertEqual(len(hook.file_sha256), 64)
            self.assertEqual(hook.provenance, f"hook:{hook.hook_id}")

    def test_hook_id_is_casefolded_and_provenance_follows(self) -> None:
        snapshot = snapshot_for(self.root, [hook_entry("Probe-ONE", "probe", argv=["probe"])])

        self.assertEqual(snapshot.hooks[0].hook_id, "probe-one")
        self.assertEqual(snapshot.hooks[0].provenance, "hook:probe-one")

    def test_omitted_timeout_uses_the_documented_default(self) -> None:
        snapshot = snapshot_for(
            self.root,
            [hook_entry("probe-one", "probe", argv=["probe"], timeout_seconds=None)],
        )

        self.assertEqual(
            snapshot.hooks[0].timeout_seconds,
            discovery_hooks.DEFAULT_HOOK_TIMEOUT_SECONDS,
        )

    def test_omitted_argv_is_empty(self) -> None:
        write_manifest(
            self.root,
            [
                {
                    "hook_id": "probe-one",
                    "kind": "probe",
                    "platforms": ["windows", "macos", "linux"],
                    "runner": "server-python",
                    "entrypoint": "hook.py",
                }
            ],
        )

        snapshot = load_hook_snapshot(self.root, environ={})

        self.assertEqual(snapshot.hooks[0].argv, ())

    def test_server_python_command_uses_the_servers_interpreter(self) -> None:
        import sys

        snapshot = snapshot_for(self.root, [hook_entry("probe-one", "probe", argv=["probe"])])

        command = snapshot.hooks[0].command()

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[-1], "probe")


class PlatformFilterTests(_TempRoot):
    def test_hooks_are_filtered_by_platform(self) -> None:
        snapshot = snapshot_for(
            self.root,
            [
                hook_entry("win-only", "probe", argv=["probe"], platforms=["windows"]),
                hook_entry("nix-only", "probe", argv=["probe"], platforms=["linux", "macos"]),
            ],
        )

        self.assertEqual(
            [hook.hook_id for hook in snapshot.eligible("probe", "windows")], ["win-only"]
        )
        self.assertEqual(
            [hook.hook_id for hook in snapshot.eligible("probe", "linux")], ["nix-only"]
        )
        self.assertEqual(
            [hook.hook_id for hook in snapshot.eligible("probe", "macos")], ["nix-only"]
        )
        self.assertEqual(snapshot.eligible("uart", "windows"), ())

    def test_platform_tokens_are_casefolded(self) -> None:
        snapshot = snapshot_for(
            self.root,
            [hook_entry("win-only", "probe", argv=["probe"], platforms=["Windows"])],
        )

        self.assertEqual(snapshot.hooks[0].platforms, frozenset({"windows"}))

    def test_current_platform_is_one_of_the_supported_tokens(self) -> None:
        self.assertIn(discovery_hooks.current_platform(), discovery_hooks.SUPPORTED_PLATFORMS)

    def test_eligible_counts_respect_the_platform_filter(self) -> None:
        snapshot = snapshot_for(
            self.root,
            [hook_entry("win-only", "uart", argv=["uart"], platforms=["windows"])],
        )

        self.assertEqual(snapshot.eligible_counts("windows"), {"probe": 0, "uart": 1})
        self.assertEqual(snapshot.eligible_counts("linux"), {"probe": 0, "uart": 0})


class ContainmentTests(_TempRoot):
    def _expect_refusal(self, entry: dict[str, object], fragment: str) -> None:
        write_manifest(self.root, [entry])
        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.root, environ={})
        self.assertIn(fragment, str(caught.exception))

    def test_parent_traversal_is_refused(self) -> None:
        outside = self.root.parent / "outside_hook.py"
        outside.write_text("print()", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

        self._expect_refusal(
            hook_entry("escape", "probe", entrypoint="../outside_hook.py"),
            "must stay below",
        )

    def test_deep_traversal_is_refused(self) -> None:
        self._expect_refusal(
            hook_entry("escape", "probe", entrypoint="a/../../b/hook.py"),
            "must stay below",
        )

    def test_absolute_entrypoint_is_refused_for_server_python(self) -> None:
        self._expect_refusal(
            hook_entry("absolute", "probe", entrypoint=str(self.root / "hook.py")),
            "must be relative",
        )

    def test_nul_byte_in_entrypoint_is_refused(self) -> None:
        self._expect_refusal(
            hook_entry("nul", "probe", entrypoint="hook\x00.py"),
            "NUL",
        )

    def test_nul_byte_in_argv_is_refused(self) -> None:
        self._expect_refusal(
            hook_entry("nul-argv", "probe", argv=["probe\x00"]),
            "NUL",
        )

    def test_directory_entrypoint_is_refused(self) -> None:
        (self.root / "adirectory").mkdir()

        self._expect_refusal(
            hook_entry("dir", "probe", entrypoint="adirectory"),
            "not a file",
        )

    def test_missing_entrypoint_is_refused(self) -> None:
        self._expect_refusal(
            hook_entry("missing", "probe", entrypoint="nope.py"),
            "not a file",
        )

    def test_entrypoint_naming_the_root_itself_is_refused(self) -> None:
        self._expect_refusal(hook_entry("root", "probe", entrypoint="."), "must stay below")

    def test_symlink_escaping_the_root_is_refused(self) -> None:
        outside = self.root.parent / "symlink_target_hook.py"
        outside.write_text("print()", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        link = self.root / "linked.py"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        self._expect_refusal(
            hook_entry("symlink", "probe", entrypoint="linked.py"),
            "outside the hook directory",
        )

    def test_symlink_inside_the_root_is_accepted(self) -> None:
        inner = self.root / "inner"
        inner.mkdir()
        target = inner / "real_hook.py"
        target.write_text("print()", encoding="utf-8")
        link = self.root / "inside_link.py"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        snapshot = snapshot_for(
            self.root, [hook_entry("inside", "probe", entrypoint="inside_link.py")]
        )

        self.assertEqual(len(snapshot.hooks), 1)

    def test_realpath_escape_is_refused_even_when_is_relative_to_passes(self) -> None:
        """`is_relative_to` compares resolved text and cannot see a link escaping.

        Creating a symlink needs a privilege this platform may withhold, so the
        realpath comparison itself is exercised directly here. Without this the
        second half of the containment check would be untested on Windows.
        """

        write_manifest(self.root, [hook_entry("linked", "probe", entrypoint="hook.py")])
        escaped = os.path.join(os.path.realpath(tempfile.gettempdir()), "escaped_hook.py")
        original_realpath = os.path.realpath
        entrypoint = str((self.root / "hook.py").resolve())
        seen: dict[str, int] = {}

        def fake_realpath(path: object, *args: object, **kwargs: object) -> str:
            resolved = original_realpath(path, *args, **kwargs)  # type: ignore[arg-type]
            # `Path.resolve()` delegates here too, so let the resolution pass through
            # and only divert the containment re-check that follows it.
            if resolved == entrypoint:
                seen[resolved] = seen.get(resolved, 0) + 1
                if seen[resolved] > 1:
                    return escaped
            return resolved

        with unittest.mock.patch.object(os.path, "realpath", fake_realpath):
            with self.assertRaises(DiscoveryHookError) as caught:
                load_hook_snapshot(self.root, environ={})

        self.assertIn("outside the hook directory", str(caught.exception))
        self.assertGreater(seen.get(entrypoint, 0), 1, "the realpath re-check never ran")

    def test_oversized_hook_file_is_refused(self) -> None:
        big = self.root / "big_hook.py"
        big.write_bytes(b"#" * (discovery_hooks.MAX_HOOK_FILE_BYTES + 1))

        self._expect_refusal(
            hook_entry("big", "probe", entrypoint="big_hook.py"),
            "cannot be verified",
        )


class DocumentValidationTests(_TempRoot):
    def _expect_document_refusal(self, document: object, fragment: str) -> None:
        with self.assertRaises(DiscoveryHookError) as caught:
            parse_manifest_document(document)
        self.assertIn(fragment, str(caught.exception))

    def test_non_object_manifest_is_refused(self) -> None:
        self._expect_document_refusal([], "must be a JSON object")

    def test_wrong_schema_version_is_refused(self) -> None:
        self._expect_document_refusal({"schema_version": 2, "hooks": []}, "schema_version 1")

    def test_missing_schema_version_is_refused(self) -> None:
        self._expect_document_refusal({"hooks": []}, "schema_version 1")

    def test_unknown_manifest_field_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [], "executable": "evil"},
            "unknown field(s): executable",
        )

    def test_hooks_must_be_a_list(self) -> None:
        self._expect_document_refusal({"schema_version": 1, "hooks": {}}, "hooks must be a list")

    def test_unknown_hook_field_is_refused(self) -> None:
        entry = hook_entry("one", "probe")
        entry["shell"] = True
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [entry]}, "unknown field(s): shell"
        )

    def test_duplicate_hook_id_within_one_source_is_refused(self) -> None:
        self._expect_document_refusal(
            {
                "schema_version": 1,
                "hooks": [hook_entry("same", "probe"), hook_entry("same", "uart")],
            },
            "duplicate hook_id",
        )

    def test_duplicate_hook_id_is_detected_after_casefolding(self) -> None:
        self._expect_document_refusal(
            {
                "schema_version": 1,
                "hooks": [hook_entry("Same", "probe"), hook_entry("same", "uart")],
            },
            "duplicate hook_id",
        )

    def test_invalid_runner_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("one", "probe", runner="shell")]},
            "runner must be one of",
        )

    def test_invalid_kind_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("one", "flash")]},
            "kind must be one of",
        )

    def test_invalid_platform_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("one", "probe", platforms=["solaris"])]},
            "is not one of",
        )

    def test_empty_platform_list_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("one", "probe", platforms=[])]},
            "non-empty list",
        )

    def test_oversized_timeout_is_refused(self) -> None:
        self._expect_document_refusal(
            {
                "schema_version": 1,
                "hooks": [
                    hook_entry(
                        "one",
                        "probe",
                        timeout_seconds=discovery_hooks.MAX_HOOK_TIMEOUT_SECONDS + 1,
                    )
                ],
            },
            "must not exceed",
        )

    def test_zero_and_negative_timeouts_are_refused(self) -> None:
        for value in (0, -1, -0.5):
            with self.subTest(value=value):
                self._expect_document_refusal(
                    {
                        "schema_version": 1,
                        "hooks": [hook_entry("one", "probe", timeout_seconds=value)],
                    },
                    "positive and finite",
                )

    def test_non_finite_timeout_is_refused(self) -> None:
        document = {"schema_version": 1, "hooks": [hook_entry("one", "probe")]}
        document["hooks"][0]["timeout_seconds"] = float("inf")  # type: ignore[index]
        self._expect_document_refusal(document, "positive and finite")

    def test_boolean_timeout_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("one", "probe", timeout_seconds=True)]},
            "must be a number",
        )

    def test_empty_hook_id_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("", "probe")]},
            "must be non-empty text",
        )

    def test_hook_id_with_path_separators_is_refused(self) -> None:
        for bad in ("a/b", "a\\b", "a:b", "a b"):
            with self.subTest(hook_id=bad):
                self._expect_document_refusal(
                    {"schema_version": 1, "hooks": [hook_entry(bad, "probe")]},
                    "must use only",
                )

    def test_hook_id_starting_with_punctuation_is_refused(self) -> None:
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("-lead", "probe")]},
            "must start with a letter or digit",
        )

    def test_oversized_hook_id_is_refused(self) -> None:
        long_id = "a" * (discovery_hooks.MAX_HOOK_ID_CHARS + 1)
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry(long_id, "probe")]},
            "exceeds",
        )

    def test_too_many_hooks_is_refused(self) -> None:
        entries = [
            hook_entry(f"hook-{index}", "probe")
            for index in range(discovery_hooks.MAX_HOOKS_PER_MANIFEST + 1)
        ]
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": entries}, "more than"
        )

    def test_too_many_argv_entries_is_refused(self) -> None:
        argv = [f"a{index}" for index in range(discovery_hooks.MAX_ARGV_ITEMS + 1)]
        self._expect_document_refusal(
            {"schema_version": 1, "hooks": [hook_entry("one", "probe", argv=argv)]},
            "must not exceed",
        )

    def test_malformed_json_manifest_raises_a_typed_error(self) -> None:
        write_raw_manifest(self.root, "{not json")

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.root, environ={})

        self.assertIn("not valid JSON", str(caught.exception))

    def test_non_utf8_manifest_raises_a_typed_error(self) -> None:
        (self.root / discovery_hooks.MANIFEST_FILENAME).write_bytes(b"\xff\xfe{}")

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.root, environ={})

        self.assertIn("UTF-8", str(caught.exception))

    def test_a_malformed_manifest_is_recoverable_by_rewriting_it(self) -> None:
        """Trap 6: no restart may be required to recover from a bad manifest."""

        write_raw_manifest(self.root, "{not json")
        with self.assertRaises(DiscoveryHookError):
            load_hook_snapshot(self.root, environ={})

        write_manifest(self.root, [hook_entry("probe-one", "probe", argv=["probe"])])
        snapshot = load_hook_snapshot(self.root, environ={})

        self.assertEqual([hook.hook_id for hook in snapshot.hooks], ["probe-one"])


class OperatorRegistryTests(_TempRoot):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.root / "project"
        self.operator = self.root / "operator"
        self.project.mkdir()
        self.operator.mkdir()

    def _operator_env(self) -> dict[str, str]:
        return {DISCOVERY_HOOK_REGISTRY_ENV: str(self.operator / "registry.json")}

    def test_operator_registry_is_loaded_from_the_environment_variable(self) -> None:
        write_manifest(self.project, [hook_entry("project-hook", "probe", argv=["probe"])])
        write_manifest(
            self.operator,
            [hook_entry("operator-hook", "probe", argv=["probe"])],
            filename="registry.json",
        )

        snapshot = load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertEqual(
            {(hook.source, hook.hook_id) for hook in snapshot.hooks},
            {("project", "project-hook"), ("operator", "operator-hook")},
        )

    def test_same_hook_id_in_both_sources_stays_distinguishable(self) -> None:
        write_manifest(self.project, [hook_entry("shared", "probe", argv=["probe"])])
        write_manifest(
            self.operator, [hook_entry("shared", "probe", argv=["probe"])], filename="registry.json"
        )

        snapshot = load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertEqual(len(snapshot.hooks), 2)
        self.assertEqual(
            sorted(hook.friendly_id for hook in snapshot.hooks),
            ["operator/shared", "project/shared"],
        )

    def test_operator_entrypoint_is_resolved_against_the_registry_directory(self) -> None:
        write_manifest(
            self.operator, [hook_entry("operator-hook", "probe", argv=["probe"])],
            filename="registry.json",
        )

        snapshot = load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertEqual(snapshot.hooks[0].entrypoint, (self.operator / "hook.py").resolve())

    def test_missing_operator_registry_is_a_typed_error(self) -> None:
        environ = {DISCOVERY_HOOK_REGISTRY_ENV: str(self.operator / "absent.json")}

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.project, environ=environ)

        self.assertIn("does not exist", str(caught.exception))

    def test_blank_environment_variable_is_ignored(self) -> None:
        write_manifest(self.project, [hook_entry("project-hook", "probe", argv=["probe"])])

        snapshot = load_hook_snapshot(
            self.project, environ={DISCOVERY_HOOK_REGISTRY_ENV: "   "}
        )

        self.assertEqual([hook.hook_id for hook in snapshot.hooks], ["project-hook"])

    def test_executable_runner_requires_an_absolute_path(self) -> None:
        write_manifest(
            self.operator,
            [hook_entry("relative-exe", "probe", runner="executable", entrypoint="hook.py")],
            filename="registry.json",
        )

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertIn("absolute path", str(caught.exception))

    def test_executable_runner_accepts_an_absolute_path_outside_the_root(self) -> None:
        elsewhere = self.root / "vendor" / "tool.py"
        elsewhere.parent.mkdir()
        elsewhere.write_text("print()", encoding="utf-8")
        write_manifest(
            self.operator,
            [
                hook_entry(
                    "vendor-exe", "probe", runner="executable", entrypoint=str(elsewhere)
                )
            ],
            filename="registry.json",
        )

        snapshot = load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertEqual(snapshot.hooks[0].entrypoint, elsewhere.resolve())
        self.assertEqual(snapshot.hooks[0].command()[0], str(elsewhere.resolve()))

    def test_executable_runner_still_refuses_a_missing_file(self) -> None:
        write_manifest(
            self.operator,
            [
                hook_entry(
                    "vendor-exe",
                    "probe",
                    runner="executable",
                    entrypoint=str(self.root / "absent-tool"),
                )
            ],
            filename="registry.json",
        )

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertIn("not a file", str(caught.exception))


class AggregateHookCapTests(_TempRoot):
    """FIX 4 (C4): each source is capped individually, but nothing capped the total.

    64 hooks (two sources each maxed at `MAX_HOOKS_PER_MANIFEST`) x 60s sequential is
    ~64 minutes for one refresh. `MAX_HOOKS_TOTAL` refuses the aggregate outright,
    before anything executes.
    """

    def setUp(self) -> None:
        super().setUp()
        self.project = self.root / "project"
        self.operator = self.root / "operator"
        self.project.mkdir()
        self.operator.mkdir()

    def _operator_env(self) -> dict[str, str]:
        return {DISCOVERY_HOOK_REGISTRY_ENV: str(self.operator / "registry.json")}

    @staticmethod
    def _entries(prefix: str, count: int) -> list[dict[str, Any]]:
        return [hook_entry(f"{prefix}-{index}", "probe") for index in range(count)]

    def test_exactly_at_the_total_cap_is_accepted(self) -> None:
        half = discovery_hooks.MAX_HOOKS_TOTAL // 2
        write_manifest(self.project, self._entries("project", half))
        write_manifest(
            self.operator,
            self._entries("operator", discovery_hooks.MAX_HOOKS_TOTAL - half),
            filename="registry.json",
        )

        snapshot = load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertEqual(len(snapshot.hooks), discovery_hooks.MAX_HOOKS_TOTAL)

    def test_one_over_the_total_cap_across_two_sources_is_refused(self) -> None:
        half = discovery_hooks.MAX_HOOKS_TOTAL // 2
        write_manifest(self.project, self._entries("project", half + 1))
        write_manifest(
            self.operator,
            self._entries("operator", discovery_hooks.MAX_HOOKS_TOTAL - half),
            filename="registry.json",
        )

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertIn("exceeds the total cap", str(caught.exception))

    def test_two_individually_maxed_manifests_are_refused_in_aggregate(self) -> None:
        """The realistic worst case the guide's arithmetic describes: 32 + 32 = 64."""

        write_manifest(
            self.project, self._entries("project", discovery_hooks.MAX_HOOKS_PER_MANIFEST)
        )
        write_manifest(
            self.operator,
            self._entries("operator", discovery_hooks.MAX_HOOKS_PER_MANIFEST),
            filename="registry.json",
        )

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertIn(str(2 * discovery_hooks.MAX_HOOKS_PER_MANIFEST), str(caught.exception))

    def test_no_hashing_occurs_when_the_aggregate_cap_is_exceeded(self) -> None:
        """FIX 10 (C10/D9): the count check must run before any per-declaration work.

        `resolve_declaration` does symlink-safe path-containment resolution and a full
        SHA-256 hash of up to `MAX_HOOK_FILE_BYTES` per file; a maximal 32+32 pair used
        to pay for all of that before the old post-merge-only check could reject it.
        Spying on `resolve_declaration` proves it is never called at all once the
        parsed declaration count alone already exceeds the cap.
        """

        write_manifest(
            self.project, self._entries("project", discovery_hooks.MAX_HOOKS_PER_MANIFEST)
        )
        write_manifest(
            self.operator,
            self._entries("operator", discovery_hooks.MAX_HOOKS_PER_MANIFEST),
            filename="registry.json",
        )

        with unittest.mock.patch.object(discovery_hooks, "resolve_declaration") as spy:
            with self.assertRaises(DiscoveryHookError):
                load_hook_snapshot(self.project, environ=self._operator_env())

        spy.assert_not_called()

    def test_missing_entrypoints_never_surface_when_the_aggregate_cap_is_exceeded(self) -> None:
        """Black-box companion to the spy test above, with no reliance on mocking.

        Every declared hook points at an entrypoint that does not exist, so if
        per-declaration resolution ran at all, it would raise a "not a file" error
        instead of the aggregate-cap error. Getting the cap error proves resolution
        never started.
        """

        missing_entries = [
            hook_entry(f"project-{index}", "probe", entrypoint="missing.py")
            for index in range(discovery_hooks.MAX_HOOKS_PER_MANIFEST)
        ]
        write_manifest(self.project, missing_entries)
        write_manifest(
            self.operator,
            [
                hook_entry(f"operator-{index}", "probe", entrypoint="missing.py")
                for index in range(discovery_hooks.MAX_HOOKS_PER_MANIFEST)
            ],
            filename="registry.json",
        )

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.project, environ=self._operator_env())

        self.assertIn("exceeds the total cap", str(caught.exception))
        self.assertNotIn("not a file", str(caught.exception))


class OrderingTests(_TempRoot):
    def test_hooks_are_sorted_by_source_kind_then_id(self) -> None:
        snapshot = snapshot_for(
            self.root,
            [
                hook_entry("zulu", "uart", argv=["uart"]),
                hook_entry("alpha", "uart", argv=["uart"]),
                hook_entry("yankee", "probe", argv=["probe"]),
                hook_entry("bravo", "probe", argv=["probe"]),
            ],
        )

        self.assertEqual(
            [(hook.kind, hook.hook_id) for hook in snapshot.hooks],
            [("probe", "bravo"), ("probe", "yankee"), ("uart", "alpha"), ("uart", "zulu")],
        )

    def test_ordering_is_stable_across_repeated_loads(self) -> None:
        entries = [
            hook_entry("zulu", "uart", argv=["uart"]),
            hook_entry("alpha", "probe", argv=["probe"]),
            hook_entry("mike", "probe", argv=["probe"]),
        ]
        write_manifest(self.root, entries)

        orders = [
            [hook.friendly_id for hook in load_hook_snapshot(self.root, environ={}).hooks]
            for _ in range(5)
        ]

        self.assertEqual(len(set(map(tuple, orders))), 1)

    def test_probe_hooks_sort_before_uart_hooks_even_when_declared_last(self) -> None:
        """Kind orders ahead of hook_id, so a 'z' probe still precedes an 'a' uart."""

        snapshot = snapshot_for(
            self.root,
            [hook_entry("a-uart", "uart", argv=["uart"]), hook_entry("z-probe", "probe", argv=["probe"])],
        )

        self.assertEqual([hook.kind for hook in snapshot.hooks], ["probe", "uart"])

    def test_hook_id_must_be_unique_within_a_source_across_kinds(self) -> None:
        """Provenance is `hook:{hook_id}`, so one ID may not name two hooks."""

        write_manifest(
            self.root, [hook_entry("same", "uart", argv=["uart"]), hook_entry("same", "probe")]
        )

        with self.assertRaises(DiscoveryHookError) as caught:
            load_hook_snapshot(self.root, environ={})

        self.assertIn("duplicate hook_id", str(caught.exception))


class HashDriftTests(_TempRoot):
    def test_digest_matches_the_recorded_value_before_any_change(self) -> None:
        snapshot = snapshot_for(self.root, [hook_entry("probe-one", "probe", argv=["probe"])])
        spec = snapshot.hooks[0]

        self.assertEqual(hook_source_digest(spec), spec.file_sha256)

    def test_hook_file_change_is_detected_before_execution(self) -> None:
        snapshot = snapshot_for(self.root, [hook_entry("probe-one", "probe", argv=["probe"])])
        spec = snapshot.hooks[0]
        launches = self.root / "launches.txt"
        spec.entrypoint.write_text(
            f"open({str(launches)!r}, 'a').write('launched')", encoding="utf-8"
        )

        execution = discovery_hooks.execute_hook(spec)

        self.assertEqual(execution.outcome, "source_changed")
        self.assertEqual(execution.failure_code, "discovery/hook-source-changed")
        self.assertIn("refresh", execution.failure_detail)
        self.assertIsNone(execution.output)
        # Refused *without running anything*.
        self.assertFalse(launches.exists())
        self.assertIsNone(execution.exit_code)

    def test_a_deleted_hook_file_is_refused_not_executed(self) -> None:
        snapshot = snapshot_for(self.root, [hook_entry("probe-one", "probe", argv=["probe"])])
        spec = snapshot.hooks[0]
        spec.entrypoint.unlink()

        execution = discovery_hooks.execute_hook(spec)

        self.assertEqual(execution.outcome, "source_changed")
        self.assertIn("unverifiable", execution.failure_detail)

    def test_a_refreshed_snapshot_admits_the_changed_file_again(self) -> None:
        write_manifest(self.root, [hook_entry("probe-one", "probe", argv=["probe"])])
        first = load_hook_snapshot(self.root, environ={})
        original = first.hooks[0].file_sha256
        (self.root / "hook.py").write_text(
            '\n'.join(
                [
                    "import json, sys",
                    'json.dump({"schema_version": 1, "kind": "probe", "probes": []}, sys.stdout)',
                ]
            ),
            encoding="utf-8",
        )

        second = load_hook_snapshot(self.root, environ={})

        self.assertNotEqual(second.hooks[0].file_sha256, original)
        execution = discovery_hooks.execute_hook(second.hooks[0])
        self.assertTrue(execution.ok, execution.failure_detail)

    def test_manifest_digest_changes_when_the_manifest_changes(self) -> None:
        first = snapshot_for(self.root, [hook_entry("probe-one", "probe", argv=["probe"])])
        second = snapshot_for(
            self.root,
            [hook_entry("probe-one", "probe", argv=["probe"], timeout_seconds=11.0)],
        )

        self.assertNotEqual(first.manifest_sha256, second.manifest_sha256)

    def test_manifest_digest_is_stable_for_identical_content(self) -> None:
        entries = [hook_entry("probe-one", "probe", argv=["probe"])]
        write_manifest(self.root, entries)

        first = load_hook_snapshot(self.root, environ={})
        second = load_hook_snapshot(self.root, environ={})

        self.assertEqual(first.manifest_sha256, second.manifest_sha256)


class OutputSchemaTests(unittest.TestCase):
    def test_published_manifest_example_is_accepted_by_the_real_validator(self) -> None:
        declarations = parse_manifest_document(discovery_hooks.MANIFEST_SCHEMA_EXAMPLE)

        self.assertEqual(
            [declaration.hook_id for declaration in declarations],
            ["local-probe-fallback", "local-uart-fallback"],
        )

    def test_published_probe_output_example_is_accepted(self) -> None:
        output = discovery_hooks.parse_hook_output(
            discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE, expected_kind="probe"
        )

        self.assertEqual(len(output.probes), 1)

    def test_published_uart_output_example_is_accepted(self) -> None:
        output = discovery_hooks.parse_hook_output(
            discovery_hooks.UART_OUTPUT_SCHEMA_EXAMPLE, expected_kind="uart"
        )

        self.assertEqual(len(output.uarts), 1)
        self.assertTrue(output.uarts[0].has_stable_identity)

    def test_example_hook_sources_are_valid_python_producing_valid_output(self) -> None:
        for source, kind in (
            (discovery_hooks.EXAMPLE_PROBE_HOOK_SOURCE, "probe"),
            (discovery_hooks.EXAMPLE_UART_HOOK_SOURCE, "uart"),
        ):
            with self.subTest(kind=kind):
                compile(source, f"<{kind}-example>", "exec")

    def test_unknown_output_field_is_refused(self) -> None:
        document = dict(discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE)
        document["active_plan"] = {"plan_id": "forged"}

        with self.assertRaises(DiscoveryHookError) as caught:
            discovery_hooks.parse_hook_output(document, expected_kind="probe")

        self.assertIn("unknown field(s): active_plan", str(caught.exception))

    def test_probe_output_may_not_carry_uart_rows(self) -> None:
        document = dict(discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE)
        document["uarts"] = []

        with self.assertRaises(DiscoveryHookError) as caught:
            discovery_hooks.parse_hook_output(document, expected_kind="probe")

        self.assertIn("must not carry 'uarts'", str(caught.exception))

    def test_uart_output_may_not_carry_probe_rows(self) -> None:
        document = dict(discovery_hooks.UART_OUTPUT_SCHEMA_EXAMPLE)
        document["probes"] = []

        with self.assertRaises(DiscoveryHookError) as caught:
            discovery_hooks.parse_hook_output(document, expected_kind="uart")

        self.assertIn("must not carry 'probes'", str(caught.exception))

    def test_out_of_range_usb_identifier_is_refused(self) -> None:
        for field in ("vid", "pid"):
            for value in (-1, 0x10000):
                with self.subTest(field=field, value=value):
                    document = json.loads(json.dumps(discovery_hooks.UART_OUTPUT_SCHEMA_EXAMPLE))
                    document["uarts"][0][field] = value
                    with self.assertRaises(DiscoveryHookError) as caught:
                        discovery_hooks.parse_hook_output(document, expected_kind="uart")
                    self.assertIn("16-bit USB identifier", str(caught.exception))

    def test_session_local_uart_row_is_accepted_without_stable_fields(self) -> None:
        output = discovery_hooks.parse_hook_output(
            {
                "schema_version": 1,
                "kind": "uart",
                "uarts": [{"port_path": "COM9", "description": "Session UART"}],
            },
            expected_kind="uart",
        )

        self.assertFalse(output.uarts[0].has_stable_identity)

    def test_missing_required_probe_field_is_refused(self) -> None:
        for field in ("provider", "unique_id", "description"):
            with self.subTest(field=field):
                document = json.loads(json.dumps(discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE))
                del document["probes"][0][field]
                with self.assertRaises(DiscoveryHookError) as caught:
                    discovery_hooks.parse_hook_output(document, expected_kind="probe")
                self.assertIn(field, str(caught.exception))

    def test_empty_row_list_is_accepted(self) -> None:
        output = discovery_hooks.parse_hook_output(
            {"schema_version": 1, "kind": "probe", "probes": []}, expected_kind="probe"
        )

        self.assertEqual(output.probes, ())

    def test_provider_is_casefolded(self) -> None:
        document = json.loads(json.dumps(discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE))
        document["probes"][0]["provider"] = "CMSISDAP"

        output = discovery_hooks.parse_hook_output(document, expected_kind="probe")

        self.assertEqual(output.probes[0].provider, "cmsisdap")

    def test_unique_id_case_is_preserved_because_it_is_a_pyocd_selector(self) -> None:
        document = json.loads(json.dumps(discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE))
        document["probes"][0]["unique_id"] = "AbCdEf"

        output = discovery_hooks.parse_hook_output(document, expected_kind="probe")

        self.assertEqual(output.probes[0].unique_id, "AbCdEf")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
