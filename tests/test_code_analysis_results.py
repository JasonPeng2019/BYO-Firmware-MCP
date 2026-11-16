"""Public result contract: envelopes, status, positions and normalization.

Drives the real ``CodeAnalysisService`` + real ``ClangdAdapter`` against the
scripted fake peer.  Expected coordinates come from independent oracles in
``code_analysis_support`` (code points <-> UTF-8/16/32 units), never from the
implementation.  Fake success is protocol evidence, not native-clangd proof.
"""

from __future__ import annotations

import unittest

from tests.code_analysis_support import FakeClangd, assert_envelope, first_location, lines_of, lsp_character, make_service, mapped_range, query_params, simple_project, temporary_directory


ECHO = {"uri": "$request_uri", "range": {"start": "$request_position", "end": "$request_position"}}

UNICODE_TEXT = (
    "/* café é 中文 😀 */\r\n"
    "\tint helper(int v) { return v; } /* 😀 tail */\r\n"
    "int main(void) { /* 中😀 */ return helper(1); }"
)


class UnicodePositionMapping(unittest.TestCase):
    """Public 1-based code-point columns <-> negotiated LSP units, both directions."""

    CASES = (
        ("accented", 1, "café"),
        ("combining", 1, "é"),
        ("cjk", 1, "中文"),
        ("non_bmp", 1, "😀"),
        ("after_non_bmp", 1, "*/"),
        ("after_tab", 2, "helper"),
        ("after_emoji_next_line", 2, "tail"),
        ("crlf_last_line", 3, "return"),
    )

    def _setup(self, encoding: str | None):
        directory = temporary_directory("byo-ca-unicode-")
        base = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        project = simple_project(base / "project", sources={"src/main.c": UNICODE_TEXT})
        fake = FakeClangd(
            base / "fake",
            {
                "position_encoding": encoding,
                "responses": {"textDocument/references": {"result": [ECHO]}},
            },
        )
        service = make_service(project.root, fake=fake, testcase=self)
        return service, fake, project.path("src/main.c")

    def _positions(self) -> list[tuple[str, int, int]]:
        lines = lines_of(UNICODE_TEXT)
        positions = [
            (name, line, lines[line - 1].index(needle) + 1) for name, line, needle in self.CASES
        ]
        positions.append(("combining_mark_itself", 1, lines[0].index("́") + 1))
        positions.append(("line_end_crlf", 1, len(lines[0]) + 1))
        positions.append(("line_end_eof", 3, len(lines[2]) + 1))
        positions.append(("line_start", 2, 1))
        return positions

    def test_positions_round_trip_in_every_supported_encoding(self) -> None:
        lines = lines_of(UNICODE_TEXT)
        for encoding in ("utf-16", "utf-8", "utf-32"):
            service, fake, source = self._setup(encoding)
            for name, line, column in self._positions():
                with self.subTest(encoding=encoding, case=name):
                    envelope = service.references(
                        file_path=str(source), line=line, column=column, timeout_seconds=10.0
                    )
                    assert_envelope(self, envelope, "references", status="ok")
                    sent = query_params(fake, "textDocument/references")[-1]["position"]
                    self.assertEqual(
                        sent,
                        {
                            "line": line - 1,
                            "character": lsp_character(lines[line - 1], column, encoding),
                        },
                    )
                    location = first_location(self, envelope)
                    self.assertEqual(location["lsp_position_encoding"], encoding)
                    self.assertEqual(location["position_mapping"], "available")
                    self.assertEqual(
                        location["range"], mapped_range((line, column), (line, column))
                    )
                    self.assertEqual(location["raw_lsp_range"], {"start": sent, "end": sent})


if __name__ == "__main__":
    unittest.main()
