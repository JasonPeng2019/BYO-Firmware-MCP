"""Selected-payload native headers; loader closure belongs to release validation."""

from __future__ import annotations

import struct
from collections.abc import Callable
from pathlib import Path


def validate_native(
    path: Path,
    platform: str,
    architecture: str,
    *,
    dependency: bool = False,
    check_budget: Callable[[], object] | None = None,
) -> None:
    """Reject wrappers, fat Mach-O, wrong CPU/type and truncated native headers."""
    with path.open("rb") as stream:
        if check_budget is not None:
            check_budget()
        header = stream.read(64)
        size = stream.seek(0, 2)
        if platform == "macos":
            cpu = {"aarch64": 0x0100000C, "x86_64": 0x01000007}.get(architecture)
            if len(header) < 32:
                raise ValueError("truncated thin Mach-O 64 header")
            magic, observed_cpu, _, kind, commands, command_bytes, _, _ = struct.unpack(
                "<8I", header[:32]
            )
            if magic != 0xFEEDFACF or cpu is None or observed_cpu != cpu:
                raise ValueError("requires target-matching thin little-endian Mach-O 64")
            if kind != (6 if dependency else 2):
                raise ValueError(
                    "requires Mach-O dylib" if dependency else "requires Mach-O executable"
                )
            if command_bytes > size - 32 or commands > command_bytes // 8:
                raise ValueError("truncated Mach-O load commands")
            stream.seek(32)
            consumed = 0
            for _ in range(commands):
                if check_budget is not None:
                    check_budget()
                command = stream.read(8)
                if len(command) != 8:
                    raise ValueError("truncated Mach-O load command")
                _, length = struct.unpack("<2I", command)
                if length < 8 or length % 8 or consumed + length > command_bytes:
                    raise ValueError("malformed Mach-O load command size")
                stream.seek(length - 8, 1)
                consumed += length
            if consumed != command_bytes:
                raise ValueError("Mach-O load command count/size disagreement")
        elif platform == "linux":
            if (
                architecture != "x86_64"
                or len(header) != 64
                or header[:7] != b"\x7fELF\x02\x01\x01"
            ):
                raise ValueError("requires x86_64 ELF64 little-endian header")
            kind, machine, version = struct.unpack("<HHI", header[16:24])
            if machine != 62 or version != 1 or kind not in ({3} if dependency else {2, 3}):
                raise ValueError(
                    "ELF architecture/version/executable or shared-library type mismatch"
                )
            if int.from_bytes(header[52:54], "little") != 64:
                raise ValueError("invalid ELF64 header size")
            # Validate table bounds as well as the complete fixed header. Full
            # dynamic imports, symbol versions and interpreter are release gates.
            for offset_at, size_at, count_at, minimum in ((32, 54, 56, 56), (40, 58, 60, 64)):
                offset = int.from_bytes(header[offset_at : offset_at + 8], "little")
                width = int.from_bytes(header[size_at : size_at + 2], "little")
                count = int.from_bytes(header[count_at : count_at + 2], "little")
                if count and (offset < 64 or width < minimum or offset + width * count > size):
                    raise ValueError("truncated or malformed ELF64 header table")
        else:
            raise ValueError("unsupported native header target")
        if check_budget is not None:
            check_budget()
