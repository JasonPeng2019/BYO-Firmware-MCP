"""Actions-only ARM input preparation for installed workflow checks."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARM_CORPUS = ROOT / "tests/code_analysis_fixtures/arm_firmware"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_arm_corpus(gnu, destination, facts):
    """Actually build the frozen ARM corpus; export only argv that really ran.

    The template fixes the captured argv form. Its machine-local prefixes bind
    to this owned copy and the receipt-admitted toolchain, then every compile
    and the vector/linker-script link execute before any database is written.
    """
    shutil.copytree(ARM_CORPUS, destination, ignore=shutil.ignore_patterns("*.template.json"))
    project = destination.resolve(strict=True)

    def bind(value):
        if isinstance(value, dict):
            return {key: bind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [bind(item) for item in value]
        if not isinstance(value, str):
            return value
        for token, root in (("@PROJECT", project), ("@ARM_GNU", gnu)):
            if f"{token}_NATIVE@" in value:
                value = str(root) + value.split(f"{token}_NATIVE@", 1)[1].replace("\\", os.sep)
            value = value.replace(f"{token}@", root.as_posix())
        return value if os.name == "nt" else value.replace(".exe", "")

    def run(argv):
        completed = subprocess.run(  # noqa: S603 - receipt-admitted toolchain
            argv, cwd=project, capture_output=True, text=True, timeout=300, check=False
        )
        facts["steps"].append(
            {
                "argv": argv,
                "exit": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        if completed.returncode != 0:
            raise RuntimeError("Arm GCC step failed: " + " ".join(argv))

    template = json.loads((ARM_CORPUS / "build/compile_commands.template.json").read_bytes())
    executed = [{key: bind(value) for key, value in entry.items()} for entry in template]
    for entry in executed:
        run(entry["arguments"])
    elf = project / "build/firmware.elf"
    run(
        [
            executed[0]["arguments"][0],
            "-mcpu=cortex-m4",
            "-mthumb",
            "-mfloat-abi=soft",
            "-nostdlib",
            "-Wl,-Map," + str(project / "build/firmware.map"),
            "-T",
            str(project / "firmware.ld"),
            *(entry["output"] for entry in executed),
            "-o",
            str(elf),
        ]
    )
    header = elf.read_bytes()[:20]
    # ELFCLASS32, little-endian, EM_ARM: the linked image is the selected target.
    if header[:6] != b"\x7fELF\x01\x01" or int.from_bytes(header[18:20], "little") != 40:
        raise RuntimeError("Linked image is not a 32-bit little-endian ARM ELF")
    database = project / "build/compile_commands.json"
    database.write_text(json.dumps(executed, indent=2) + "\n", encoding="utf-8")
    configuration = bind(json.loads((ARM_CORPUS / "byo-analysis.template.json").read_bytes()))
    (project / "byo-analysis.json").write_text(
        json.dumps(configuration, indent=2) + "\n", encoding="utf-8"
    )
    facts.update(
        outcome="PASS",
        corpus=str(project),
        database_sha256=digest(database),
        elf_sha256=digest(elf),
        map_sha256=digest(project / "build/firmware.map"),
        objects={entry["output"]: digest(entry["output"]) for entry in executed},
    )
