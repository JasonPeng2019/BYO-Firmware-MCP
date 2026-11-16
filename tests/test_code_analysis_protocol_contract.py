"""LSP protocol contract: framing, initialization, encodings and server requests.

Every case drives the real ``CodeAnalysisService`` and real ``ClangdAdapter``
(owned pipes, framing and reader) against ``tests/fake_clangd.py``; only the
adapter's private ``_argv`` is redirected to the fake.  Fixture success here is
protocol evidence, never native-clangd proof (see ``test_code_analysis_real_clangd``).
"""

from __future__ import annotations

import unittest

from tests.code_analysis_support import FakeServiceCase, assert_envelope

import pytest


class TransportFailures(FakeServiceCase):

    @pytest.mark.mvp
    def test_error_reply_is_not_an_empty_success(self) -> None:
        self.fake.configure(
            responses={"textDocument/definition": {"error": {"code": -32603, "message": "boom"}}}
        )
        envelope = self.definition()
        assert_envelope(self, envelope, "definition", status={"error", "incomplete"})
        self.assertNotIn(envelope["status"], {"ok", "not_found"})


if __name__ == "__main__":
    unittest.main()
