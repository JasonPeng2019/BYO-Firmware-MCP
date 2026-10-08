"""LSP protocol contract: framing, initialization, encodings and server requests.

Every case drives the real ``CodeAnalysisService`` and real ``ClangdAdapter``
(owned pipes, framing and reader) against ``tests/fake_clangd.py``; only the
adapter's private ``_argv`` is redirected to the fake.  Fixture success here is
protocol evidence, never native-clangd proof (see ``test_code_analysis_real_clangd``).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tests.code_analysis_support import (
    Call,
    FakeClangd,
    FakeServiceCase,
    assert_envelope,
    assert_failure,
    assert_source,
    file_uri,
    first_location,
    lsp_character,
    make_service,
    query_params,
    raw_range,
    same_path,
    sha256_file,
)

POSITION_FIELDS = (
    "uri",
    "path",
    "position_mapping",
    "range",
    "raw_lsp_range",
    "lsp_position_encoding",
)


def _contains_value(value: Any, wanted: Any) -> bool:
    if value == wanted:
        return True
    if isinstance(value, dict):
        return any(_contains_value(item, wanted) for item in value.values())
    if isinstance(value, list):
        return any(_contains_value(item, wanted) for item in value)
    return False


class InitializationContract(FakeServiceCase):
    def test_initialize_announces_root_database_encodings_and_versioned_diagnostics(self) -> None:
        envelope = self.definition()
        assert_envelope(self, envelope, "definition", status={"ok", "not_found"})

        initialize = self.fake.received("initialize")
        self.assertEqual(len(initialize), 1, "exactly one initialize per session")
        params = initialize[0]["params"]
        self.assertTrue(same_path(Path(_uri_path(params["rootUri"])), self.project.root), params)
        folders = params.get("workspaceFolders")
        if folders is not None:
            self.assertEqual(len(folders), 1, folders)
            self.assertTrue(same_path(Path(_uri_path(folders[0]["uri"])), self.project.root))
        database_dir = params["initializationOptions"]["compilationDatabasePath"]
        self.assertTrue(same_path(database_dir, self.project.root), database_dir)
        capabilities = params["capabilities"]
        encodings = capabilities["general"]["positionEncodings"]
        self.assertTrue({"utf-8", "utf-16", "utf-32"} >= set(encodings), encodings)
        self.assertIn("utf-16", encodings, "standard UTF-16 must remain negotiable")
        self.assertIs(
            capabilities["textDocument"]["publishDiagnostics"]["versionSupport"], True, capabilities
        )

    def test_initialized_only_after_successful_initialize_reply(self) -> None:
        self.definition()
        events = self.fake.events()
        reply_index = next(
            index
            for index, event in enumerate(events)
            if event["event"] == "sent"
            and "result" in event["message"]
            and event["message"]["id"] == self.fake.received("initialize")[0]["id"]
        )
        initialized_index = next(
            index
            for index, event in enumerate(events)
            if event["event"] == "received" and event["message"].get("method") == "initialized"
        )
        self.assertLess(reply_index, initialized_index)
        first_query = next(
            index
            for index, event in enumerate(events)
            if event["event"] == "received"
            and event["message"].get("method") == "textDocument/definition"
        )
        self.assertLess(initialized_index, first_query)

    def test_status_never_starts_the_language_server(self) -> None:
        envelope = assert_envelope(self, self.service.status(), "status")
        self.assertEqual(envelope["data"]["session_state"], "not_started")
        self.assertEqual(self.fake.starts(), [], "status must not start an LSP child")
        self.assertEqual(self.fake.received("initialize"), [])

    def test_empty_query_driver_allowlist_grants_no_compiler_execution(self) -> None:
        self.definition()
        argv = self.fake.starts()[0]["argv"]
        drivers = [item for item in argv if item.startswith("--query-driver")]
        for item in drivers:
            self.assertEqual(item.partition("=")[2], "", f"empty allowlist widened: {argv}")


class FailedInitialization(FakeServiceCase):
    def scenario(self) -> dict[str, Any]:
        return {"responses": {"initialize": {"error": {"code": -32603, "message": "init failed"}}}}

    def test_failed_initialize_is_an_error_and_never_sends_initialized(self) -> None:
        envelope = self.definition()
        assert_envelope(self, envelope, "definition", status="error")
        self.assertIn(envelope["code"], {"analysis/protocol-error", "analysis/child-exited"})
        self.assertEqual(self.fake.received("initialized"), [])
        self.assertEqual(self.fake.received("textDocument/definition"), [])


class QueryDriverArguments(unittest.TestCase):
    def test_exact_existing_driver_reaches_clangd_as_a_literal(self) -> None:
        from tests.code_analysis_support import simple_project, temporary_directory

        with temporary_directory("byo-ca-driver-") as base:
            project = simple_project(base / "project")
            driver = Path(sys.executable).resolve()
            project.write_config(
                {"schema_version": 1, "clangd": {"query_driver_paths": [str(driver)]}}
            )
            fake = FakeClangd(base / "fake")
            service = make_service(project.root, fake=fake, testcase=self)
            envelope = service.definition(
                file_path=str(project.path("src/main.c")), line=3, column=5, timeout_seconds=10.0
            )
            assert_envelope(self, envelope, "definition", status={"ok", "not_found"})
            argv = fake.starts()[0]["argv"]
            values = [item.partition("=")[2] for item in argv if item.startswith("--query-driver=")]
            self.assertEqual(len(values), 1, argv)
            self.assertIn(
                os.path.normcase(str(driver)).replace("\\", "/"),
                os.path.normcase(values[0]).replace("\\", "/"),
                argv,
            )
            service.shutdown()

    def test_metacharacters_or_commas_never_broaden_a_literal_driver(self) -> None:
        from tests.code_analysis_support import simple_project, temporary_directory

        with temporary_directory("byo-ca-driver-meta-") as base:
            project = simple_project(base / "project")
            odd = base / "tools" / "arm,gcc[1]*.exe"
            try:
                odd.parent.mkdir(parents=True)
                odd.write_bytes(Path(sys.executable).read_bytes()[:64])
            except OSError as exc:  # '*' is not a legal Windows file-name character
                odd = base / "tools" / "arm,gcc[1].exe"
                odd.write_bytes(b"MZ")
                self.assertTrue(odd.exists(), exc)
            if os.name != "nt":
                odd.chmod(0o755)
            project.write_config(
                {"schema_version": 1, "clangd": {"query_driver_paths": [str(odd)]}}
            )
            fake = FakeClangd(base / "fake")
            service = make_service(project.root, fake=fake, testcase=self)
            status = assert_envelope(self, service.status(), "status")
            codes = {problem["code"] for problem in status["data"]["configuration_problems"]}
            if "analysis/driver-invalid" in codes:
                envelope = service.definition(
                    file_path=str(project.path("src/main.c")),
                    line=3,
                    column=5,
                    timeout_seconds=10.0,
                )
                assert_failure(
                    self, envelope, "definition", "not_configured", "analysis/driver-invalid"
                )
                return
            # Otherwise the literal must be escaped, never passed as a raw
            # comma-separated glob allowlist.
            envelope = service.definition(
                file_path=str(project.path("src/main.c")), line=3, column=5, timeout_seconds=10.0
            )
            assert_envelope(self, envelope, "definition")
            argv = fake.starts()[0]["argv"] if fake.starts() else []
            for item in argv:
                if item.startswith("--query-driver="):
                    value = item.partition("=")[2]
                    self.assertNotEqual(value, str(odd), "raw metacharacters reached clangd")
                    self.assertNotIn(str(odd.name), value.split(","), argv)


class PositionEncodingNegotiation(unittest.TestCase):
    """The server's selection is honored; UTF-16 is the default; others are refused."""

    TEXT = "// café 中文 😀 é\nint 😀_helper_x; int target(int v) { return v; }\nint use(void) { return target(1); }\n"

    def _run(self, encoding: str | None) -> tuple[dict[str, Any], FakeClangd, Path]:
        from tests.code_analysis_support import simple_project, temporary_directory

        directory = temporary_directory("byo-ca-enc-")
        base = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        project = simple_project(base / "project", sources={"src/main.c": self.TEXT})
        source = project.path("src/main.c")
        scenario: dict[str, Any] = {"position_encoding": encoding}
        # Echo the requested position back as the definition range start so
        # the reverse mapping is checked with the negotiated units.  An
        # unsupported selection has no oracle, so it gets UTF-16 numbers that
        # the service must never use.
        oracle = encoding if encoding in {"utf-8", "utf-16", "utf-32"} else "utf-16"
        line_one = self.TEXT.split("\n")[1]
        target_column = line_one.index("target") + 1
        start = lsp_character(line_one, target_column, oracle)
        end = lsp_character(line_one, target_column + len("target"), oracle)
        scenario["responses"] = {
            "textDocument/definition": {
                "result": {"uri": file_uri(source), "range": raw_range((1, start), (1, end))}
            }
        }
        fake = FakeClangd(base / "fake", scenario)
        service = make_service(project.root, fake=fake, testcase=self)
        line_two = self.TEXT.split("\n")[2]
        column = line_two.index("target") + 1
        envelope = service.definition(
            file_path=str(source), line=3, column=column, timeout_seconds=10.0
        )
        return envelope, fake, source

    def test_supported_encodings_convert_both_directions(self) -> None:
        line_one = self.TEXT.split("\n")[1]
        line_two = self.TEXT.split("\n")[2]
        target_column = line_one.index("target") + 1
        for encoding in ("utf-8", "utf-16", "utf-32", None):
            with self.subTest(encoding=encoding):
                envelope, fake, source = self._run(encoding)
                assert_envelope(self, envelope, "definition", status="ok")
                assert_source(self, envelope, source)
                effective = encoding or "utf-16"
                sent = query_params(fake, "textDocument/definition")[0]["position"]
                self.assertEqual(
                    sent,
                    {
                        "line": 2,
                        "character": lsp_character(
                            line_two, line_two.index("target") + 1, effective
                        ),
                    },
                )
                location = first_location(self, envelope)
                self.assertTrue(set(POSITION_FIELDS) <= set(location), location)
                self.assertEqual(location["lsp_position_encoding"], effective)
                self.assertEqual(location["position_mapping"], "available")
                self.assertEqual(
                    location["range"],
                    {
                        "start": {"line": 2, "column": target_column},
                        "end": {"line": 2, "column": target_column + len("target")},
                    },
                )

    def test_unsupported_server_selection_is_rejected_without_guessing(self) -> None:
        for encoding in ("utf-7", "bogus"):
            with self.subTest(encoding=encoding):
                envelope, fake, _source = self._run(encoding)
                assert_envelope(self, envelope, "definition", status={"error", "incomplete"})
                self.assertIn(
                    envelope["code"], {"analysis/capability-unsupported", "analysis/protocol-error"}
                )
                self.assertEqual(fake.received("textDocument/definition"), [])


