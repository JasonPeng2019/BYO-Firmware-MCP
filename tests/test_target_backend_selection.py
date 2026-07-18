from __future__ import annotations

from dataclasses import replace

from pyocd_debug_mcp.adapters.swd_pyocd import PyOCDSWDInterface
from pyocd_debug_mcp import server
from pyocd_debug_mcp.services import target_control


class RecordingBackend(PyOCDSWDInterface):
    def __init__(self) -> None:
        self.handles: list[object] = []

    def get_state(self, handle) -> str:  # type: ignore[no-untyped-def]
        self.handles.append(handle)
        return "injected-state"


def test_target_control_uses_explicitly_injected_backend() -> None:
    backend = RecordingBackend()
    previous = target_control.configure_backend(backend)
    handle = object()
    try:
        assert target_control.current_backend() is backend
        assert target_control.get_state(handle) == "injected-state"  # type: ignore[arg-type]
        assert backend.handles == [handle]
    finally:
        target_control.configure_backend(previous)


def test_custom_backend_probe_selection_never_enters_pyocd_resolution(monkeypatch) -> None:
    base = server.resolve_board_config("nrf52840dk", None)
    assert base is not None
    board = replace(base, debug_backend="vendor-debug")
    monkeypatch.setenv("PYOCD_PROBE_UID", "must-not-leak")
    monkeypatch.setattr(
        server,
        "resolve_probe_for_board",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("custom backend entered pyOCD probe resolution")
        ),
    )

    assert server._resolve_probe_uid_for_connect(board, None) is None
    assert server._resolve_probe_uid_for_connect(board, "vendor-probe") == "vendor-probe"
