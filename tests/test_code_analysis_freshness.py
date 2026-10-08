"""Saved-revision freshness: diagnostics versions, concurrent edits, generations.

Contract: "Explicit version-matched empty notification is ok with zero counts";
old, unversioned, silent or other-file notifications are never empty success;
changed saved bytes yield ``incomplete``/source-changed; configuration changes
start a new generation.  Runs the real service/adapter against the fake peer.
"""

from __future__ import annotations

import unittest
from typing import Any

from tests.code_analysis_support import (
    ARGUMENT_TIMEOUT_GRACE_SECONDS,
    OWNED_CLEANUP_SECONDS,
    SCHEDULING_SLACK_SECONDS,
    Call,
    FakeServiceCase,
    assert_envelope,
    assert_failure,
    assert_not_clean,
    assert_source,
    query_params,
    raw_range,
    sha256_file,
    wait_pids_gone,
)

ONE_ERROR = [{"range": raw_range((2, 4), (2, 10)), "severity": 1, "message": "fresh error"}]


class DiagnosticsVersionMatching(FakeServiceCase):
    def _diagnostics(self, timeout: float = 10.0) -> dict[str, Any]:
        return self.service.diagnostics(file_path=str(self.source()), timeout_seconds=timeout)

    def test_version_matched_empty_notification_is_ok_with_zero_counts(self) -> None:
        self.fake.configure(diagnostics_mode="versioned", diagnostics=[])
        envelope = assert_envelope(self, self._diagnostics(), "diagnostics", status="ok")
        source = assert_source(self, envelope, self.source())
        self.assertEqual(envelope["data"]["diagnostics"], [])
        self.assertEqual(
            (
                envelope["data"]["observed_count"],
                envelope["data"]["returned_count"],
                envelope["data"]["truncated"],
            ),
            (0, 0, False),
        )
        published = [
            message["params"]
            for message in self.fake.sent("textDocument/publishDiagnostics")
            if message["params"].get("version") == source["document_version"]
        ]
        self.assertTrue(published, "ok must correspond to a notification for the returned version")

    def test_unversioned_stale_foreign_and_future_notifications_are_never_clean(self) -> None:
        for mode in ("unversioned", "stale", "wrong_uri", "future"):
            with self.subTest(mode=mode):
                self.service.shutdown()
                self.service = self._fresh_service()
                self.fake.configure(diagnostics_mode=mode, diagnostics=[])
                envelope = assert_envelope(self, self._diagnostics(timeout=2.0), "diagnostics")
                assert_not_clean(self, envelope)
                self.assertEqual(envelope["status"], "incomplete", envelope)
                self.assertIn(
                    envelope["code"],
                    {
                        "analysis/diagnostics-unversioned",
                        "analysis/diagnostics-missing",
                        "analysis/timeout",
                    },
                )
                if mode == "unversioned":
                    self.assertEqual(envelope["code"], "analysis/diagnostics-unversioned")

    def test_silent_server_is_bounded_and_not_clean(self) -> None:
        self.fake.configure(diagnostics_mode="silent")
        call = Call(self.service.diagnostics, file_path=str(self.source()), timeout_seconds=1.0)
        envelope = call.join(1.0 + ARGUMENT_TIMEOUT_GRACE_SECONDS + SCHEDULING_SLACK_SECONDS)
        assert_envelope(self, envelope, "diagnostics", status="incomplete")
        self.assertIn(envelope["code"], {"analysis/timeout", "analysis/diagnostics-missing"})
        self.assertLess(call.elapsed, 1.0 + SCHEDULING_SLACK_SECONDS)

    def test_unchanged_bytes_without_a_current_notification_are_forced(self) -> None:
        # The document is opened by a definition while the peer is silent, so
        # no notification for that version exists; diagnostics must force one
        # with a full-content didChange carrying top-level wantDiagnostics.
        self.fake.configure(diagnostics_mode="silent")
        opened = assert_source(self, self.definition(), self.source())["document_version"]
        self.fake.configure(diagnostics_mode="versioned", diagnostics=ONE_ERROR)
        envelope = assert_envelope(self, self._diagnostics(), "diagnostics", status="ok")
        changes = query_params(self.fake, "textDocument/didChange")
        self.assertTrue(changes, "forced diagnostics need didChange")
        forced = changes[-1]
        self.assertIs(forced.get("wantDiagnostics"), True, forced)
        self.assertEqual(
            forced["contentChanges"][-1], {"text": self.source().read_bytes().decode("utf-8")}
        )
        self.assertGreater(forced["textDocument"]["version"], opened)
        self.assertEqual(envelope["source"]["document_version"], forced["textDocument"]["version"])
        self.assertEqual(
            [item["message"] for item in envelope["data"]["diagnostics"]], ["fresh error"]
        )

    def test_header_change_invalidates_cached_diagnostics(self) -> None:
        self.fake.configure(diagnostics_sequence=[[], ONE_ERROR, ONE_ERROR])
        first = assert_envelope(self, self._diagnostics(), "diagnostics", status="ok")
        self.assertEqual(first["data"]["diagnostics"], [])
        self.project.write_text(
            "src/util.h", "#pragma once\nstatic inline int add(int a) { return a; }\n"
        )
        second = assert_envelope(self, self._diagnostics(), "diagnostics", status="ok")
        self.assertEqual(
            [item["message"] for item in second["data"]["diagnostics"]],
            ["fresh error"],
            "a dependency change must not reuse the earlier clean notification",
        )
        self.assertGreater(
            second["source"]["document_version"], first["source"]["document_version"]
        )

    def _fresh_service(self):
        from tests.code_analysis_support import make_service

        return make_service(self.project.root, fake=self.fake, testcase=self)


