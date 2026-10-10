"""Portable validation contracts, never executable/analyzer distribution proof.

Every target header is exercised through production receipts on every host.
Matching-host protocol/process execution is covered separately in Actions.
"""

from __future__ import annotations

import contextlib
import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.code_analysis_managed_support import (
    TARGETS,
    RuntimeFixture,
    host_target,
    native_header,
    production,
)


class ReceiptCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="portable receipts ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.manifest = production("code_analysis_manifest")
        self.runtime = production("code_analysis_runtime")
        self.enterContext(mock.patch.object(self.manifest, "_metadata_cache", {}))
        self.enterContext(mock.patch.object(self.manifest, "_receipt_cache", {}))
        self.enterContext(mock.patch.object(self.runtime, "_probe_cache", {}))
        self.sequence = 0

    def fixture(self, target: str) -> RuntimeFixture:
        self.sequence += 1
        return RuntimeFixture(self.base / str(self.sequence), target=target, synthetic_headers=True)

    @contextlib.contextmanager
    def host(self, target: str, machine: str | None = None):
        _, _, system, native_machine = TARGETS[target]
        # Host inputs are public platform observations; never replace validation.
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("platform.system", return_value=system))
            stack.enter_context(
                mock.patch("platform.machine", return_value=machine or native_machine)
            )
            if os.name == "nt" and not target.startswith("windows"):
                # Windows cannot store POSIX execute bits on suffixless fixtures.
                # Supply only that synthetic file-mode observation for cross-target
                # validation. Actual mode/access failures run on matching POSIX.
                original = Path.stat

                def fixture_mode(path, *args, **kwargs):
                    info = original(path, *args, **kwargs)
                    if path.as_posix().endswith(
                        ("analysis/cppcheck/cppcheck", "analysis/clangd/bin/clangd")
                    ):
                        values = {
                            key: getattr(info, key) for key in dir(info) if key.startswith("st_")
                        }
                        values["st_mode"] |= 0o111
                        return SimpleNamespace(**values)
                    return info

                stack.enter_context(mock.patch.object(Path, "stat", fixture_mode))
            yield

    def receipt(self, fixture: RuntimeFixture, name: str = "clangd"):
        return self.manifest.payload_receipt(fixture.root, name, time.monotonic() + 10, None)

    def reject(self, fixture: RuntimeFixture, name: str = "clangd") -> None:
        with (
            mock.patch.object(self.runtime, "spawn_before_deadline") as spawn,
            mock.patch.object(
                self.runtime, "_which_path_only", return_value="untrusted-path"
            ) as path,
        ):
            result = self.runtime.resolve_analysis_program(
                fixture.context(self.base), name, time.monotonic() + 10
            )
        self.assertTrue(result.problems, result)
        self.assertEqual(result.problems[0].code, "analysis/executable-unavailable")
        self.assertNotEqual(result.origin, "path")
        spawn.assert_not_called()
        path.assert_not_called()