class FramingAndRouting(FakeServiceCase):
    def scenario(self) -> dict[str, Any]:
        return {"framing": "split"}

    def test_split_frames_with_multibyte_bodies_reassemble(self) -> None:
        value = "中文 😀 café — é " * 40
        self.fake.configure(
            framing="split",
            responses={
                "textDocument/hover": {"result": {"contents": {"kind": "markdown", "value": value}}}
            },
        )
        line, column = self.position_of("helper", occurrence=1)
        envelope = self.service.hover(
            file_path=str(self.source()), line=line, column=column, timeout_seconds=10.0
        )
        assert_envelope(self, envelope, "hover", status="ok")
        self.assertEqual(envelope["data"]["contents"], {"kind": "markdown", "value": value})

    def test_notification_concatenated_with_reply_is_routed(self) -> None:
        self.fake.configure(
            framing="contiguous",
            responses={
                "textDocument/definition": {
                    "result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))},
                    "with_notification": {
                        "jsonrpc": "2.0",
                        "method": "window/logMessage",
                        "params": {"type": 3, "message": "concatenated"},
                    },
                }
            },
        )
        envelope = self.definition()
        assert_envelope(self, envelope, "definition", status="ok")
        self.assertEqual(
            first_location(self, envelope)["raw_lsp_range"], raw_range((2, 4), (2, 10))
        )

    def test_out_of_order_replies_reach_their_own_callers(self) -> None:
        echo = {
            "uri": "$request_uri",
            "range": {"start": "$request_position", "end": "$request_position"},
        }
        self.fake.configure(
            framing="contiguous",
            responses={"textDocument/references": {"result": [echo], "batch": 2}},
        )
        first = self.position_of("helper", occurrence=0)
        second = self.position_of("add")
        calls = [
            Call(
                self.service.references,
                file_path=str(self.source()),
                line=line,
                column=column,
                timeout_seconds=15.0,
            )
            for line, column in (first, second)
        ]
        results = [call.join(30) for call in calls]
        ids = [message["id"] for message in self.fake.received("textDocument/references")]
        self.assertEqual(len(set(ids)), 2, "request ids must be unique on a shared transport")
        for (line, column), envelope in zip((first, second), results, strict=True):
            assert_envelope(self, envelope, "references", status="ok")
            mapped = first_location(self, envelope)["range"]
            self.assertEqual(mapped["start"], {"line": line, "column": column}, envelope)


