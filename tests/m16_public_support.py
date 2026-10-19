"""M16 host-only factory source and target-register boundary; policy stays public."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch


def register(address, role, expected=None, unit_bytes=None, *, mask=0xFFFFFFFF):
    return dict(
        address=address,
        width_bits=32,
        role=role,
        mask=mask,
        expected=expected,
        unit_bytes=unit_bytes,
        read_only=True,
        factory=True,
        source_pages=[1],
        source_note=f"Synthetic factory register table: {role} at {address:#x}; host test only.",
    )


@contextmanager
def factory_registers(f):
    """Only target I/O is substituted; no setup/gate/policy/acceptance seam."""
    source = f.root / "synthetic-factory-reference.pdf"
    source.write_bytes(
        b"%PDF-1.4\n% Synthetic host-only factory reference, not vendor or physical evidence.\n"
        b"Factory read-only device/group, flash capacity, UID registers and bounded layout.\n"
    )
    request = dict(
        start=0x80000,
        end=0x82000,
        source_path=str(source),
        source_url="https://example.invalid/synthetic-vendor/factory-reference.pdf",
        source_pages=[1],
        source_note="Synthetic register table and two-sector user layout.",
        identity_coverage="Synthetic supported-unit binding; package suffix is not proven.",
        registers=[
            register(0x10000100, "device", 0x52840),
            register(0x10000110, "flash_size", unit_bytes=1024),
            register(0x10000060, "unique"),
            register(0x10000064, "unique"),
        ],
    )
    values = {0x10000100: 0x52840, 0x10000110: 1024, 0x10000060: 0x62A73C51, 0x10000064: 0x8279BE14}
    original = f.backend.read_memory

    def read(handle, address, width_bits, **kwargs):
        if address not in values:
            return original(handle, address, width_bits, **kwargs)
        f.backend.calls.append(("factory-read", handle.probe_uid, address, width_bits))
        assert width_bits == 32
        value = values[address]
        if isinstance(value, Exception):
            raise value
        return value

    with patch.object(f.backend, "read_memory", read):
        yield SimpleNamespace(request=request, values=values, source=source)


def admit(f, request):
    f.flow.json("load_setup_tool", dict(board_id=f.board, tool_name="board_safety_refresh"))
    return f.flow.json("board_safety_refresh", dict(board_id=f.board, user_bootloader=request))
