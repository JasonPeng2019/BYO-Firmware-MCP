"""Small standards-layout ELF32 ARM images, serialized without a compiler."""

from __future__ import annotations

from dataclasses import dataclass
import struct


UNDEF, TEXT, DATA, BSS = 0, 1, 2, 3
ABS, COMMON = 0xFFF1, 0xFFF2
LOCAL, GLOBAL, WEAK = 0, 1, 2
NOTYPE, OBJECT, FUNC, SECTION, FILE = 0, 1, 2, 3, 4


@dataclass(frozen=True)
class ElfSymbol:
    name: str
    address: int
    size: int = 4
    type: int = OBJECT
    binding: int = LOCAL
    section: int = DATA


def elf32_symbols(
    symbols: list[ElfSymbol],
    *,
    dynamic: list[ElfSymbol] | None = None,
    symtab: bool = True,
    symtab_name: str = ".symtab",
) -> bytes:
    """Serialize ET_EXEC/EM_ARM, load segments, sections, strings and symbols.

    Table index zero is the required null symbol. Locals precede nonlocals,
    sh_info identifies the first nonlocal, and each table links its own strings.
    Section indexes TEXT/DATA/BSS remain constant across table combinations.
    """

    image = bytearray(52 + 2 * 32)
    # name, type, flags, address, offset, size, link, info, alignment, entry size
    sections = [("", 0, 0, 0, 0, 0, 0, 0, 0, 0)]

    def append(data: bytes, alignment: int = 4) -> int:
        image.extend(b"\0" * (-len(image) % alignment))
        offset = len(image)
        image.extend(data)
        return offset

    text = b"\x70\x47" * 256  # Thumb bx lr; bytes only, never executed.
    text_offset = append(text)
    data = b"\0" * 512
    data_offset = append(data)
    sections.extend(
        [
            (".text", 1, 6, 0x08000000, text_offset, len(text), 0, 0, 4, 0),
            (".data", 1, 3, 0x20000000, data_offset, len(data), 0, 0, 4, 0),
            (".bss", 8, 3, 0x20000200, len(image), 64, 0, 0, 4, 0),
        ]
    )

    def table(entries: list[ElfSymbol], name: str, table_type: int, string_name: str) -> None:
        strings = bytearray(b"\0")
        encoded = bytearray(16)
        ordered = [item for item in entries if item.binding == LOCAL] + [
            item for item in entries if item.binding != LOCAL
        ]
        for item in ordered:
            name_offset = len(strings)
            strings.extend(item.name.encode("utf-8") + b"\0")
            encoded.extend(
                struct.pack(
                    "<IIIBBH",
                    name_offset,
                    item.address,
                    item.size,
                    (item.binding << 4) | item.type,
                    0,
                    item.section,
                )
            )
        string_index = len(sections)
        sections.append((string_name, 3, 0, 0, append(bytes(strings), 1), len(strings), 0, 0, 1, 0))
        first_nonlocal = 1 + sum(item.binding == LOCAL for item in ordered)
        sections.append(
            (
                name,
                table_type,
                0,
                0,
                append(bytes(encoded)),
                len(encoded),
                string_index,
                first_nonlocal,
                4,
                16,
            )
        )

    if symtab:
        table(symbols, symtab_name, 2, ".strtab")
    if dynamic is not None:
        table(dynamic, ".dynsym", 11, ".dynstr")
    shstrings = bytearray(b"\0")
    shnames = []
    for section in sections[1:]:
        shnames.append(len(shstrings))
        shstrings.extend(section[0].encode("ascii") + b"\0")
    shstr_name = len(shstrings)
    shstrings.extend(b".shstrtab\0")
    shstr_index = len(sections)
    sections.append((".shstrtab", 3, 0, 0, append(bytes(shstrings), 1), len(shstrings), 0, 0, 1, 0))
    image.extend(b"\0" * (-len(image) % 4))
    shoff = len(image)
    for name_offset, section in zip([0, *shnames, shstr_name], sections):
        image.extend(struct.pack("<10I", name_offset, *section[1:]))
    ident = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    image[:52] = struct.pack(
        "<16sHHIIIIIHHHHHH",
        ident,
        2,
        40,
        1,
        0x08000001,
        52,
        shoff,
        0x05000000,
        52,
        32,
        2,
        40,
        len(sections),
        shstr_index,
    )
    image[52:84] = struct.pack(
        "<8I", 1, text_offset, 0x08000000, 0x08000000, len(text), len(text), 5, 4
    )
    image[84:116] = struct.pack(
        "<8I", 1, data_offset, 0x20000000, 0x20000000, len(data), len(data) + 64, 6, 4
    )
    return bytes(image)


def duplicate_counter_elf() -> bytes:
    return elf32_symbols([ElfSymbol("counter", 0x20000000), ElfSymbol("counter", 0x20000004)])