class ServerInitiatedRequests(FakeServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.requests = _server_requests(file_uri(self.source()))
        self.fake.configure(server_requests=self.requests)

    def test_server_requests_are_answered_without_execution(self) -> None:
        before = self.project.tree_digest()
        envelope = self.definition()
        assert_envelope(self, envelope, "definition", status={"ok", "not_found"})
        responses = self.fake.wait_for(
            lambda: (
                (found := self.fake.client_responses())
                and len(found) >= len(self.requests)
                and found
            ),
            message="client responses to every server request",
        )
        self.assertEqual(
            responses["edit"].get("result", {}).get("applied"), False, responses["edit"]
        )
        self.assertEqual(responses["unknown"]["error"]["code"], -32601, responses["unknown"])
        configuration = responses["configuration"]
        self.assertNotIn("error", configuration)
        self.assertIsInstance(configuration["result"], list)
        self.assertEqual(len(configuration["result"]), 2)
        self.assertNotIn("error", responses["progress"], responses["progress"])
        self.assertNotIn("error", responses["message"], responses["message"])
        self.assertTrue(
            "error" in responses["register"] or "result" in responses["register"], responses
        )
        folders = responses["folders"]
        self.assertNotIn("error", folders)
        self.assertEqual(len(folders["result"]), 1)
        self.assertTrue(same_path(_uri_path(folders["result"][0]["uri"]), self.project.root))
        self.assertEqual(
            self.project.tree_digest(), before, "a server edit/command changed the project"
        )
        self.assertEqual(self.fake.received("workspace/executeCommand"), [])


class TransportFailures(FakeServiceCase):
    def test_malformed_transport_fails_the_waiter_and_a_later_call_starts_fresh(self) -> None:
        self.fake.configure(responses={"textDocument/definition": {"malformed": True}})
        envelope = self.definition()
        assert_envelope(self, envelope, "definition", status="error")
        self.assertIn(envelope["code"], {"analysis/protocol-error", "analysis/child-exited"})

        self.fake.configure(
            responses={
                "textDocument/definition": {
                    "result": {"uri": "$request_uri", "range": raw_range((2, 4), (2, 10))}
                }
            }
        )
        recovered = self.definition()
        assert_envelope(self, recovered, "definition", status="ok")
        self.assertEqual(len(self.fake.starts()), 2, "a fresh child serves the next call")

    def test_error_reply_is_not_an_empty_success(self) -> None:
        self.fake.configure(
            responses={"textDocument/definition": {"error": {"code": -32603, "message": "boom"}}}
        )
        envelope = self.definition()
        assert_envelope(self, envelope, "definition", status={"error", "incomplete"})
        self.assertNotIn(envelope["status"], {"ok", "not_found"})


class DocumentSynchronization(FakeServiceCase):
    sources = {
        "src/main.c": "int helper(int v)\r\n{\r\n\treturn v; /* café 😀 */\r\n}\r\nint main(void) { return helper(1); }",
    }

    def test_did_open_sends_exact_saved_text_and_language(self) -> None:
        self.definition()
        opened = query_params(self.fake, "textDocument/didOpen")
        self.assertEqual(len(opened), 1, opened)
        document = opened[0]["textDocument"]
        self.assertEqual(document["text"], self.source().read_bytes().decode("utf-8"))
        self.assertIn("\r\n", document["text"], "CRLF must survive synchronization")
        self.assertEqual(document["languageId"], "c")
        self.assertIsInstance(document["version"], int)
        self.assertGreater(document["version"], 0)
        self.assertEqual(Path(_uri_path(document["uri"])).resolve(), self.source().resolve())

    def test_changed_saved_bytes_use_full_content_did_change_with_a_higher_version(self) -> None:
        first = self.definition()
        version_one = assert_source(self, first, self.source())["document_version"]
        updated = self.source().read_bytes().replace(b"return v;", b"return v + 1;")
        self.source().write_bytes(updated)
        second = self.definition()
        version_two = assert_source(self, second, self.source())["document_version"]
        self.assertGreater(version_two, version_one)
        changes = query_params(self.fake, "textDocument/didChange")
        self.assertTrue(changes, "changed saved bytes must be synchronized")
        latest = changes[-1]
        self.assertEqual(latest["textDocument"]["version"], version_two)
        self.assertEqual(latest["contentChanges"][-1], {"text": updated.decode("utf-8")})
        self.assertEqual(second["source"]["sha256"], sha256_file(self.source()))

    def test_different_documents_share_one_transport(self) -> None:
        self.project.write_text("src/other.c", "int other(void) { return 2; }\n")
        self.project.write_database(
            [
                self.project.entry("src/main.c", arguments=["clang", "-c", str(self.source())]),
                self.project.entry(
                    "src/other.c", arguments=["clang", "-c", str(self.project.path("src/other.c"))]
                ),
            ]
        )
        self.definition()
        self.service.hover(
            file_path=str(self.project.path("src/other.c")), line=1, column=5, timeout_seconds=10.0
        )
        self.assertEqual(len(self.fake.starts()), 1)
        ids = [message["id"] for message in self.fake.received() if "id" in message]
        self.assertEqual(len(ids), len(set(ids)), "request ids must be unique per transport")


def _uri_path(uri: str) -> str:
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname

    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise AssertionError(f"not a file URI: {uri}")
    return url2pathname(unquote(parsed.path)) if os.name == "nt" else unquote(parsed.path)


def _server_requests(source_uri: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "edit",
            "method": "workspace/applyEdit",
            "params": {
                "label": "rewrite",
                "edit": {
                    "changes": {
                        source_uri: [{"range": raw_range((0, 0), (0, 0)), "newText": "EVIL();\n"}]
                    }
                },
            },
        },
        {"id": "unknown", "method": "custom/doSomethingDangerous", "params": {"cmd": "rm -rf /"}},
        {
            "id": "configuration",
            "method": "workspace/configuration",
            "params": {"items": [{"section": "clangd"}, {"section": "other"}]},
        },
        {"id": "progress", "method": "window/workDoneProgress/create", "params": {"token": "t1"}},
        {
            "id": "message",
            "method": "window/showMessageRequest",
            "params": {"type": 1, "message": "run it?", "actions": [{"title": "yes"}]},
        },
        {
            "id": "register",
            "method": "client/registerCapability",
            "params": {
                "registrations": [{"id": "r1", "method": "workspace/didChangeWatchedFiles"}]
            },
        },
        {"id": "folders", "method": "workspace/workspaceFolders", "params": None},
    ]