class PortableReceipts(ReceiptCase):
    def test_release_closed_objects_integer_tokens_and_inventory_digest_types(self) -> None:
        for target in TARGETS:
            with self.subTest(target=target), self.host(target):
                self.receipt(self.fixture(target))
                for case in (
                    "unknown-release",
                    "missing-release",
                    "bool-protocol",
                    "float-protocol",
                    "source-extra",
                    "source-commit-type",
                    "toolchain-extra",
                    "inventory-extra",
                    "inventory-missing",
                    "digest-upper",
                    "digest-null",
                    "size-bool",
                    "size-float",
                    "flag-integer",
                    "duplicate-leaf",
                    "duplicate-json-key",
                ):
                    with self.subTest(case=case):
                        fixture = self.fixture(target)
                        manifest = fixture.manifest
                        entry = fixture.inventory_entry("analysis/clangd/LICENSE.TXT")
                        if case == "unknown-release":
                            manifest["target"] = target
                        elif case == "missing-release":
                            del manifest["channel"]
                        elif case in ("bool-protocol", "float-protocol"):
                            manifest["sidecar_protocol"] = True if case == "bool-protocol" else 1.0
                        elif case == "source-extra":
                            manifest["source"]["firmware_mcp"]["branch"] = "mutable"
                        elif case == "source-commit-type":
                            manifest["source"]["firmware_mcp"]["commit"] = None
                        elif case == "toolchain-extra":
                            manifest["toolchain"]["compiler"] = "extra"
                        elif case == "inventory-extra":
                            entry["mode"] = 0o755
                        elif case == "inventory-missing":
                            del entry["kind"]
                        elif case == "digest-upper":
                            entry["sha256"] = "A" * 64
                        elif case == "digest-null":
                            entry["sha256"] = None
                        elif case in ("size-bool", "size-float"):
                            entry["size"] = True if case == "size-bool" else 1.0
                        elif case == "flag-integer":
                            entry["executable"] = 0
                        elif case == "duplicate-leaf":
                            manifest["files"].append(dict(entry))
                        fixture.save_manifest()
                        if case == "duplicate-json-key":
                            raw = json.dumps(manifest).replace(
                                '"schema": 1', '"schema": 1, "schema": 1'
                            )
                            fixture.manifest_path.write_text(raw, encoding="utf-8")
                        self.reject(fixture)

    def test_four_targets_and_native_host_aliases_accept_both_selected_namespaces(self) -> None:
        aliases = {
            "windows-x86_64": ("AMD64", "x86_64"),
            "macos-aarch64": ("arm64", "aarch64"),
            "macos-x86_64": ("x86_64", "AMD64"),
            "linux-x86_64": ("x86_64", "AMD64"),
        }
        for target, machines in aliases.items():
            for machine in machines:
                with self.subTest(target=target, machine=machine), self.host(target, machine):
                    fixture = self.fixture(target)
                    for name in ("cppcheck", "clangd"):
                        receipt = self.receipt(fixture, name)
                        self.assertEqual(
                            receipt.executable,
                            fixture.root / fixture.mapping["programs"][name]["executable"],
                        )
                        self.manifest.validate_receipt(receipt, time.monotonic() + 10, None)

    def test_mapping_release_host_disagreement_and_unsupported_targets_fail_closed(self) -> None:
        for target in TARGETS:
            with self.subTest(target=target), self.host(target):
                self.receipt(self.fixture(target))
                for other in TARGETS:
                    if other == target:
                        continue
                    for where in ("mapping", "release", "host"):
                        with self.subTest(other=other, where=where):
                            fixture = self.fixture(target)
                            system, architecture, _, _ = TARGETS[other]
                            if where == "mapping":
                                fixture.mapping.update(platform=system, architecture=architecture)
                                fixture.replace_mapping(fixture.mapping)
                            elif where == "release":
                                fixture.manifest.update(platform=system, architecture=architecture)
                                fixture.save_manifest()
                            if where == "host":
                                with self.host(other):
                                    self.reject(fixture)
                            else:
                                self.reject(fixture)
                for system, architecture in (
                    ("linux", "aarch64"),
                    ("windows", "aarch64"),
                    ("freebsd", "x86_64"),
                    ("macos", "universal"),
                ):
                    fixture = self.fixture(target)
                    fixture.mapping.update(platform=system, architecture=architecture)
                    fixture.replace_mapping(fixture.mapping)
                    self.reject(fixture)

    def test_target_native_headers_reject_foreign_truncated_scripts_and_wrappers(self) -> None:
        for target in TARGETS:
            with self.subTest(target=target), self.host(target):
                self.receipt(self.fixture(target))
                invalid = [
                    b"",
                    b"#!/bin/sh\nexit 0\n",
                    b"@echo off\r\n",
                    native_header(target)[:15],
                ]
                invalid.append(
                    native_header(target)[
                        : 153
                        if target.startswith("windows")
                        else 31
                        if target.startswith("macos")
                        else 63
                    ]
                )
                invalid.extend(native_header(other) for other in TARGETS if other != target)
                for payload in invalid:
                    with self.subTest(magic=payload[:16]):
                        fixture = self.fixture(target)
                        fixture.put(fixture.mapping["programs"]["clangd"]["executable"], payload)
                        fixture.seal()
                        self.reject(fixture)

    def test_thin_mach64_cpu_filetype_magic_and_complete_header(self) -> None:
        for target in ("macos-aarch64", "macos-x86_64"):
            with self.subTest(target=target), self.host(target):
                self.receipt(self.fixture(target))
                original = native_header(target)
                cases = [
                    (0, 0xFEEDFACE),
                    (0, 0xCAFEBABE),
                    (0, 0xBEBAFECA),
                    (0, 0xCAFEBABF),
                    (0, 0xBFBAFECA),
                    (4, 12),
                    (4, 7),
                    (12, 1),
                    (12, 6),
                ]
                for offset, value in cases:
                    fixture = self.fixture(target)
                    payload = bytearray(original)
                    struct.pack_into("<I", payload, offset, value)
                    fixture.put(fixture.mapping["programs"]["clangd"]["executable"], bytes(payload))
                    fixture.seal()
                    self.reject(fixture)
                fixture = self.fixture(target)
                fixture.put(fixture.mapping["programs"]["clangd"]["executable"], original[:31])
                fixture.seal()
                self.reject(fixture)

    def test_elf64_requires_little_endian_x86_64_exec_or_pie(self) -> None:
        target = "linux-x86_64"
        with self.host(target):
            for filetype in (2, 3):
                fixture = self.fixture(target)
                payload = bytearray(native_header(target))
                struct.pack_into("<H", payload, 16, filetype)
                fixture.put(fixture.mapping["programs"]["clangd"]["executable"], bytes(payload))
                fixture.seal()
                self.receipt(fixture)
            for offset, replacement in (
                (4, b"\x01"),
                (5, b"\x02"),
                (6, b"\x00"),
                (16, b"\x01\x00"),
                (18, b"\xb7\x00"),
            ):
                fixture = self.fixture(target)
                payload = bytearray(native_header(target))
                payload[offset : offset + len(replacement)] = replacement
                fixture.put(fixture.mapping["programs"]["clangd"]["executable"], bytes(payload))
                fixture.seal()
                self.reject(fixture)

    def test_pe64_signature_machine_optional_header_and_truncation_unchanged(self) -> None:
        target = "windows-x86_64"
        with self.host(target):
            self.receipt(self.fixture(target))
            for offset, replacement in (
                (0, b"NO"),
                (60, b"\0" * 4),
                (128, b"PX\0\0"),
                (132, b"\x64\xaa"),
                (148, b"\0\0"),
                (152, b"\x0b\x01"),
            ):
                fixture = self.fixture(target)
                payload = bytearray(native_header(target))
                payload[offset : offset + len(replacement)] = replacement
                fixture.put(fixture.mapping["programs"]["clangd"]["executable"], bytes(payload))
                fixture.seal()
                self.reject(fixture)

    def test_private_libraries_require_declared_namespace_header_architecture_and_type(
        self,
    ) -> None:
        for target in TARGETS:
            with self.subTest(target=target), self.host(target):
                parent = (
                    "analysis/clangd/bin/"
                    if target.startswith("windows")
                    else "analysis/clangd/lib/"
                )
                extension = (
                    ".dll"
                    if target.startswith("windows")
                    else ".dylib"
                    if target.startswith("macos")
                    else ".so.1.2"
                )
                dependency = parent + "private" + extension
                fixture = self.fixture(target)
                fixture.mapping["programs"]["clangd"]["dependencies"] = [dependency]
                fixture.put(dependency, native_header(target, library=True))
                fixture.seal()
                self.receipt(fixture)
                for case in (
                    "undeclared",
                    "missing",
                    "wrong-header",
                    "executable-type",
                    "wrong-classification",
                    "wrong-digest",
                    "missing-sbom-owner",
                ):
                    with self.subTest(case=case):
                        if case == "executable-type" and target.startswith("windows"):
                            continue  # Windows contract validates PE64 header, not DLL characteristics.
                        fixture = self.fixture(target)
                        fixture.mapping["programs"]["clangd"]["dependencies"] = [dependency]
                        fixture.put(dependency, native_header(target, library=True))
                        if case == "undeclared":
                            fixture.mapping["programs"]["clangd"]["dependencies"] = []
                        elif case == "missing":
                            (fixture.root / dependency).unlink()
                        elif case in ("wrong-header", "executable-type"):
                            fixture.put(
                                dependency,
                                b"script" if case == "wrong-header" else native_header(target),
                            )
                        fixture.seal()
                        if case == "wrong-classification":
                            fixture.inventory_entry(dependency)["kind"] = "analysis-resource"
                            fixture.save_manifest()
                        elif case == "wrong-digest":
                            fixture.inventory_entry(dependency)["sha256"] = "0" * 64
                            fixture.save_manifest()
                        elif case == "missing-sbom-owner":
                            relation = next(
                                d
                                for d in fixture.sbom["dependencies"]
                                if d["ref"] == "byo-analysis:clangd"
                            )
                            relation["dependsOn"].remove("byo-native:" + dependency)
                            fixture.save_sbom()
                        self.reject(fixture)

    def test_selected_failure_isolated_but_mapping_target_failure_affects_both(self) -> None:
        for target in TARGETS:
            with self.subTest(target=target), self.host(target):
                fixture = self.fixture(target)
                (fixture.root / "analysis/clangd/LICENSE.TXT").unlink()
                self.receipt(fixture, "cppcheck")
                self.reject(fixture, "clangd")
                fixture = self.fixture(target)
                fixture.mapping["architecture"] = "unsupported"
                fixture.replace_mapping(fixture.mapping)
                for name in ("cppcheck", "clangd"):
                    self.reject(fixture, name)


