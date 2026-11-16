"""Portable validation contracts, never executable/analyzer distribution proof.

Every target header is exercised through production receipts on every host.
Matching-host protocol/process execution is covered separately in Actions.
"""

from __future__ import annotations

import contextlib
import os
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


class ManagedChildEnvironment(ReceiptCase):

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

                def cleanup(process):
                    # Synthetic completed cleanup for the mocked spawn, never native proof.
                    if os.name == "posix":
                        process.cleanup_receipt = {
                            "coverage": "retained_group_and_captured_descendants",
                            "scope_confirmed": True,
                            "unobserved_detached_descendants": "not_proven",
                            "leader_reaped": True,
                            "leader_exit": process.returncode,
                            "uncertainties": [],
                            "wait_error": None,
                        }
                    return True

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
                    mock.patch.object(self.runtime, "terminate_process_group", side_effect=cleanup),
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