class ResultUris(FakeServiceCase):
    """Local file URIs decode exactly; non-local URIs are preserved unmapped."""

    def _definition_returning(self, uri: str) -> dict[str, Any]:
        self.fake.configure(
            responses={
                "textDocument/definition": {
                    "result": {"uri": uri, "range": raw_range((0, 0), (0, 3))}
                }
            }
        )
        return self.definition()

    def test_percent_encoded_unicode_and_spaced_local_paths(self) -> None:
        target = self.project.write_text("include dir/café 中文 😀.h", "int x;\n")
        envelope = self._definition_returning(target.resolve().as_uri())
        assert_envelope(self, envelope, "definition", status="ok")
        location = first_location(self, envelope)
        self.assertTrue(same_path(location["path"], target), location)
        self.assertEqual(location["position_mapping"], "available")
        self.assertEqual(
            location["range"], {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 4}}
        )

    @unittest.skipUnless(os.name == "nt", "Windows drive-letter URI forms")
    def test_windows_drive_forms_normalize_to_the_same_local_path(self) -> None:
        target = self.project.write_text("src/drive.h", "int y;\n")
        resolved = str(target.resolve())
        drive, rest = resolved[0], resolved[2:].replace("\\", "/")
        for uri in (
            f"file:///{drive.upper()}:{quote(rest)}",
            f"file:///{drive.lower()}%3A{quote(rest)}",
        ):
            with self.subTest(uri=uri):
                envelope = self._definition_returning(uri)
                assert_envelope(self, envelope, "definition", status="ok")
                self.assertTrue(same_path(first_location(self, envelope)["path"], target))

    def test_non_local_uris_are_preserved_without_mapping_or_fetching(self) -> None:
        for uri in ("https://example.invalid/sdk/x.c", "file://remotehost/share/x.c"):
            with self.subTest(uri=uri):
                envelope = self._definition_returning(uri)
                assert_envelope(self, envelope, "definition", status={"ok", "incomplete"})
                location = first_location(self, envelope)
                self.assertEqual(location["uri"], uri)
                self.assertIsNone(location["path"])
                self.assertEqual(location["position_mapping"], "unavailable")
                self.assertIsNone(location["range"])
                self.assertEqual(location["raw_lsp_range"], raw_range((0, 0), (0, 3)))
                self.assertTrue(envelope["warnings"], "unmapped location needs a warning")