class ConcurrentSavedFileChanges(FakeServiceCase):
    def _change_source(self) -> bytes:
        updated = self.source().read_bytes().replace(b"value + 1", b"value + 2")
        self.source().write_bytes(updated)
        return updated

    def test_definition_racing_a_saved_change_is_source_changed(self) -> None:
        gate = self.fake.gate("definition")
        self.fake.configure(
            responses={
                "textDocument/definition": {
                    "gate": str(gate),
                    "result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))},
                }
            }
        )
        call = Call(self.definition)
        self.fake.wait_received("textDocument/definition")
        self._change_source()
        self.fake.open_gate("definition")
        envelope = call.join(20)
        assert_failure(self, envelope, "definition", "incomplete", "analysis/source-changed")
        self.assertNotEqual(envelope.get("source", {}).get("sha256"), sha256_file(self.source()))

    def test_diagnostics_racing_a_saved_change_is_source_changed(self) -> None:
        gate = self.fake.gate("diagnostics")
        self.fake.configure(diagnostics_gate=str(gate), diagnostics=[])
        call = Call(self.service.diagnostics, file_path=str(self.source()), timeout_seconds=15.0)
        self.fake.wait_received("textDocument/didOpen")
        self._change_source()
        self.fake.open_gate("diagnostics")
        envelope = call.join(25)
        assert_failure(self, envelope, "diagnostics", "incomplete", "analysis/source-changed")

    def test_same_file_calls_never_mix_revisions(self) -> None:
        gate = self.fake.gate("first")
        self.fake.configure(
            responses={
                "textDocument/definition": [
                    {
                        "gate": str(gate),
                        "result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))},
                    },
                    {"result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))}},
                ]
            }
        )
        first = Call(self.definition)
        self.fake.wait_received("textDocument/definition")
        updated = self._change_source()
        second = Call(self.definition)
        self.fake.open_gate("first")
        first_envelope = first.join(25)
        second_envelope = second.join(25)
        assert_failure(self, first_envelope, "definition", "incomplete", "analysis/source-changed")
        assert_envelope(self, second_envelope, "definition", status="ok")
        source = assert_source(self, second_envelope, self.source())
        self.assertEqual(source["sha256"], sha256_file(self.source()))
        first_version = (first_envelope.get("source") or {}).get("document_version") or 0
        self.assertGreater(source["document_version"], first_version)
        synced = [
            (params["textDocument"]["version"], params["contentChanges"][-1]["text"])
            for params in query_params(self.fake, "textDocument/didChange")
        ]
        self.assertIn((source["document_version"], updated.decode("utf-8")), synced)


class ConfigurationGenerations(FakeServiceCase):
    def test_database_change_between_calls_starts_a_new_generation(self) -> None:
        first = assert_envelope(self, self.definition(), "definition")
        old_pids = self.fake.pids()
        self.project.write_database(
            [
                self.project.entry(
                    "src/main.c",
                    arguments=[
                        "clang",
                        "-std=c99",
                        "-DGENERATION=2",
                        "-I",
                        str(self.project.path("src")),
                        "-c",
                        str(self.source()),
                    ],
                )
            ]
        )
        second = assert_envelope(self, self.definition(), "definition")
        self.assertNotEqual(
            first["compilation_database_sha256"], second["compilation_database_sha256"]
        )
        self.assertEqual(
            second["compilation_database_sha256"],
            sha256_file(self.project.path("compile_commands.json")),
        )
        self.assertEqual(len(self.fake.starts()), 2, "a configuration change needs a new session")
        self.assertEqual(
            wait_pids_gone(old_pids, ARGUMENT_TIMEOUT_GRACE_SECONDS + OWNED_CLEANUP_SECONDS),
            [],
            "the old generation must be terminated",
        )

    def test_configuration_change_during_a_call_is_configuration_changed(self) -> None:
        gate = self.fake.gate("config")
        self.fake.configure(
            responses={
                "textDocument/definition": [
                    {
                        "gate": str(gate),
                        "result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))},
                    },
                    {"result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))}},
                ]
            }
        )
        call = Call(self.definition)
        self.fake.wait_received("textDocument/definition")
        self.project.write_config({"schema_version": 1, "clangd": {"query_driver_paths": []}})
        self.fake.open_gate("config")
        envelope = call.join(25)
        assert_failure(self, envelope, "definition", "incomplete", "analysis/configuration-changed")
        assert_envelope(self, self.definition(), "definition", status="ok")

    def test_clangd_fragment_conflict_appearing_between_calls_blocks_queries(self) -> None:
        assert_envelope(self, self.definition(), "definition")
        self.project.write_text(".clangd", "CompileFlags:\n  CompilationDatabase: elsewhere\n")
        envelope = self.definition()
        assert_failure(
            self, envelope, "definition", "not_configured", "analysis/clangd-config-conflict"
        )
        status = assert_envelope(self, self.service.status(), "status", status="not_configured")
        self.assertIn(
            "analysis/clangd-config-conflict",
            [problem["code"] for problem in status["data"]["configuration_problems"]],
        )


if __name__ == "__main__":
    unittest.main()
