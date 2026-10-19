"""Offline independent M17 image/document qualification; never opens hardware."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import zipfile

from capstone import Cs, CS_ARCH_ARM, CS_MODE_LITTLE_ENDIAN, CS_MODE_MCLASS, CS_MODE_THUMB
from elftools.elf.elffile import ELFFile
from intelhex import IntelHex


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qualify(path):
    """Parse every addressed load and every defined function independently."""
    manifest = json.loads((path.parent / "fixture-manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert sha(path.parent / name) == digest, name
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        assert elf.header.e_machine == "EM_ARM" and elf.little_endian
        loads = [
            (int(s["p_paddr"]), s.data())
            for s in elf.iter_segments()
            if s["p_type"] == "PT_LOAD" and s["p_filesz"]
        ]
        addressed = {a + i: b for a, data in loads for i, b in enumerate(data)}
        reference = IntelHex(str(path.with_suffix(".hex")))
        assert addressed == reference.todict()
        low, high = min(addressed), max(addressed) + 1
        assert (
            bytes(addressed.get(a, 255) for a in range(low, high))
            == path.with_suffix(".bin").read_bytes()
        )
        sp, entry = struct.unpack("<II", bytes(addressed[a] for a in range(low, low + 8)))
        ram_end = manifest.get("ram_window", [0x20000000, 0x20010000])[1]
        assert 0x20000000 < sp <= ram_end and sp % 8 == 0
        assert sp == manifest["initial_sp"]
        assert entry & 1 and entry == int(elf.header.e_entry)
        assert low <= entry & ~1 < high
        bounds = manifest.get("flash_window", [low, high])
        assert all(bounds[0] <= a < bounds[1] for a in addressed)
        decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS | CS_MODE_LITTLE_ENDIAN)
        table = elf.get_section_by_name(".symtab")
        symbols = {s.name: int(s["st_value"]) for s in table.iter_symbols() if s.name}
        functions = {}
        for symbol in table.iter_symbols():
            size = int(symbol["st_size"])
            if symbol["st_info"]["type"] != "STT_FUNC" or not size:
                continue
            start = int(symbol["st_value"]) & ~1
            data = bytes(addressed[a] for a in range(start, start + size))
            functions[symbol.name] = [
                dict(address=i.address, mnemonic=i.mnemonic, operands=i.op_str)
                for i in decoder.disasm(data, start)
            ]
            assert functions[symbol.name]
        if path.parent.name == "bootloader":
            assert [(i["mnemonic"], i["operands"]) for i in functions["jump_application"]] == [
                ("msr", "msp, r0"),
                ("bx", "r1"),
            ]
        return dict(
            path=str(path),
            sha256=sha(path),
            bounds=bounds,
            sp=sp,
            entry=entry,
            addressed_bytes=len(addressed),
            loads=[
                dict(start=a, bytes=len(d), sha256=hashlib.sha256(d).hexdigest()) for a, d in loads
            ],
            sections=[
                dict(
                    name=s.name,
                    address=int(s["sh_addr"]),
                    size=int(s["sh_size"]),
                    type=s["sh_type"],
                )
                for s in elf.iter_sections()
            ],
            symbols=symbols,
            functions=functions,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    firmware = args.execution / "firmware"
    paths = [
        firmware / "extended" / board / role / "fixture.elf"
        for board in ("nrf52840", "stm32l476")
        for role in ("bootloader", "boot-application", "uart-application")
    ]
    paths += [firmware / board / "fixture.elf" for board in ("nrf52840", "stm32l476")]
    images = [qualify(path) for path in paths]
    documents = []
    docs = args.execution / "vendor-documents"
    manual = docs / "m17-reference-retrieval-20261017T1200Z/STM32-RM0351-third-party-mirror.pdf"
    for path, pages, qualification in (
        (
            docs / "nRF52840-PS-v1.1.pdf",
            [1, 31, 32, 33, 34, 35],
            "Nordic v1.1 older cached publication",
        ),
        (
            docs / "STM32L476xx-DS10198.pdf",
            [1, 103, 107, 261],
            "ST DS10198 Rev11 cached publication",
        ),
        (
            manual,
            [1, 1827, 1853, 1854],
            "ST-authored RM0351 Rev6 April2018 from RT-Thread mirror; no latest/vendor-origin authentication",
        ),
    ):
        texts = {}
        for page in pages:
            command = ["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(path), "-"]
            process = subprocess.run(command, capture_output=True, check=True)
            text = process.stdout.decode("utf-8", errors="replace")
            target = args.output / (path.stem + f"-page-{page}.txt")
            target.write_text(text, encoding="utf-8")
            texts[str(page)] = dict(
                path=str(target), sha256=sha(target), argv=command, raw_exit=process.returncode
            )
        documents.append(
            dict(
                path=str(path),
                bytes=path.stat().st_size,
                sha256=sha(path),
                qualification=qualification,
                pages=texts,
            )
        )
    inputs = set(paths)
    for path in paths:
        inputs.update(path.parent.iterdir())
    inputs.update(
        [
            firmware / name
            for name in (
                "bootloader_fixture.c",
                "uart_fixture.c",
                "fixture.c",
                "build_fixtures.py",
                "build_extended_fixtures.py",
            )
        ]
    )
    inputs.update(Path(row["path"]) for row in documents)
    rows = []
    with zipfile.ZipFile(
        args.output / "qualified-inputs.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for i, path in enumerate(sorted(inputs)):
            if not path.is_file():
                continue
            member = f"{i:03}-{path.name}"
            archive.write(path, member)
            rows.append(
                dict(path=str(path), member=member, bytes=path.stat().st_size, sha256=sha(path))
            )
    record = dict(
        images=images,
        documents=documents,
        inputs=rows,
        qualification="Offline parsed/disassembled inputs only, no hardware or product authority.",
    )
    (args.output / "audit.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(
        json.dumps(
            dict(
                images=len(images),
                documents=len(documents),
                inputs=len(rows),
                audit=str(args.output / "audit.json"),
            )
        )
    )


if __name__ == "__main__":
    main()
