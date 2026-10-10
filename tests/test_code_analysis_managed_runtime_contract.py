"""Behavior contracts from the accepted target runtime mapping, independently.

Resolve through the production launch context, never a replacement resolver.
Valid controls precede failures to prevent an unresolved stub passing rejection
tests. Synthetic payloads prove only unit/protocol contracts, not distribution
completeness, Windows DLL closure, analyzer compatibility or installed acceptance.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.code_analysis_managed_support import (
    EVIDENCE,
    ManagedCase,
    RuntimeFixture,
    digest,
    default_mapping,
    host_target,
    protocol_redirect,
    production,
    write_json,
)
from tests.code_analysis_support import AnalysisProject, Call

RESOURCE = "analysis/clangd/lib/clang/23/include/stddef.h"
EXECUTABLE = default_mapping()["programs"]["clangd"]["executable"]
PATH_CLANGD = "clangd.exe" if os.name == "nt" else "clangd"


class WindowsCanonicalDatabaseAlias(unittest.TestCase):
    def test_directory_junction_root_and_build_select_same_canonical_database(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows directory junction contract")
        base = EVIDENCE / "j" / uuid.uuid4().hex[:6]
        project = AnalysisProject(base / "project")
        source = project.write_text("main.c", "int value(void) { return 7; }\n")
        database = project.write_database([project.entry("main.c")])
        original = database.read_bytes()
        junction = project.root / "build"
        argv = [
            str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(junction),
            str(project.root),
        ]
        created = subprocess.run(argv, capture_output=True, check=False)
        write_json(
            base / "junction-create.json",
            {
                "argv": argv,
                "exit": created.returncode,
                "stdout": created.stdout.decode("utf-8", "replace"),
                "stderr": created.stderr.decode("utf-8", "replace"),
                "junction": str(junction),
                "target": str(project.root),
            },
        )
        self.assertEqual(created.returncode, 0, "Directory junction prerequisite failed")
        observations = []
        try:
            alias = junction / "compile_commands.json"
            self.assertEqual(junction.resolve(), project.root.resolve())
            self.assertTrue(database.samefile(alias))
            self.assertEqual(database.resolve(), alias.resolve())
            for selected in (None, "compile_commands.json", "build/compile_commands.json"):
                with self.subTest(selected=selected):
                    if selected is not None:
                        project.write_config(
                            {"schema_version": 1, "compilation_database": selected}
                        )
                    snapshot = production("code_analysis_config").load_analysis_config(project.root)
                    observations.append(
                        {
                            "selected": selected,
                            "canonical_database": str(snapshot.database_path),
                            "database_sha256": snapshot.database_sha256,
                            "problems": [p.to_payload() for p in snapshot.problems],
                        }
                    )
                    self.assertFalse(snapshot.problems, observations[-1])
                    self.assertEqual(snapshot.database_path, database.resolve())
                    self.assertEqual(snapshot.database_sha256, digest(database))
                    self.assertEqual(len(snapshot.entries), 1)
                    self.assertEqual(snapshot.entries[0].source, source.resolve())
                    self.assertEqual(database.read_bytes(), original)
                    self.assertEqual(alias.read_bytes(), original)
        finally:
            # rmdir removes precisely this owned junction, never its target tree.
            self.assertEqual(junction.resolve(), project.root.resolve())
            junction.rmdir()
            write_json(
                base / "junction-selection.json",
                {
                    "observations": observations,
                    "junction_removed": not junction.exists(),
                    "target_preserved": project.root.is_dir(),
                    "database_preserved": database.read_bytes() == original,
                    "file_symlink_credit": False,
                },
            )
        self.assertFalse(junction.exists())
        self.assertTrue(project.root.is_dir())
        self.assertEqual(database.read_bytes(), original)
        # Negative control: a distinct build/ database with the same bytes is
        # ambiguous, so the alias result above came from file identity.
        project.path("byo-analysis.json").unlink()
        distinct = project.write_database([project.entry("main.c")], "build/compile_commands.json")
        self.assertEqual(distinct.read_bytes(), original)
        self.assertFalse(distinct.samefile(database))
        snapshot = production("code_analysis_config").load_analysis_config(project.root)
        codes = [p.code for p in snapshot.problems]
        write_json(base / "distinct-build-control.json", {"problems": codes})
        self.assertIn("analysis/database-ambiguous", codes)


class ManagedSelection(ManagedCase):
    def test_complete_runtime_resolves_both_analyzers_without_path(self) -> None:
        with self.observer() as observed:
            for name in ("clangd", "cppcheck"):
                with self.subTest(program=name):
                    program = self.resolve(name)
                    self.assert_usable(program, name)
                    expected = self.fixture.mapping["programs"][name]
                    directories = (
                        [expected["resource_dir"]]
                        if name == "clangd"
                        else [expected["cfg_dir"], expected["platforms_dir"]]
                    )
                    self.assertEqual(
                        {Path(p).resolve() for p in program.data_paths},
                        {(self.fixture.root / p).resolve() for p in directories},
                    )
                    self.assertTrue(program.identity, "Validated receipt identity is required")
            self.assertEqual(len(observed.calls), 2)

    def test_valid_supplied_runtime_precedes_unmanaged_path(self) -> None:
        context = self.fixture.context(self.project, managed=False)
        with self.observer() as observed:
            self.assert_usable(self.resolve(context=context))
            self.assertEqual(len(observed.calls), 1)

    def test_managed_failure_cannot_substitute_visible_path(self) -> None:
        path_bin = self.base / "path-bin"
        path_bin.mkdir()
        shutil.copy2(self.fixture.root / EXECUTABLE, path_bin / PATH_CLANGD)
        self.fixture.mapping_path.unlink()
        with self.observer() as observed, mock.patch.dict(os.environ, {"PATH": str(path_bin)}):
            self.assert_rejected_without_spawn(observed, "runtime.json")

    def test_unmanaged_supplied_runtime_failure_retains_cause_and_fallback_warning(self) -> None:
        path_bin = self.base / "path-bin"
        path_bin.mkdir()
        shutil.copy2(self.fixture.root / EXECUTABLE, path_bin / PATH_CLANGD)
        self.fixture.mapping_path.unlink()
        context = self.fixture.context(self.project, managed=False)
        with self.observer() as observed, mock.patch.dict(os.environ, {"PATH": str(path_bin)}):
            original = observed.redirect

            def redirect(argv):
                if Path(argv[0]).resolve() == (path_bin / PATH_CLANGD).resolve():
                    from tests.code_analysis_support import FAKE_CLANGD
                    import sys

                    return [
                        sys.executable,
                        str(FAKE_CLANGD),
                        "--scenario",
                        str(self.fake.scenario_path),
                        *argv[1:],
                    ]
                return original(argv)

            with mock.patch.object(observed, "redirect", redirect):
                service = production("services.code_analysis").CodeAnalysisService(context)
                try:
                    status = service.status()
                finally:
                    service.shutdown()
            write_json(self.base / "fallback-status.json", status)
            self.assertEqual(status["status"], "ok", status)
            self.assertEqual(status["data"]["executable_origin"], "path")
            self.assertEqual(
                Path(status["data"]["executable"]).resolve(), (path_bin / PATH_CLANGD).resolve()
            )
            self.assertTrue(
                any(
                    "runtime.json" in json.dumps(p)
                    for p in status["data"]["configuration_problems"]
                ),
                status,
            )
            self.assertTrue(
                any("fallback" in w.lower() or "path" in w.lower() for w in status["warnings"]),
                status,
            )


class MappingSyntax(ManagedCase):
    def test_license_and_dependency_lists_reject_duplicates_and_missing_required_notices(
        self,
    ) -> None:
        with self.observer() as observed:
            for case in ("duplicate-license", "missing-notice", "duplicate-dependency"):
                with self.subTest(case=case):
                    self.require_valid_control(observed)
                    mapping = copy.deepcopy(self.fixture.mapping)
                    program = mapping["programs"]["cppcheck"]
                    if case == "duplicate-license":
                        program["licenses"].append(program["licenses"][0])
                    elif case == "missing-notice":
                        program["licenses"].pop()
                    else:
                        program["dependencies"] = ["analysis/cppcheck/vendor.dll"] * 2
                    self.fixture.replace_mapping(mapping)
                    self.assert_rejected_without_spawn(
                        observed, "dependencies" if case == "duplicate-dependency" else "licenses"
                    )

    def test_schema_version_rejects_bool_float_string_null_and_other_integer(self) -> None:
        with self.observer() as observed:
            for value in (True, False, 1.0, "1", None, 2):
                with self.subTest(value=repr(value)):
                    self.require_valid_control(observed)
                    mapping = copy.deepcopy(self.fixture.mapping)
                    mapping["schema_version"] = value
                    self.fixture.replace_mapping(mapping)
                    self.assert_rejected_without_spawn(observed, "schema_version")

    def test_unknown_missing_and_wrong_type_fields_at_every_closed_level(self) -> None:
        # One case per declared field; no defaults or coercion are permitted.
        canonical = self.fixture.mapping
        levels = [([], canonical), (["programs"], canonical["programs"])] + [
            (["programs", name], p) for name, p in canonical["programs"].items()
        ]
        with self.observer() as observed:
            for prefix, fields in levels:
                for field_name in [*fields, "command"]:
                    modes = (
                        ("unknown",)
                        if field_name == "command"
                        else ("missing", "null", "wrong-type")
                    )
                    for mode in modes:
                        label = ".".join([*prefix, field_name])
                        with self.subTest(field=label, mode=mode):
                            self.require_valid_control(observed)
                            mapping = copy.deepcopy(canonical)
                            target = mapping
                            for component in prefix:
                                target = target[component]
                            if mode == "missing":
                                del target[field_name]
                            else:
                                target[field_name] = (
                                    None if mode == "null" else {"unexpected": "value"}
                                )
                            self.fixture.replace_mapping(mapping)
                            self.assert_rejected_without_spawn(observed, field_name)

    def test_strict_json_rejects_duplicate_keys_nonfinite_numbers_and_nonobject_root(self) -> None:
        raw = json.dumps(self.fixture.mapping).encode()
        cases = {
            "duplicate-top": (
                raw.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
                "schema_version",
            ),
            "duplicate-program": (
                raw.replace(b'"version": "23.1.0"', b'"version": "23.1.0", "version": "23.1.0"'),
                "version",
            ),
            "NaN": (
                raw.replace(b'"schema_version": 1', b'"schema_version": NaN'),
                "schema_version",
            ),
            "Infinity": (
                raw.replace(b'"schema_version": 1', b'"schema_version": Infinity'),
                "schema_version",
            ),
            "negative-Infinity": (
                raw.replace(b'"schema_version": 1', b'"schema_version": -Infinity'),
                "schema_version",
            ),
            "null-root": (b"null", "runtime.json"),
            "array-root": (b"[]", "runtime.json"),
        }
        with self.observer() as observed:
            for case, (payload, hint) in cases.items():
                with self.subTest(case=case):
                    self.require_valid_control(observed)
                    self.fixture.replace_mapping(payload)
                    self.assert_rejected_without_spawn(observed, hint)

    def test_actual_decoded_lf_cannot_pass_any_absolute_end_pattern(self) -> None:
        cases = [
            ("clangd", "version", "23.1.0\n"),
            ("clangd", "resource_dir", "analysis/clangd/lib/clang/23\n"),
            ("clangd", "dependencies", ["analysis/clangd/bin/extra.dll\n"]),
            ("cppcheck", "dependencies", ["analysis/cppcheck/extra.dll\n"]),
            (
                "clangd",
                "licenses",
                ["analysis/clangd/LICENSE.TXT", "analysis/clangd/licenses/extra\n"],
            ),
        ]
        with self.observer() as observed:
            for program, field, value in cases:
                with self.subTest(program=program, field=field):
                    self.require_valid_control(observed)
                    mapping = copy.deepcopy(self.fixture.mapping)
                    mapping["programs"][program][field] = value
                    self.fixture.replace_mapping(mapping)
                    self.assert_rejected_without_spawn(observed, field)


class TargetAndVersion(ManagedCase):
    def test_mapping_release_and_actual_host_must_agree(self) -> None:
        with self.observer() as observed:
            for where, field, value in [
                (
                    "mapping",
                    "platform",
                    "windows" if self.fixture.mapping["platform"] != "windows" else "linux",
                ),
                (
                    "mapping",
                    "architecture",
                    "x86_64" if self.fixture.mapping["architecture"] == "aarch64" else "aarch64",
                ),
                (
                    "release",
                    "platform",
                    "windows" if self.fixture.mapping["platform"] != "windows" else "linux",
                ),
                (
                    "release",
                    "architecture",
                    "x86_64" if self.fixture.mapping["architecture"] == "aarch64" else "aarch64",
                ),
            ]:
                with self.subTest(where=where, field=field):
                    self.require_valid_control(observed)
                    if where == "mapping":
                        mapping = copy.deepcopy(self.fixture.mapping)
                        mapping[field] = value
                        self.fixture.replace_mapping(mapping)
                    else:
                        self.fixture.manifest[field] = value
                        self.fixture.save_manifest()
                    self.assert_rejected_without_spawn(observed, field)
                    self.fixture.manifest.update(
                        platform=self.fixture.mapping["platform"],
                        architecture=self.fixture.mapping["architecture"],
                    )
                    self.fixture.replace_mapping(self.fixture.mapping)

    def test_arm64_host_rejects_x86_64_mapping_before_spawn(self) -> None:
        with self.observer() as observed:
            self.require_valid_control(observed)
            with mock.patch(
                "platform.machine",
                return_value="x86_64"
                if self.fixture.mapping["architecture"] == "aarch64"
                else "ARM64",
            ):
                self.assert_rejected_without_spawn(observed, "architecture")

    def test_resource_major_must_equal_numeric_clangd_version(self) -> None:
        with self.observer() as observed:
            self.require_valid_control(observed)
            mapping = copy.deepcopy(self.fixture.mapping)
            mapping["programs"]["clangd"]["resource_dir"] = "analysis/clangd/lib/clang/22"
            self.fixture.replace_mapping(mapping)
            self.assert_rejected_without_spawn(observed, "resource_dir")

    def test_probe_numeric_version_mismatch_is_unavailable_and_identity_cached(self) -> None:
        with self.observer() as observed:
            self.require_valid_control(observed)
            self.fake.configure(version={"banner": "clangd version 22.1.0"})
            before = len(observed.calls)
            first = self.resolve()
            second = self.resolve()
            for result in (first, second):
                self.assertTrue(result.problems, result)
                self.assertEqual(result.problems[0].code, "analysis/executable-unavailable")
                description = json.dumps([p.to_payload() for p in result.problems])
                self.assertIn("23.1.0", description)
                self.assertIn("22.1.0", description)
            self.assertEqual(len(observed.calls), before + 1)

    def test_wrong_pe_machine_is_rejected_before_probe(self) -> None:
        if host_target() != "windows-x86_64":
            self.skipTest(
                "Original Windows PE input checks; neutral format cases are in portability"
            )
        with self.observer() as observed:
            self.require_valid_control(observed)
            executable = self.fixture.root / EXECUTABLE
            payload = bytearray(executable.read_bytes())
            offset = int.from_bytes(payload[0x3C:0x40], "little")
            payload[offset + 4 : offset + 6] = (0xAA64).to_bytes(2, "little")
            executable.write_bytes(payload)
            self.fixture.seal()
            self.assert_rejected_without_spawn(observed, "clangd")


class PayloadIntegrity(ManagedCase):
    def test_sbom_uses_canonical_metadata_kind_and_never_probes_wrong_kind(self) -> None:
        entry = self.fixture.inventory_entry("sbom.cdx.json")
        self.assertEqual(entry["kind"], "metadata")
        self.assertIs(entry["executable"], False)
        with self.observer() as observed:
            for name in ("cppcheck", "clangd"):
                self.assert_usable(self.resolve(name), name)
            for kind in ("sbom", "analysis-resource", "license"):
                with self.subTest(kind=kind):
                    # Keep classification independent of cached-receipt mutation.
                    self.fixture = RuntimeFixture(self.base / ("sbom-kind-" + kind))
                    self.context = self.fixture.context(self.project)
                    for name in ("cppcheck", "clangd"):
                        self.require_valid_control(observed, name)
                    self.fixture.inventory_entry("sbom.cdx.json")["kind"] = kind
                    self.fixture.save_manifest()
                    for name in ("cppcheck", "clangd"):
                        self.assert_rejected_without_spawn(observed, "sbom.cdx.json", name)

    def test_inventory_size_and_executable_flags_reject_type_coercion(self) -> None:
        with self.observer() as observed:
            for field, value in [
                ("size", True),
                ("size", 1.0),
                ("size", "1"),
                ("executable", 1),
                ("executable", "false"),
            ]:
                with self.subTest(field=field, value=repr(value)):
                    self.require_valid_control(observed)
                    self.fixture.inventory_entry(RESOURCE)[field] = value
                    self.fixture.save_manifest()
                    self.assert_rejected_without_spawn(observed, field)
                    self.fixture.seal()

    def test_required_data_directory_cannot_be_an_inventoried_regular_file(self) -> None:
        with self.observer() as observed:
            self.require_valid_control(observed)
            platforms = self.fixture.root / "analysis/cppcheck/platforms"
            (platforms / "win64.xml").unlink()
            platforms.rmdir()
            platforms.write_bytes(b"a regular file is not the required directory")
            self.fixture.seal()
            self.assert_rejected_without_spawn(observed, "platforms", "cppcheck")

    def test_missing_extra_wrong_bytes_and_wrong_inventory_are_rejected_before_spawn(self) -> None:
        with self.observer() as observed:
            for case in (
                "missing-resource",
                "missing-cfg",
                "empty-platforms",
                "missing-license",
                "extra-leaf",
                "wrong-data",
                "wrong-executable",
                "wrong-size",
                "wrong-hash",
                "missing-files-entry",
                "wrong-kind",
                "wrong-executable-flag",
            ):
                with self.subTest(case=case):
                    self.require_valid_control(observed)
                    self.fixture = RuntimeFixture(self.base / case)
                    self.context = self.fixture.context(self.project)
                    observed.redirect = protocol_redirect(self.fixture, self.fake)
                    name, hint = "clangd", "stddef.h"
                    if case == "missing-resource":
                        (self.fixture.root / RESOURCE).unlink()
                    elif case == "missing-cfg":
                        (self.fixture.root / "analysis/cppcheck/cfg/std.cfg").unlink()
                        name, hint = "cppcheck", "std.cfg"
                    elif case == "empty-platforms":
                        (self.fixture.root / "analysis/cppcheck/platforms/win64.xml").unlink()
                        name, hint = "cppcheck", "platforms"
                    elif case == "missing-license":
                        (self.fixture.root / "analysis/clangd/LICENSE.TXT").unlink()
                        hint = "LICENSE.TXT"
                    elif case == "extra-leaf":
                        self.fixture.put("analysis/clangd/lib/clang/23/include/extra.h", b"extra")
                        hint = "extra.h"
                    elif case in ("wrong-data", "wrong-executable"):
                        target = RESOURCE if case == "wrong-data" else EXECUTABLE
                        self.fixture.put(
                            target, (self.fixture.root / target).read_bytes() + b"changed"
                        )
                        hint = Path(target).name
                    elif case == "missing-files-entry":
                        self.fixture.inventory.remove(self.fixture.inventory_entry(RESOURCE))
                        self.fixture.save_manifest()
                    else:
                        key, value = {
                            "wrong-size": ("size", 99),
                            "wrong-hash": ("sha256", "0" * 64),
                            "wrong-kind": ("kind", "metadata"),
                            "wrong-executable-flag": ("executable", True),
                        }[case]
                        self.fixture.inventory_entry(RESOURCE)[key] = value
                        self.fixture.save_manifest()
                    self.assert_rejected_without_spawn(observed, hint, name)

    def test_mapping_and_sbom_bytes_require_inventory_identity(self) -> None:
        with self.observer() as observed:
            for target in ("analysis/runtime.json", "sbom.cdx.json"):
                with self.subTest(target=target):
                    self.require_valid_control(observed)
                    path = self.fixture.root / target
                    original = path.read_bytes()
                    path.write_bytes(original + b" ")
                    self.assert_rejected_without_spawn(observed, Path(target).name)
                    path.write_bytes(original)

    def test_sbom_application_version_license_and_every_payload_relationship_are_required(
        self,
    ) -> None:
        with self.observer() as observed:
            original = copy.deepcopy(self.fixture.sbom)
            for case in (
                "missing-application",
                "wrong-version",
                "missing-licenses",
                "missing-provenance",
                "missing-file",
                "missing-ownership",
                "wrong-file-hash",
                "unknown-reference",
            ):
                with self.subTest(case=case):
                    self.require_valid_control(observed)
                    self.fixture.sbom = copy.deepcopy(original)
                    components = self.fixture.sbom["components"]
                    app = next(c for c in components if c["bom-ref"] == "byo-analysis:clangd")
                    leaf = next(c for c in components if c.get("name") == RESOURCE)
                    relation = next(
                        d for d in self.fixture.sbom["dependencies"] if d["ref"] == app["bom-ref"]
                    )
                    if case == "missing-application":
                        components.remove(app)
                    elif case == "wrong-version":
                        app["version"] = "22.1.0"
                    elif case == "missing-licenses":
                        app["licenses"] = []
                    elif case == "missing-provenance":
                        app["properties"] = []
                    elif case == "missing-file":
                        components.remove(leaf)
                    elif case == "missing-ownership":
                        relation["dependsOn"].remove(leaf["bom-ref"])
                    elif case == "wrong-file-hash":
                        leaf["hashes"][0]["content"] = "0" * 64
                    else:
                        relation["dependsOn"].append("absent-file-reference")
                    self.fixture.save_sbom()
                    self.assert_rejected_without_spawn(observed, "sbom")

    def test_mapped_dll_must_be_inventoried_owned_and_present(self) -> None:
        with self.observer() as observed:
            self.require_valid_control(observed)
            mapping = copy.deepcopy(self.fixture.mapping)
            mapping["programs"]["clangd"]["dependencies"] = ["analysis/clangd/bin/vendor.dll"]
            self.fixture.replace_mapping(mapping)
            self.assert_rejected_without_spawn(observed, "vendor.dll")


class PathIdentity(ManagedCase):
    def test_paths_reject_escape_case_backslash_reserved_and_trailing_components(self) -> None:
        paths = [
            "../clangd.exe",
            "C:/tools/clangd.exe",
            "//host/share/clangd.exe",
            "analysis\\clangd\\bin\\clangd.exe",
            "analysis/clangd/bin/./clangd.exe",
            "analysis/clangd/bin//clangd.exe",
            "analysis/Clangd/bin/clangd.exe",
            "analysis/clangd/licenses/CON",
            "analysis/clangd/licenses/NUL.txt",
            "analysis/clangd/licenses/COM1.txt",
            "analysis/clangd/licenses/trailing. ",
        ]
        with self.observer() as observed:
            for path in paths:
                with self.subTest(path=path):
                    self.require_valid_control(observed)
                    mapping = copy.deepcopy(self.fixture.mapping)
                    field = "licenses" if "/licenses/" in path else "executable"
                    mapping["programs"]["clangd"][field] = (
                        ["analysis/clangd/LICENSE.TXT", path] if field == "licenses" else path
                    )
                    self.fixture.replace_mapping(mapping)
                    self.assert_rejected_without_spawn(observed, field)

    def test_inventory_case_collision_and_wrong_spelling_are_rejected(self) -> None:
        with self.observer() as observed:
            for case in ("collision", "wrong-spelling"):
                with self.subTest(case=case):
                    self.require_valid_control(observed)
                    entry = self.fixture.inventory_entry(RESOURCE)
                    wrong = dict(entry, path=RESOURCE.replace("stddef.h", "STDDEF.h"))
                    if case == "collision":
                        self.fixture.inventory.append(wrong)
                    else:
                        entry["path"] = wrong["path"]
                    self.fixture.save_manifest()
                    self.assert_rejected_without_spawn(observed, "stddef")
                    self.fixture.seal()

    def test_directory_junction_cannot_escape_the_runtime(self) -> None:
        with self.observer() as observed:
            self.require_valid_control(observed)
            directory = self.fixture.root / "analysis/clangd/lib/clang/23/include"
            outside = self.base / "outside"
            self.assertTrue(directory.resolve().is_relative_to(self.base.resolve()))
            self.assertTrue(outside.resolve().is_relative_to(self.base.resolve()))
            directory.rename(outside)
            # Native Windows junction creation requires no symlink privilege.
            if os.name == "nt":
                result = subprocess_junction(directory, outside)
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                directory.symlink_to(outside, target_is_directory=True)
            try:
                self.assert_rejected_without_spawn(observed, "include")
            finally:
                if os.name == "nt":
                    directory.rmdir()  # remove just the task-owned junction
                else:
                    directory.unlink()


def subprocess_junction(directory: Path, target: Path):
    import subprocess

    return subprocess.run(
        [
            os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(directory),
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class ReceiptLifetime(ManagedCase):
    def test_changed_namespace_directory_is_reenumerated_before_cached_probe(self) -> None:
        with self.observer() as observed:
            self.assert_usable(self.resolve())
            self.fixture.put("analysis/clangd/lib/clang/23/include/extra.h", b"unapproved")
            before = len(observed.calls)
            changed = self.resolve()
            self.assertTrue(changed.problems, changed)
            self.assertEqual(
                len(observed.calls), before, "Receipt cannot hide an added namespace leaf"
            )

    def test_protocol_query_passes_canonical_mapped_resource_directory_in_actual_owned_argv(
        self,
    ) -> None:
        self.fake.configure(
            responses={"textDocument/hover": {"result": {"contents": "managed hover"}}}
        )
        with self.observer() as observed:
            service = self.service(observed)
            result = service.hover(str(self.source), 2, 25, timeout_seconds=10)
            write_json(self.base / "resource-query.json", result)
            try:
                self.assertEqual(result["status"], "ok", result)
                argv = [a for a in observed.calls if "--version" not in a]
                self.assertEqual(len(argv), 1)
                self.assertIn(
                    "--resource-dir=" + str(self.fixture.root / "analysis/clangd/lib/clang/23"),
                    argv[0],
                )
            finally:
                service.shutdown()

    def test_unchanged_receipt_reuses_owned_version_probe(self) -> None:
        with self.observer() as observed:
            first, second = self.resolve(), self.resolve()
            self.assert_usable(first)
            self.assert_usable(second)
            self.assertEqual(first.identity, second.identity)
            self.assertEqual(len(observed.calls), 1)

    def test_executable_and_data_changes_invalidate_then_later_call_revalidates(self) -> None:
        with self.observer() as observed:
            for target in (EXECUTABLE, RESOURCE):
                with self.subTest(target=target):
                    self.fixture = RuntimeFixture(self.base / ("receipt-" + Path(target).name))
                    self.context = self.fixture.context(self.project)
                    observed.redirect = protocol_redirect(self.fixture, self.fake)
                    first = self.resolve()
                    self.assert_usable(first)
                    path = self.fixture.root / target
                    original = path.read_bytes()
                    path.write_bytes(original + b"changed")
                    before = len(observed.calls)
                    changed = self.resolve()
                    self.assertTrue(changed.problems, changed)
                    self.assertEqual(len(observed.calls), before)
                    path.write_bytes(original)
                    restored = self.resolve()
                    self.assert_usable(restored)
                    self.assertEqual(
                        len(observed.calls),
                        before + 1,
                        "Later independent validation must not retain stale version cache",
                    )

    def test_atomic_same_size_same_mtime_replacement_changes_file_identity(self) -> None:
        with self.observer() as observed:
            first = self.resolve()
            self.assert_usable(first)
            path = self.fixture.root / RESOURCE
            stat = path.stat()
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(replacement, path)
            before = len(observed.calls)
            changed = self.resolve()
            self.assertTrue(
                changed.problems, "Metadata-equivalent new file identity invalidates receipt"
            )
            self.assertEqual(len(observed.calls), before)
            self.assert_usable(self.resolve())
            self.assertEqual(len(observed.calls), before + 1)

    def test_status_reports_bound_hashes_and_data_without_lsp(self) -> None:
        with self.observer() as observed:
            service = self.service(observed)
            status = service.status()
            service.shutdown()
            write_json(self.base / "status.json", status)
            self.assertEqual(status["status"], "ok", status)
            data = status["data"]
            for field, expected in {
                "runtime_manifest_sha256": digest(self.fixture.manifest_path),
                "analysis_mapping_sha256": digest(self.fixture.mapping_path),
                "executable_sha256": digest(self.fixture.root / EXECUTABLE),
            }.items():
                self.assertEqual(data[field], expected)
            self.assertEqual(Path(data["runtime_root"]).resolve(), self.fixture.root)
            self.assertEqual(data["executable_origin"], "managed_runtime")
            self.assertEqual(
                {Path(p).resolve() for p in data["data_paths"]},
                {self.fixture.root / "analysis/clangd/lib/clang/23"},
            )
            self.assertEqual(data["session_state"], "not_started")
            self.assertTrue(all(argv[1:] == ["--version"] for argv in observed.calls))
            self.assertEqual(self.fake.starts(), [])

    def test_runtime_mutation_during_query_cancels_drains_and_never_returns_old_success(
        self,
    ) -> None:
        self.fake.configure(
            responses={
                "textDocument/hover": {
                    "gate": str(self.fake.gate("hover")),
                    "result": {"contents": "old success"},
                }
            }
        )
        with self.observer() as observed:
            service = self.service(observed)
            call = Call(
                service.hover, file_path=str(self.source), line=2, column=25, timeout_seconds=15
            )
            try:
                # If preparation cannot enter LSP, preserve its actual RED cause.
                deadline = time.monotonic() + 5
                while not self.fake.received("textDocument/hover") and time.monotonic() < deadline:
                    if call.finished is not None:
                        break
                    time.sleep(0.02)
                self.assertTrue(
                    self.fake.received("textDocument/hover"),
                    call.join(1) if call.finished is not None else "query never reached barrier",
                )
                path = self.fixture.root / RESOURCE
                path.write_bytes(path.read_bytes() + b"changed during admitted query")
                self.fake.open_gate("hover")
                result = call.join(10)
                write_json(self.base / "mutation-result.json", result)
                self.assertEqual(result["status"], "incomplete", result)
                self.assertEqual(result["code"], "analysis/configuration-changed", result)
                observed.check_cleanup()
                self.assertEqual(
                    len(self.fake.starts()), 1, "No retry may hide changed runtime bytes"
                )
            finally:
                self.fake.open_gate("hover")
                service.shutdown()


class SharedDeadline(ManagedCase):
    def test_expired_caller_never_spawns_and_later_independent_call_can_probe(self) -> None:
        with self.observer() as observed:
            with self.assertRaises(TimeoutError):
                self.resolve(budget=0)
            self.assertEqual(observed.calls, [])
            self.assert_usable(self.resolve())
            self.assertEqual(len(observed.calls), 1)

    def test_started_probe_owns_five_second_cap_and_cached_failure_without_lsp(self) -> None:
        self.fake.configure(version={"sleep": 60})
        with self.observer() as observed:
            started = time.monotonic()
            first = self.resolve(budget=30)
            elapsed = time.monotonic() - started
            second = self.resolve(budget=30)
            for result in (first, second):
                self.assertTrue(result.problems, result)
                self.assertIn("5", result.problems[0].message)
            self.assertGreaterEqual(elapsed, 4.5, "Own cap must be an actual started probe")
            self.assertLess(elapsed, 8.5)
            self.assertEqual(len(observed.calls), 1)
            self.assertEqual(self.fake.starts(), [])

    def test_caller_probe_expiry_is_transient_and_does_not_poison_next_call(self) -> None:
        self.fake.configure(version={"sleep": 60})
        with self.observer() as observed:
            with self.assertRaises(TimeoutError):
                self.resolve(budget=0.5)
            self.assertEqual(len(observed.calls), 1)
            self.fake.configure(version={})
            self.assert_usable(self.resolve())
            self.assertEqual(len(observed.calls), 2)

    def test_cancellation_while_hashing_is_transient_without_spawn(self) -> None:
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        operation = SimpleNamespace(cancellation_requested=cancelled)
        original = Path.open

        def barrier(path, *args, **kwargs):
            if path == self.fixture.root / EXECUTABLE:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Hashing barrier was not released")
            return original(path, *args, **kwargs)

        with (
            self.observer() as observed,
            mock.patch.object(self.runtime, "current_operation", return_value=operation),
        ):
            with mock.patch.object(Path, "open", barrier):
                call = Call(self.resolve)
                try:
                    self.assertTrue(
                        entered.wait(2),
                        "Resolver never reached executable read; mapping unresolved",
                    )
                    cancelled.set()
                finally:
                    release.set()
                with self.assertRaises(TimeoutError):
                    call.join(5)
            self.assertEqual(observed.calls, [])
            cancelled.clear()
            self.assert_usable(self.resolve())

    def test_cancelled_waiter_does_not_cancel_shared_probe_or_cache_failure(self) -> None:
        entered, release, cancel_waiter = threading.Event(), threading.Event(), threading.Event()
        caller_local = threading.local()
        waiting_on_probe = threading.Event()
        real_lock = self.runtime._probe_lock

        class ObservedLock:
            def acquire(self, **kwargs):
                if hasattr(caller_local, "cancellation"):
                    waiting_on_probe.set()
                return real_lock.acquire(**kwargs)

            def release(self):
                real_lock.release()

        def operation():
            return (
                SimpleNamespace(cancellation_requested=caller_local.cancellation)
                if hasattr(caller_local, "cancellation")
                else None
            )

        with (
            self.observer() as observed,
            mock.patch.object(self.runtime, "current_operation", operation),
        ):
            original = observed.spawn

            def barrier_spawn(argv, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5), "Spawn barrier not released")
                return original(argv, **kwargs)

            with (
                mock.patch.object(self.runtime, "popen_owned", barrier_spawn),
                mock.patch.object(self.runtime, "_probe_lock", ObservedLock()),
            ):
                first = Call(self.resolve)
                try:
                    self.assertTrue(entered.wait(2), "Valid mapping never admitted a probe")

                    def waiting():
                        caller_local.cancellation = cancel_waiter
                        return self.resolve()

                    second = Call(waiting)
                    self.assertTrue(
                        waiting_on_probe.wait(2), "Second caller never reached the held probe lock"
                    )
                    cancel_waiter.set()
                    with self.assertRaises(TimeoutError):
                        second.join(3)
                finally:
                    release.set()
                self.assert_usable(first.join(5))
            self.assert_usable(self.resolve())
            self.assertEqual(len(observed.calls), 1)

    def test_late_spawn_after_caller_cancellation_is_owned_and_reaped(self) -> None:
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        operation = SimpleNamespace(cancellation_requested=cancelled)
        with (
            self.observer() as observed,
            mock.patch.object(self.runtime, "current_operation", return_value=operation),
        ):
            original = observed.spawn

            def delayed(argv, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return original(argv, **kwargs)

            with mock.patch.object(self.runtime, "popen_owned", delayed):
                call = Call(self.resolve)
                try:
                    self.assertTrue(entered.wait(2), "No probe admitted")
                    cancelled.set()
                    with self.assertRaises(TimeoutError):
                        call.join(3)
                finally:
                    release.set()
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if observed.children and all(
                        p.poll() is not None for p, _, _ in observed.children
                    ):
                        break
                    time.sleep(0.02)
                self.assertEqual(
                    len(observed.children), 1, "Late actual owned spawn must be observed"
                )
                observed.check_cleanup()
            cancelled.clear()
            self.assert_usable(self.resolve())
            self.assertEqual(len(observed.calls), 2)


if __name__ == "__main__":
    unittest.main()