class DefinitionShapes(FakeServiceCase):
    def _with(self, result: Any) -> dict[str, Any]:
        self.fake.configure(responses={"textDocument/definition": {"result": result}})
        return self.definition()

    def test_location_array_and_null(self) -> None:
        uri = file_uri(self.source())
        single = self._with({"uri": uri, "range": raw_range((2, 4), (2, 10))})
        assert_envelope(self, single, "definition", status="ok")
        self.assertEqual(len(single["data"]["locations"]), 1)
        many = self._with(
            [
                {"uri": uri, "range": raw_range((2, 4), (2, 10))},
                {"uri": uri, "range": raw_range((4, 4), (4, 8))},
            ]
        )
        self.assertEqual(
            [item["raw_lsp_range"] for item in many["data"]["locations"]],
            [raw_range((2, 4), (2, 10)), raw_range((4, 4), (4, 8))],
            "backend order must be retained",
        )
        for empty in (None, []):
            with self.subTest(result=empty):
                envelope = self._with(empty)
                assert_envelope(self, envelope, "definition", status="not_found")
                self.assertEqual(envelope["data"]["locations"], [])
                self.assertNotIn("code", envelope, "not_found is not an execution failure")

    def test_location_links_keep_target_selection_and_origin_ranges(self) -> None:
        uri = file_uri(self.source())
        link = {
            "originSelectionRange": raw_range((4, 24), (4, 30)),
            "targetUri": uri,
            "targetRange": raw_range((2, 0), (2, 43)),
            "targetSelectionRange": raw_range((2, 4), (2, 10)),
        }
        envelope = self._with([link])
        assert_envelope(self, envelope, "definition", status="ok")
        location = first_location(self, envelope)
        self.assertEqual(location["uri"], uri)
        self.assertTrue(same_path(location["path"], self.source()))
        for name in ("targetRange", "targetSelectionRange", "originSelectionRange"):
            self.assertTrue(_contains_value(location, link[name]), f"{name} lost: {location}")