@unittest.skipUnless(os.name == "posix", "POSIX filesystem permission/leaf contracts")
class PosixReceiptIdentity(ReceiptCase):
    def test_modes_and_caller_execute_access_fail_before_probe(self) -> None:
        fixture = self.fixture(host_target())
        self.receipt(fixture)
        path = fixture.root / fixture.mapping["programs"]["clangd"]["executable"]
        path.chmod(0o644)
        self.reject(fixture)
        fixture = self.fixture(host_target())
        executable = fixture.root / fixture.mapping["programs"]["clangd"]["executable"]
        original = os.access
        with mock.patch.object(
            os,
            "access",
            side_effect=lambda path, mode, *a, **k: (
                False
                if Path(path) == executable and mode & os.X_OK
                else original(path, mode, *a, **k)
            ),
        ):
            self.reject(fixture)

    def test_mode_high_resolution_time_and_ctime_invalidate_receipt_without_repair(self) -> None:
        for mutation in ("mode", "mtime-ns", "same-size-restored-mtime"):
            with self.subTest(mutation=mutation):
                fixture = self.fixture(host_target())
                receipt = self.receipt(fixture)
                path = fixture.root / fixture.mapping["programs"]["clangd"]["executable"]
                before = path.stat()
                if mutation == "mode":
                    path.chmod(0o700)
                elif mutation == "mtime-ns":
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000))
                    self.assertNotEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                else:
                    data = path.read_bytes()
                    # ctime resolution varies; wait for a distinct native identity.
                    deadline = time.monotonic() + 2
                    while (
                        path.stat().st_ctime_ns == before.st_ctime_ns
                        and time.monotonic() < deadline
                    ):
                        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    self.assertNotEqual(path.stat().st_ctime_ns, before.st_ctime_ns)
                    self.assertEqual(path.stat().st_size, before.st_size)
                    self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                with self.assertRaises(self.manifest.RuntimeIntegrityError) as raised:
                    self.manifest.validate_receipt(receipt, time.monotonic() + 10, None)
                self.assertTrue(raised.exception.changed)
                self.assertTrue(raised.exception.expected)
                self.assertTrue(raised.exception.observed)

    def test_symlink_hardlink_fifo_and_case_colliding_inventory_fail_closed(self) -> None:
        for mutation in ("symlink", "hardlink", "fifo", "case-collision"):
            with self.subTest(mutation=mutation):
                fixture = self.fixture(host_target())
                self.receipt(self.fixture(host_target()))
                path = fixture.root / "analysis/clangd/lib/clang/23/include/stddef.h"
                if mutation == "case-collision":
                    fixture.inventory.append(
                        dict(
                            fixture.inventory_entry(path.relative_to(fixture.root).as_posix()),
                            path="analysis/clangd/lib/clang/23/include/STDDEF.h",
                        )
                    )
                    fixture.save_manifest()
                else:
                    outside = self.base / ("outside-" + mutation)
                    outside.write_bytes(path.read_bytes())
                    path.unlink()
                    if mutation == "symlink":
                        path.symlink_to(outside)
                    elif mutation == "hardlink":
                        os.link(outside, path)
                    else:
                        os.mkfifo(path)
                self.reject(fixture)


