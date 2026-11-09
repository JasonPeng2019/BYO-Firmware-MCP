"""Keep a unittest fixture's owned temporary files before unittest tears it down."""

from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import sys
import tempfile
import unittest

import pytest

from tests.fixture_directory import retain_directory


_session_root: Path
_original_environment: dict[str, str | None] = {}


def pytest_sessionstart(session):
    global _session_root
    _session_root = Path(tempfile.mkdtemp(prefix="byo-tests-"))
    for key, child in (
        ("BYO_MCP_ARTIFACT_ROOT", "project"),
        ("BYO_MCP_MONITOR_ROOT", "monitor"),
        ("BYO_MANAGED_TEST_EVIDENCE", "managed"),
    ):
        _original_environment[key] = os.environ.get(key)
        if not os.environ.get(key, "").strip():
            os.environ[key] = str(_session_root / child)


def pytest_sessionfinish(session):
    try:
        if session.exitstatus:
            print(f"Test runtime retained: {_session_root}", file=sys.stderr)
        else:
            shutil.rmtree(_session_root)
    except OSError as error:
        print(f"Fixture cleanup failed: {error}; retained: {_session_root}", file=sys.stderr)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    finally:
        for key, value in _original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def retain_failed_inputs(request, monkeypatch):
    case = request.instance
    if not isinstance(case, unittest.TestCase):
        return

    def keep_inputs():
        try:
            temporary = Path(tempfile.gettempdir()).resolve()
            destination = (
                Path(os.environ.get("BYO_TEST_FAILURES", request.config.rootpath / ".test-failures"))
                / case.id()
            )
            copied = []
            for name in ("root", "base", "project", "store_dir"):
                value = getattr(case, name, None)
                if not isinstance(value, (str, Path)):
                    continue
                path = Path(value).resolve()
                if (
                    not path.is_dir()
                    or not path.is_relative_to(temporary)
                    or path == temporary
                    or any(path.is_relative_to(p) for p in copied)
                ):
                    continue
                if retain_directory(path, destination / name):
                    copied.append(path)
        except Exception as error:
            print(f"Additional fixture capture error: {error}", file=sys.stderr)

    def retaining(call):
        def run(*args, **kwargs):
            try:
                return call(*args, **kwargs)
            except unittest.SkipTest:
                raise
            except Exception:
                keep_inputs()
                raise

        return run

    # unittest handles these exceptions before pytest sees them. Capture while
    # the owned directory still exists, including partial setUp and cleanups.
    for name in ("_callSetUp", "_callTestMethod", "_callTearDown", "_callCleanup"):
        monkeypatch.setattr(case, name, retaining(getattr(case, name)))
    sub_test = case.subTest

    @contextmanager
    def retaining_subtest(*args, **kwargs):
        with sub_test(*args, **kwargs):
            try:
                yield
            except unittest.SkipTest:
                raise
            except Exception:
                # unittest records subtest failures without raising from the
                # method. Keep inputs before it handles the exception/teardown.
                keep_inputs()
                raise

    monkeypatch.setattr(case, "subTest", retaining_subtest)
