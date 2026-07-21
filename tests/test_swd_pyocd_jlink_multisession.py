from __future__ import annotations

import gc
import threading
import time
import unittest
import weakref
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

from pyocd_debug_mcp.adapters import swd_pyocd
from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp.target_errors import TargetConnectionError


def _callback(raw_callback: Callable[[str], object] | None) -> Callable[[bytes], object | None]:
    return lambda value: raw_callback(value.decode(errors="replace")) if raw_callback else None


class FakeLink:
    """Model pylink's bytes-to-text callback wrapping, not just constructor storage."""

    def __init__(
        self,
        *,
        log: Callable[[str], object] | None = None,
        detailed_log: Callable[[str], object] | None = None,
        error: Callable[[str], object] | None = None,
        warn: Callable[[str], object] | None = None,
        unsecure_hook: object = None,
        use_tmpcpy: bool | None = None,
    ) -> None:
        self.log_handler = _callback(log)
        self.detailed_log_handler = _callback(detailed_log)
        self.error_handler = _callback(error)
        self.warning_handler = _callback(warn)
        self._unsecure_hook = unsecure_hook
        self.use_tmpcpy = use_tmpcpy


class FakeTarget:
    def __init__(self) -> None:
        self.halt_calls = 0

    def halt(self) -> None:
        self.halt_calls += 1

    def get_state(self) -> Any:
        return SimpleNamespace(name="HALTED")


class FakeProbe:
    def __init__(
        self,
        uid: str,
        family: str = "jlink",
        close_error: Exception | None = None,
    ) -> None:
        self.unique_id = uid
        self.family = family
        self._link = FakeLink()
        self.reset_values: list[bool] = []
        self.opened = False
        self.close_error = close_error
        self.close_calls = 0

    @property
    def is_open(self) -> Callable[[], bool]:
        # pyOCD 0.45's JLinkProbe property returns pylink's opened method itself.
        return self._provider_opened

    def _provider_opened(self) -> bool:
        return self.opened

    def assert_reset(self, asserted: bool) -> None:
        self.reset_values.append(asserted)

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.opened = False


class FakeSession:
    def __init__(
        self,
        uid: str,
        *,
        family: str = "jlink",
        open_error: Exception | None = None,
        close_error: Exception | None = None,
        provider_close_error: Exception | None = None,
        close_leaves_provider_open: bool = False,
        open_started: threading.Event | None = None,
        open_release: threading.Event | None = None,
    ) -> None:
        self.probe = FakeProbe(uid, family, provider_close_error)
        self.target = FakeTarget()
        self.open_error = open_error
        self.close_error = close_error
        self.close_leaves_provider_open = close_leaves_provider_open
        self.open_started = open_started
        self.open_release = open_release
        self.close_calls = 0

    def open(self) -> None:
        if self.open_started is not None:
            self.open_started.set()
        if self.open_release is not None:
            self.open_release.wait(timeout=2)
        if self.open_error is not None:
            raise self.open_error
        self.probe.opened = True

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        if not self.close_leaves_provider_open:
            self.probe.opened = False


def board(family: str) -> Any:
    return SimpleNamespace(
        probe_family=family,
        pyocd_target=None,
        debug_protocol=None,
        debug_connect_mode=None,
        debug_clock_hz=None,
    )


class JLinkMultiSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        swd_pyocd._ACTIVE_JLINK_SESSIONS.clear()
        swd_pyocd._NORMAL_JLINK_SESSION = None
        self.provider_patch = patch.object(
            swd_pyocd,
            "probe_family_from_pyocd_probe",
            lambda probe: getattr(probe, "family", "unknown"),
        )
        self.provider_patch.start()

    def tearDown(self) -> None:
        self.provider_patch.stop()
        swd_pyocd._ACTIVE_JLINK_SESSIONS.clear()
        swd_pyocd._NORMAL_JLINK_SESSION = None

    def _open_sequence(self, interface: PyOCDSWDInterface, sessions: list[FakeSession]) -> Any:
        return patch.object(interface, "_choose_session", side_effect=sessions)

    def test_dynamic_and_configured_sessions_share_slot_allocator(self) -> None:
        first = FakeSession("683710208")
        second = FakeSession("683854191")
        third = FakeSession("683999999")
        originals = [candidate.probe._link for candidate in (first, second, third)]
        interface = PyOCDSWDInterface()
        self.assertTrue(callable(first.probe.is_open))

        with (
            self._open_sequence(interface, [first, second, third]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            first_handle = interface.open(board=None, unique_id=first.probe.unique_id, target=None)
            second_handle = interface.open(
                board=board("jlink"), unique_id=second.probe.unique_id, target=None
            )
            interface.close(first_handle)
            third_handle = interface.open(board=None, unique_id=third.probe.unique_id, target=None)

        self.assertIs(first.probe._link, originals[0])
        self.assertIsNot(second.probe._link, originals[1])
        self.assertIs(second.probe._link.use_tmpcpy, True)
        self.assertIs(third.probe._link, originals[2])
        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, third)
        for callback in (
            second.probe._link.log_handler,
            second.probe._link.detailed_log_handler,
            second.probe._link.error_handler,
            second.probe._link.warning_handler,
        ):
            callback(b"diagnostic")

        interface.close(second_handle)
        interface.close(third_handle)
        self.assertFalse(swd_pyocd._ACTIVE_JLINK_SESSIONS)
        self.assertIsNone(swd_pyocd._NORMAL_JLINK_SESSION)

    def test_non_jlink_session_is_not_tracked_or_modified(self) -> None:
        candidate = FakeSession("cmsis-dap-a", family="cmsisdap")
        original = candidate.probe._link
        interface = PyOCDSWDInterface()

        with (
            self._open_sequence(interface, [candidate]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            handle = interface.open(board=None, unique_id="cmsis-dap-a", target=None)

        self.assertIs(candidate.probe._link, original)
        self.assertFalse(swd_pyocd._ACTIVE_JLINK_SESSIONS)
        interface.close(handle)

    def test_failed_open_and_recovered_failed_close_do_not_leak_slots(self) -> None:
        failed_open = FakeSession("683710208", open_error=RuntimeError("permission denied"))
        failed_close = FakeSession("683854191", close_error=RuntimeError("close failed"))
        interface = PyOCDSWDInterface()

        with (
            self._open_sequence(interface, [failed_open, failed_close]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
            self.assertRaises(TargetConnectionError),
        ):
            interface.open(board=None, unique_id=failed_open.probe.unique_id, target=None)
        self.assertFalse(swd_pyocd._ACTIVE_JLINK_SESSIONS)

        with (
            self._open_sequence(interface, [failed_close]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            handle = interface.open(board=None, unique_id=failed_close.probe.unique_id, target=None)
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            interface.close(handle)
        self.assertEqual(failed_close.probe.close_calls, 1)
        self.assertFalse(swd_pyocd._ACTIVE_JLINK_SESSIONS)
        self.assertIsNone(swd_pyocd._NORMAL_JLINK_SESSION)

    def test_unconfirmed_failed_close_retains_normal_slot(self) -> None:
        failed_close = FakeSession(
            "683710208",
            close_error=RuntimeError("session close failed"),
            provider_close_error=RuntimeError("provider close failed"),
        )
        next_session = FakeSession("683854191")
        original_next_link = next_session.probe._link
        interface = PyOCDSWDInterface()

        with (
            self._open_sequence(interface, [failed_close, next_session]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            handle = interface.open(board=None, unique_id=failed_close.probe.unique_id, target=None)
            with self.assertRaisesRegex(RuntimeError, "session close failed"):
                interface.close(handle)
            next_handle = interface.open(
                board=None,
                unique_id=next_session.probe.unique_id,
                target=None,
            )

        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, failed_close)
        self.assertIn(id(failed_close), swd_pyocd._ACTIVE_JLINK_SESSIONS)
        self.assertIsNot(next_session.probe._link, original_next_link)
        self.assertIs(next_session.probe._link.use_tmpcpy, True)
        interface.close(next_handle)
        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, failed_close)

    def test_unconfirmed_close_reports_the_retained_reservation_type(self) -> None:
        normal = FakeSession(
            "683710208",
            provider_close_error=RuntimeError("normal provider close failed"),
            close_leaves_provider_open=True,
        )
        isolated = FakeSession(
            "683854191",
            provider_close_error=RuntimeError("isolated provider close failed"),
            close_leaves_provider_open=True,
        )
        interface = PyOCDSWDInterface()

        with (
            self._open_sequence(interface, [normal, isolated]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            normal_handle = interface.open(
                board=None,
                unique_id=normal.probe.unique_id,
                target=None,
            )
            with self.assertRaisesRegex(
                TargetConnectionError,
                "normal DLL slot remains reserved",
            ):
                interface.close(normal_handle)
            isolated_handle = interface.open(
                board=None,
                unique_id=isolated.probe.unique_id,
                target=None,
            )
            with self.assertRaisesRegex(
                TargetConnectionError,
                "isolated J-Link session remains reserved",
            ):
                interface.close(isolated_handle)

        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, normal)
        self.assertIs(swd_pyocd._ACTIVE_JLINK_SESSIONS[id(isolated)], isolated)

    def test_post_open_failure_retains_slot_when_cleanup_is_unconfirmed(self) -> None:
        failed = FakeSession(
            "683710208",
            close_error=RuntimeError("session close failed"),
            provider_close_error=RuntimeError("provider close failed"),
        )
        next_session = FakeSession("683854191")
        original_next_link = next_session.probe._link
        interface = PyOCDSWDInterface()

        def verify(session: FakeSession, *args: object) -> None:
            del args
            if session is failed and session.probe.opened:
                raise RuntimeError("post-open verification failed")

        with (
            self._open_sequence(interface, [failed, next_session]),
            patch.object(interface, "_verify_session_pack_source", side_effect=verify),
        ):
            with self.assertRaisesRegex(TargetConnectionError, "post-open verification failed"):
                interface.open(board=None, unique_id=failed.probe.unique_id, target=None)
            next_handle = interface.open(
                board=None,
                unique_id=next_session.probe.unique_id,
                target=None,
            )

        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, failed)
        self.assertIsNot(next_session.probe._link, original_next_link)
        self.assertIs(next_session.probe._link.use_tmpcpy, True)
        interface.close(next_handle)

    def test_unconfirmed_establishment_retains_owner_across_garbage_collection(self) -> None:
        failed = FakeSession(
            "683710208",
            close_error=RuntimeError("session close failed"),
            provider_close_error=RuntimeError("provider close failed"),
        )
        owner_ref = weakref.ref(failed)
        interface = PyOCDSWDInterface()

        def fail_after_open(session: FakeSession, *args: object) -> None:
            del args
            if session.probe.opened:
                raise RuntimeError("post-open verification failed")

        with (
            self._open_sequence(interface, [failed]),
            patch.object(
                interface,
                "_verify_session_pack_source",
                side_effect=fail_after_open,
            ),
            self.assertRaisesRegex(TargetConnectionError, "post-open verification failed"),
        ):
            interface.open(board=None, unique_id=failed.probe.unique_id, target=None)

        del failed
        gc.collect()
        retained_owner = owner_ref()
        self.assertIsNotNone(retained_owner)
        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, retained_owner)
        assert retained_owner is not None
        self.assertIs(
            swd_pyocd._ACTIVE_JLINK_SESSIONS[id(retained_owner)],
            retained_owner,
        )

        next_session = FakeSession("683854191")
        original_next_link = next_session.probe._link
        with (
            self._open_sequence(interface, [next_session]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            next_handle = interface.open(
                board=None,
                unique_id=next_session.probe.unique_id,
                target=None,
            )
        self.assertIsNot(next_session.probe._link, original_next_link)
        interface.close(next_handle)
        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, retained_owner)

    def test_retry_post_open_failure_retains_slot_when_cleanup_is_unconfirmed(self) -> None:
        first = FakeSession("683710208", open_error=RuntimeError("transient serial open"))
        retry = FakeSession(
            "683710208",
            close_error=RuntimeError("retry session close failed"),
            provider_close_error=RuntimeError("retry provider close failed"),
        )
        interface = PyOCDSWDInterface()

        def verify(session: FakeSession, *args: object) -> None:
            del args
            if session is retry and session.probe.opened:
                raise RuntimeError("retry post-open verification failed")

        with (
            self._open_sequence(interface, [first, retry]),
            patch.object(interface, "_verify_session_pack_source", side_effect=verify),
            patch.object(swd_pyocd, "_should_retry_without_uid", return_value=True),
            self.assertRaisesRegex(TargetConnectionError, "retry post-open verification failed"),
        ):
            interface.open(
                board=board("jlink"),
                unique_id=first.probe.unique_id,
                target=None,
            )

        self.assertEqual(first.close_calls, 1)
        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, retry)
        self.assertIn(id(retry), swd_pyocd._ACTIVE_JLINK_SESSIONS)

    def test_under_reset_post_open_failure_retains_slot_when_cleanup_is_unconfirmed(self) -> None:
        failed = FakeSession(
            "683710208",
            close_error=RuntimeError("under-reset session close failed"),
            provider_close_error=RuntimeError("under-reset provider close failed"),
        )
        interface = PyOCDSWDInterface()

        def verify(session: FakeSession, *args: object) -> None:
            del args
            if session.probe.opened:
                raise RuntimeError("under-reset post-open verification failed")

        with (
            self._open_sequence(interface, [failed]),
            patch.object(interface, "_verify_session_pack_source", side_effect=verify),
            self.assertRaisesRegex(
                TargetConnectionError,
                "under-reset post-open verification failed",
            ),
        ):
            interface.connect_under_reset(
                board=None,
                unique_id=failed.probe.unique_id,
                target=None,
            )

        self.assertEqual(failed.probe.reset_values, [False])
        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, failed)
        self.assertIn(id(failed), swd_pyocd._ACTIVE_JLINK_SESSIONS)

    def test_connect_under_reset_registers_and_public_close_releases(self) -> None:
        candidate = FakeSession("683710208")
        interface = PyOCDSWDInterface()
        with (
            self._open_sequence(interface, [candidate]),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            handle = interface.connect_under_reset(
                board=None,
                unique_id=candidate.probe.unique_id,
                target=None,
            )

        self.assertEqual(candidate.target.halt_calls, 1)
        self.assertIs(swd_pyocd._NORMAL_JLINK_SESSION, candidate)
        interface.close(handle)
        self.assertFalse(swd_pyocd._ACTIVE_JLINK_SESSIONS)

    def test_concurrent_opens_serialize_selection_through_registration(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        first = FakeSession("683710208", open_started=first_started, open_release=release_first)
        second = FakeSession("683854191")
        interface = PyOCDSWDInterface()
        choose_count = 0
        choose_lock = threading.Lock()
        sessions = iter((first, second))

        def choose(*, probe_uid: str | None, options: dict[str, object] | None) -> FakeSession:
            nonlocal choose_count
            del probe_uid, options
            with choose_lock:
                choose_count += 1
                return next(sessions)

        handles: list[Any] = []

        def connect(uid: str) -> None:
            handles.append(interface.open(board=None, unique_id=uid, target=None))

        with (
            patch.object(interface, "_choose_session", side_effect=choose),
            patch.object(interface, "_verify_session_pack_source", lambda *args: None),
        ):
            first_thread = threading.Thread(target=connect, args=(first.probe.unique_id,))
            second_thread = threading.Thread(target=connect, args=(second.probe.unique_id,))
            first_thread.start()
            self.assertTrue(first_started.wait(timeout=1))
            second_thread.start()
            time.sleep(0.05)
            self.assertEqual(choose_count, 1)
            release_first.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)

        self.assertEqual(choose_count, 2)
        self.assertEqual(len(handles), 2)
        self.assertIs(second.probe._link.use_tmpcpy, True)
        for handle in handles:
            interface.close(handle)


if __name__ == "__main__":
    unittest.main()