class ManagedChildEnvironment(ReceiptCase):
    def test_runtime_selection_requires_explicit_trusted_context_not_cwd_or_environment(
        self,
    ) -> None:
        trusted = self.fixture(host_target())
        decoy = self.fixture(host_target())
        process = mock.Mock(returncode=0, stdout=None, stderr=None)
        process.communicate.return_value = (b"clangd version 23.1.0\n", b"")
        with (
            mock.patch.dict(
                os.environ,
                {"BYO_RUNTIME_ROOT": str(decoy.root), "BYO_ANALYSIS_RUNTIME_ROOT": str(decoy.root)},
            ),
            mock.patch.object(Path, "cwd", return_value=decoy.root),
            mock.patch.object(self.runtime, "_context", None),
            mock.patch.object(self.runtime, "_which_path_only", return_value=None) as path,
            mock.patch.object(
                self.runtime, "spawn_before_deadline", return_value=(process, None)
            ) as spawn,
            mock.patch.object(self.runtime, "terminate_process_group", return_value=True),
        ):
            ordinary = self.runtime.analysis_runtime_context(self.base)
            self.assertIsNone(ordinary.runtime_root)
            self.assertFalse(ordinary.managed)
            result = self.runtime.resolve_analysis_program(
                trusted.context(self.base), "clangd", time.monotonic() + 10
            )
            self.assertFalse(result.problems, result)
            self.assertEqual(result.runtime_root, trusted.root)
            self.assertEqual(
                Path(spawn.call_args.args[0][0]),
                trusted.root / trusted.mapping["programs"]["clangd"]["executable"],
            )
            path.assert_not_called()
        with self.assertRaises(ValueError):
            self.runtime.AnalysisRuntimeContext(self.base, managed=True)

    def test_probe_and_lsp_strip_loader_poison_preserve_development_and_query_driver(self) -> None:
        poison = {
            "LD_PRELOAD": "invalid.so",
            "LD_LIBRARY_PATH": "/untrusted",
            "LD_AUDIT": "audit.so",
            "DYLD_INSERT_LIBRARIES": "invalid.dylib",
            "DYLD_LIBRARY_PATH": "/untrusted",
            "DYLD_FUTURE_OVERRIDE": "future",
        }
        preserved = {"PATH": "/deliberate-development-path", "BYO_TEST_KEEP": "ordinary-setting"}
        for managed in (True, False):
            with self.subTest(managed=managed):
                fixture = self.fixture(host_target())
                context = fixture.context(self.base, managed=managed)
                if not managed:
                    context = self.runtime.AnalysisRuntimeContext(self.base)
                process = mock.Mock(returncode=0, stdout=None, stderr=None)
                process.communicate.return_value = (b"clangd version 23.1.0\n", b"")
                with (
                    mock.patch.dict(os.environ, {**poison, **preserved}),
                    mock.patch.object(
                        self.runtime,
                        "_which_path_only",
                        return_value=str(
                            fixture.root / fixture.mapping["programs"]["clangd"]["executable"]
                        ),
                    ),
                    mock.patch.object(
                        self.runtime, "spawn_before_deadline", return_value=(process, None)
                    ) as spawn,
                    mock.patch.object(self.runtime, "terminate_process_group", return_value=True),
                ):
                    program = self.runtime.resolve_analysis_program(
                        context, "clangd", time.monotonic() + 10
                    )
                    self.assertFalse(program.problems, program)
                    probe_env = spawn.call_args.kwargs.get("env")
                    self.assert_environment(probe_env, managed, poison, preserved)
                    adapter_module = production("adapters.clangd")
                    driver = self.base / "toolchain" / "arm-none-eabi-*"
                    config = SimpleNamespace(
                        project_root=self.base,
                        database_path=self.base / "compile_commands.json",
                        query_driver_paths=(driver,),
                    )
                    adapter = adapter_module.ClangdAdapter(program, config)
                    with mock.patch.object(
                        adapter_module,
                        "spawn_before_deadline",
                        side_effect=OSError("capture spawn only"),
                    ) as lsp_spawn:
                        with self.assertRaises(adapter_module.ClangdFailure):
                            adapter.start(time.monotonic() + 10)
                    self.assert_environment(
                        lsp_spawn.call_args.kwargs.get("env"), managed, poison, preserved
                    )
                    argv = lsp_spawn.call_args.args[0]
                    self.assertIn("--query-driver=" + str(driver).replace("\\", "/"), argv)
                    if managed:
                        self.assertIn(
                            "--resource-dir=" + str(fixture.root / "analysis/clangd/lib/clang/23"),
                            argv,
                        )
                    self.assertEqual({k: os.environ[k] for k in poison}, poison)

    def assert_environment(self, env, managed, poison, preserved) -> None:
        effective = dict(os.environ) if env is None else env
        for key, value in preserved.items():
            self.assertEqual(effective.get(key), value)
        for key, value in poison.items():
            if managed:
                self.assertNotIn(key, effective)
            else:
                self.assertEqual(effective.get(key), value)