class ClangdConfigurationBoundary(FakeServiceCase):
    def test_project_fragment_selecting_another_database_is_not_configured(self) -> None:
        for fragment in (
            "CompileFlags:\n  CompilationDatabase: other-build\n",
            "CompileFlags:\n  CompilationDatabase: None\n",
        ):
            with self.subTest(fragment=fragment):
                self.project.write_text(".clangd", fragment)
                before = self.project.path(".clangd").read_bytes()
                status = assert_envelope(self, self.service.status(), "status")
                self.assertEqual(status["status"], "not_configured", status)
                self.assertIn(
                    "analysis/clangd-config-conflict",
                    {problem["code"] for problem in status["data"]["configuration_problems"]},
                )
                envelope = self.definition()
                assert_failure(
                    self,
                    envelope,
                    "definition",
                    "not_configured",
                    "analysis/clangd-config-conflict",
                )
                self.assertEqual(
                    self.project.path(".clangd").read_bytes(), before, ".clangd rewritten"
                )

    def test_fragment_selecting_the_same_database_is_accepted(self) -> None:
        self.project.write_text(".clangd", "CompileFlags:\n  CompilationDatabase: .\n")
        status = assert_envelope(self, self.service.status(), "status")
        self.assertNotIn(
            "analysis/clangd-config-conflict",
            {problem["code"] for problem in status["data"]["configuration_problems"]},
        )


class CompileContext(FakeServiceCase):
    def test_source_outside_the_database_is_not_configured(self) -> None:
        orphan = self.project.write_text("src/orphan.c", "int orphan(void) { return 0; }\n")
        envelope = self.service.definition(
            file_path=str(orphan), line=1, column=5, timeout_seconds=10.0
        )
        assert_failure(
            self, envelope, "definition", "not_configured", "analysis/compile-context-missing"
        )

    def test_header_query_discloses_inferred_context(self) -> None:
        envelope = self.service.hover(
            file_path=str(self.project.path("src/util.h")), line=2, column=19, timeout_seconds=10.0
        )
        assert_envelope(self, envelope, "hover")
        if envelope["status"] in {"ok", "not_found"}:
            text = json.dumps(envelope["warnings"]).lower()
            self.assertTrue(
                "header" in text or "infer" in text,
                f"header results must disclose inferred context: {envelope}",
            )
        else:
            self.assertEqual(envelope["code"], "analysis/compile-context-missing", envelope)


if __name__ == "__main__":
    unittest.main()
