"""Owner checks for accepted native mapping and selected-payload receipts.

Synthetic inventories are test fixtures, never release provenance. Only the owned
version-spawn seam is replaced; all mapping, inventory and filesystem work is real.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
import threading
import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from pyocd_debug_mcp import code_analysis_runtime as runtime
from tests.code_analysis_managed_support import RuntimeFixture, host_target


def staged_runtime(root: Path) -> dict:
    return RuntimeFixture(root).mapping


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="managed runtime ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mapping = staged_runtime(self.root)
        self.executable = self.mapping["programs"]["clangd"]["executable"]
        self.context = runtime.AnalysisRuntimeContext(self.root, self.root, managed=True)
        from pyocd_debug_mcp import code_analysis_manifest as manifest

        self.manifest = manifest
        self.enterContext(patch.object(manifest, "_metadata_cache", {}))
        self.enterContext(patch.object(manifest, "_receipt_cache", {}))
        self.enterContext(patch.object(runtime, "_probe_cache", {}))
        process = Mock(returncode=0, stdout=None, stderr=None)
        process.communicate.return_value = (b"clangd version 23.1.0\r\n", b"")
        self.process = process
        self.spawn = self.enterContext(
            patch.object(runtime, "spawn_before_deadline", return_value=(process, None))
        )
        self.enterContext(patch.object(runtime, "terminate_process_group", return_value=True))
        self.path = self.enterContext(patch.object(runtime, "_which_path_only", return_value=None))

    def resolve(self, name="clangd", budget=5):
        return runtime.resolve_analysis_program(self.context, name, time.monotonic() + budget)

    def rewrite_inventory(self, relative):
        path = self.root / "release-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        data = (self.root / relative).read_bytes()
        entry = next(f for f in manifest["files"] if f["path"] == relative)
        entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def rewrite_mapping(self):
        (self.root / "analysis/runtime.json").write_text(json.dumps(self.mapping), encoding="utf-8")
        self.rewrite_inventory("analysis/runtime.json")

    def test_valid_managed_without_path_probes_once_and_reports_bound_identity(self):
        first = self.resolve()
        second = self.resolve()
        self.assertFalse(first.problems, first)
        self.assertEqual(first.origin, "managed_runtime")
        self.assertEqual(first.version, "clangd version 23.1.0\r\n")
        self.assertEqual(first.executable, self.root / self.executable)
        self.assertEqual(first.data_paths, (self.root / "analysis/clangd/lib/clang/23",))
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(
            first.executable_sha256,
            hashlib.sha256((self.root / self.executable).read_bytes()).hexdigest(),
        )
        self.assertEqual(len(first.runtime_manifest_sha256), 64)
        self.assertEqual(len(first.analysis_mapping_sha256), 64)
        self.spawn.assert_called_once()
        self.path.assert_not_called()

    def test_missing_or_corrupt_resource_fails_before_probe(self):
        header = self.root / "analysis/clangd/lib/clang/23/include/stddef.h"
        for mutation in (lambda: header.unlink(), lambda: header.write_bytes(b"wrong")):
            with self.subTest(mutation=mutation):
                mutation()
                result = self.resolve()
                self.assertTrue(result.problems)
                self.assertIn("stddef.h", result.problems[0].message)
                self.spawn.assert_not_called()
                self.path.assert_not_called()

    def test_canonical_sbom_metadata_kind_is_required_before_probe(self):
        manifest_path = self.root / "release-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        entry = next(f for f in manifest["files"] if f["path"] == "sbom.cdx.json")
        self.assertEqual(entry["kind"], "metadata")
        self.assertIs(entry["executable"], False)
        self.assertFalse(self.resolve().problems)
        self.spawn.assert_called_once()
        self.spawn.reset_mock()
        # A fresh root must fail classification itself, rather than merely
        # invalidate the positive control's already cached manifest receipt.
        invalid = RuntimeFixture(self.root / "wrong-kind")
        invalid.inventory_entry("sbom.cdx.json")["kind"] = "sbom"
        invalid.save_manifest()
        result = runtime.resolve_analysis_program(
            invalid.context(self.root), "clangd", time.monotonic() + 5
        )
        self.assertTrue(result.problems, result)
        self.assertIn("sbom.cdx.json", result.problems[0].message)
        self.spawn.assert_not_called()
        self.path.assert_not_called()

    def test_complete_mapping_is_strict_even_when_selecting_other_program(self):
        cases = [
            ("bool-schema", lambda m: m.update(schema_version=True)),
            ("float-schema", lambda m: m.update(schema_version=1.0)),
            ("missing-program", lambda m: m["programs"].pop("cppcheck")),
            ("unknown", lambda m: m["programs"]["cppcheck"].update(command="anything")),
            (
                "resource-major",
                lambda m: m["programs"]["clangd"].update(
                    resource_dir="analysis/clangd/lib/clang/22"
                ),
            ),
            ("numeric-version", lambda m: m["programs"]["cppcheck"].update(version="2.22.0\n")),
            ("notice", lambda m: m["programs"]["cppcheck"]["licenses"].pop()),
            ("dependencies-null", lambda m: m["programs"]["clangd"].update(dependencies=None)),
            (
                "dependency-lf",
                lambda m: m["programs"]["clangd"].update(
                    dependencies=["analysis/clangd/bin/test.dll\n"]
                ),
            ),
            (
                "reserved",
                lambda m: m["programs"]["clangd"]["licenses"].append(
                    "analysis/clangd/licenses/CON.txt"
                ),
            ),
            (
                "escaped",
                lambda m: m["programs"]["clangd"]["licenses"].append(
                    "analysis/clangd/licenses/../escape"
                ),
            ),
        ]
        for label, mutate in cases:
            with self.subTest(case=label):
                self.mapping = staged_runtime(self.root)
                mutate(self.mapping)
                self.rewrite_mapping()
                self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_duplicate_and_nonfinite_json_rejected_before_probe(self):
        mapping = self.root / "analysis/runtime.json"
        original = mapping.read_bytes()
        for token in (
            b'"schema_version": true',
            b'"schema_version": NaN',
            b'"schema_version": Infinity',
            b'"schema_version": 1e999',
        ):
            with self.subTest(token=token):
                mapping.write_bytes(original.replace(b'"schema_version": 1', token))
                self.rewrite_inventory("analysis/runtime.json")
                self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_reparse_metadata_is_rejected_without_traversal(self):
        path = self.root / "analysis/clangd/lib/clang/23/include"
        original = Path.lstat

        def reparse(candidate):
            value = original(candidate)
            if candidate == path:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value

        with patch.object(Path, "lstat", reparse):
            result = self.resolve()
        self.assertTrue(result.problems)
        self.assertIn("reparse", result.problems[0].message)
        self.spawn.assert_not_called()

    def test_large_file_hash_checks_actual_deadline_between_chunks(self):
        path = self.root / "chunked-program.exe"
        path.write_bytes(b"x" * (3 * 64 * 1024))
        original = Path.open
        reads = []

        class SlowStream:
            def __enter__(self):
                self.stream = original(path, "rb")
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self, size):
                reads.append(size)
                data = self.stream.read(size)
                time.sleep(0.03)
                return data

        with patch.object(Path, "open", return_value=SlowStream()):
            with self.assertRaises(TimeoutError):
                self.manifest.hash_file(path, time.monotonic() + 0.02, None)
        self.assertEqual(reads, [64 * 1024])

    def test_duplicate_keys_and_empty_extra_directories_rejected(self):
        mapping = self.root / "analysis/runtime.json"
        original = mapping.read_bytes()
        mapping.write_bytes(original[:-1] + b',"schema_version":1}')
        self.rewrite_inventory("analysis/runtime.json")
        self.assertTrue(self.resolve().problems)
        mapping.write_bytes(original)
        self.rewrite_inventory("analysis/runtime.json")
        (self.root / "analysis/clangd/unmapped-empty").mkdir()
        self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_selected_cppcheck_requires_cfg_platforms_notices_and_numeric_probe(self):
        self.process.communicate.return_value = (b"Cppcheck 2.22\r\n", b"")
        result = self.resolve("cppcheck")
        self.assertFalse(result.problems, result)
        self.assertEqual(
            result.data_paths,
            (self.root / "analysis/cppcheck/cfg", self.root / "analysis/cppcheck/platforms"),
        )
        self.assertEqual(result.version, "Cppcheck 2.22\r\n")
        self.spawn.reset_mock()
        (self.root / "analysis/cppcheck/cfg/std.cfg").unlink()
        changed = self.resolve("cppcheck")
        self.assertTrue(changed.problems)
        self.spawn.assert_not_called()

    def test_inventory_case_collisions_reserved_and_wrong_types_rejected(self):
        path = self.root / "release-manifest.json"
        original = path.read_bytes()
        for label in ("case", "reserved", "bool-size", "extra-field", "bad-hash", "wrong-kind"):
            with self.subTest(case=label):
                manifest = json.loads(original)
                entry = next(f for f in manifest["files"] if f["path"].endswith("stddef.h"))
                if label == "case":
                    manifest["files"].append(
                        {**entry, "path": entry["path"].replace("include", "Include")}
                    )
                elif label == "reserved":
                    entry["path"] = "analysis/clangd/licenses/NUL.txt"
                elif label == "bool-size":
                    entry["size"] = True
                elif label == "extra-field":
                    entry["extra"] = 1
                elif label == "bad-hash":
                    entry["sha256"] = "a" * 64 + "\n"
                else:
                    entry["kind"] = "license"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_extra_uninventoried_payload_and_missing_dependency_fail_before_probe(self):
        extra = self.root / "analysis/clangd/bin/extra.dll"
        extra.write_bytes(b"extra")
        self.assertTrue(self.resolve().problems)
        extra.unlink()
        self.mapping["programs"]["clangd"]["dependencies"] = ["analysis/clangd/bin/needed.dll"]
        self.rewrite_mapping()
        self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_sbom_application_licenses_versions_relationships_and_hashes_checked(self):
        path = self.root / "sbom.cdx.json"
        original = path.read_bytes()
        for label in (
            "name",
            "version",
            "licenses",
            "missing-provenance",
            "empty-provenance",
            "typed-provenance",
            "duplicate-provenance",
            "missing-leaf",
            "dangling",
            "hash",
        ):
            with self.subTest(case=label):
                sbom = json.loads(original)
                app = next(c for c in sbom["components"] if c["bom-ref"] == "byo-analysis:clangd")
                relation = next(d for d in sbom["dependencies"] if d["ref"] == app["bom-ref"])
                if label in {"name", "version"}:
                    app[label] = "wrong"
                elif label == "licenses":
                    app["licenses"] = []
                elif label == "missing-provenance":
                    app.pop("properties")
                elif label == "empty-provenance":
                    app["properties"][0]["value"] = " "
                elif label == "typed-provenance":
                    app["properties"][0]["value"] = 1
                elif label == "duplicate-provenance":
                    app["properties"].append(app["properties"][0])
                elif label == "missing-leaf":
                    relation["dependsOn"].pop()
                elif label == "dangling":
                    relation["dependsOn"].append("missing-component")
                else:
                    leaf = next(
                        c for c in sbom["components"] if c.get("name", "").endswith("stddef.h")
                    )
                    leaf["hashes"] = [{"alg": "SHA-256", "content": "a" * 64}]
                path.write_text(json.dumps(sbom), encoding="utf-8")
                self.rewrite_inventory("sbom.cdx.json")
                self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_selected_executable_requires_amd64_pe32_plus_before_probe(self):
        if host_target() != "windows-x86_64":
            self.skipTest(
                "Original Windows PE input checks; neutral format cases are in portability"
            )
        path = self.root / self.executable
        original = path.read_bytes()
        offset = int.from_bytes(original[60:64], "little")
        for position, replacement in (
            (0, b"NO"),
            (offset + 4, b"\x64\xaa"),
            (offset + 24, b"\x0b\x01"),
        ):
            with self.subTest(position=position):
                data = bytearray(original)
                data[position : position + 2] = replacement
                path.write_bytes(data)
                self.rewrite_inventory(self.executable)
                self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_selected_payload_and_metadata_hash_once_and_unrelated_payload_not_read(self):
        with patch.object(self.manifest, "hash_file", wraps=self.manifest.hash_file) as hashing:
            self.assertFalse(self.resolve().problems)
            first = [c.args[0].relative_to(self.root).as_posix() for c in hashing.call_args_list]
            self.assertEqual(len(first), len(set(first)))
            self.assertFalse(any(p.startswith("analysis/cppcheck/") for p in first))
            self.assertFalse(self.resolve().problems)
            self.assertEqual(hashing.call_count, len(first))
            self.process.communicate.return_value = (b"Cppcheck 2.22", b"")
            self.assertFalse(self.resolve("cppcheck").problems)
            all_paths = [c.args[0] for c in hashing.call_args_list]
            for metadata in ("release-manifest.json", "analysis/runtime.json", "sbom.cdx.json"):
                self.assertEqual(all_paths.count(self.root / metadata), 1)

    def test_change_invalidates_caches_this_call_and_revalidates_only_next_call(self):
        first = self.resolve()
        header = self.root / "analysis/clangd/lib/clang/23/include/stddef.h"
        original = header.read_bytes()
        header.write_bytes(b"x" * len(original))
        with patch.object(self.manifest, "hash_file", wraps=self.manifest.hash_file) as hashing:
            changed = self.resolve()
            self.assertTrue(changed.problems)
            self.assertTrue(changed.expected_identity)
            self.assertTrue(changed.observed_identity)
            hashing.assert_not_called()
            self.assertFalse(runtime._probe_cache)
        header.write_bytes(original)
        restored = self.resolve()
        self.assertFalse(restored.problems)
        self.assertNotEqual(first.identity, restored.identity)
        self.assertEqual(self.spawn.call_count, 2)

    def test_directory_change_reenumerates_and_prevents_probe(self):
        self.resolve()
        extra = self.root / "analysis/clangd/lib/clang/23/include/extra.h"
        extra.write_bytes(b"extra")
        with patch.object(self.manifest, "_scan", wraps=self.manifest._scan) as scan:
            result = self.resolve()
            self.assertTrue(result.problems)
            scan.assert_called()
        self.assertEqual(self.spawn.call_count, 1)

    def test_metadata_change_invalidates_receipt_and_version_cache_before_probe(self):
        self.resolve()
        mapping = self.root / "analysis/runtime.json"
        mapping.write_bytes(mapping.read_bytes() + b" ")
        self.assertTrue(self.resolve().problems)
        self.assertFalse(runtime._probe_cache)
        self.assertEqual(self.spawn.call_count, 1)

    def test_probe_numeric_mismatch_retains_raw_stdout_and_caches_failure(self):
        self.process.communicate.return_value = (b"clangd version 22.1.0\r\n", b"")
        result = self.resolve()
        self.assertTrue(result.problems)
        self.assertIn("Version mismatch", result.problems[0].message)
        self.assertEqual(result.version, "clangd version 22.1.0\r\n")
        self.assertTrue(self.resolve().problems)
        self.spawn.assert_called_once()

    def test_managed_own_cap_classification_survives_bounded_cleanup_grace(self):
        def hung(**kwargs):
            time.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired("unit owned probe", kwargs["timeout"])

        def cleanup(process):
            time.sleep(1.1)
            return True

        self.process.communicate.side_effect = hung
        with patch.object(runtime, "terminate_process_group", side_effect=cleanup):
            result = self.resolve(budget=6)
        self.assertTrue(result.problems)
        self.assertIn("5-second", result.problems[0].message)
        again = self.resolve()
        self.assertEqual(result.problems, again.problems)
        self.spawn.assert_called_once()

    def test_mutation_during_probe_cannot_return_success(self):
        header = self.root / "analysis/clangd/lib/clang/23/include/stddef.h"

        def communicate(**kwargs):
            header.write_bytes(b"changed during probe")
            return b"clangd version 23.1.0", b""

        self.process.communicate.side_effect = communicate
        result = self.resolve()
        self.assertTrue(result.problems)
        self.assertFalse(runtime._probe_cache)

    def test_managed_failure_never_path_and_explicit_unmanaged_fallback_disclosed(self):
        (self.root / "analysis/clangd/lib/clang/23/include/stddef.h").unlink()
        self.assertTrue(self.resolve().problems)
        self.path.assert_not_called()
        self.context = runtime.AnalysisRuntimeContext(self.root, self.root, managed=False)
        self.path.return_value = str(self.root / self.executable)
        result = self.resolve()
        self.assertFalse(result.problems, result)
        self.assertEqual(result.origin, "path")
        self.assertTrue(result.runtime_problems)
        self.assertTrue(result.warnings)

    def test_one_deadline_bounds_hashing_and_lock_wait_without_probe_or_cached_failure(self):
        for seam in ("hash", "lock"):
            with self.subTest(seam=seam):
                if seam == "hash":
                    original = self.manifest.remaining
                    calls = 0

                    def expire(deadline, cancellation=None):
                        nonlocal calls
                        calls += 1
                        if calls == 10:
                            raise TimeoutError("test caller expiry during hashing")
                        return original(deadline, cancellation)

                    with patch.object(self.manifest, "remaining", side_effect=expire):
                        with self.assertRaises(TimeoutError):
                            self.resolve()
                else:
                    lock = threading.Lock()
                    lock.acquire()
                    with patch.object(self.manifest, "_lock", lock):
                        with self.assertRaises(TimeoutError):
                            self.resolve(budget=0.01)
                self.spawn.assert_not_called()
                self.assertFalse(runtime._probe_cache)
        self.assertFalse(self.resolve().problems)

    def test_actual_cancellation_checked_before_mapping_io(self):
        cancellation = threading.Event()
        cancellation.set()
        operation = Mock(cancellation_requested=cancellation)
        with patch.object(runtime, "current_operation", return_value=operation):
            with self.assertRaises(TimeoutError):
                self.resolve()
        self.spawn.assert_not_called()

    def test_host_and_release_architecture_agreement(self):
        with patch.object(
            self.manifest.platform,
            "machine",
            return_value="x86_64" if self.mapping["architecture"] == "aarch64" else "ARM64",
        ):
            self.assertTrue(self.resolve().problems)
        path = self.root / "release-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["architecture"] = (
            "x86_64" if self.mapping["architecture"] == "aarch64" else "aarch64"
        )
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()

    def test_unreadable_data_rejected_before_probe(self):
        original = Path.open
        header = self.root / "analysis/clangd/lib/clang/23/include/stddef.h"

        def unreadable(path, *args, **kwargs):
            if path == header:
                raise PermissionError("unit unreadable data")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", unreadable):
            self.assertTrue(self.resolve().problems)
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
