"""Verify retained M12 command inputs and available lane/prior-source imports."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    product = Path(__file__).resolve().parents[1]
    report = dict(
        at=datetime.now(timezone.utc).isoformat(),
        commands=[],
        snapshots=0,
        checked_imports=0,
        external_imports=0,
        errors=[],
    )
    for folder in sorted(evidence.iterdir()):
        result_path = folder / "result.json"
        if not folder.is_dir() or not result_path.is_file():
            continue
        record = json.loads(result_path.read_text())
        index = {}
        if (folder / "inputs.zip").exists():
            assert digest((folder / "inputs.zip").read_bytes()) == record["inputs_zip_sha256"]
            with zipfile.ZipFile(folder / "inputs.zip") as archive:
                for row in record["inputs"]:
                    assert digest(archive.read(row["member"])) == row["sha256"], row
                    index[str(Path(row["path"]).resolve()).casefold()] = row["sha256"]
        else:
            for row in record["inputs"]:
                assert digest(Path(row["snapshot"]).read_bytes()) == row["sha256"], row
                index[str(Path(row["path"]).resolve()).casefold()] = row["sha256"]
        report["snapshots"] += len(index)
        prior = folder / "case/prior-source.zip"
        if prior.exists():
            prior_product = folder / "case/archive/MCP_Server_Dev/BYO-Firmware-MCP"
            with zipfile.ZipFile(prior) as archive:
                for member in archive.namelist():
                    if member.endswith("/"):
                        continue
                    relative = Path(member).relative_to("MCP_Server_Dev/BYO-Firmware-MCP")
                    index[str((prior_product / relative).resolve()).casefold()] = digest(
                        archive.read(member)
                    )
            for row in record["inputs"]:
                source = Path(row["path"])
                relative = source.relative_to(product)
                if relative.parts[0] == "tests":
                    index[str((prior_product / relative).resolve()).casefold()] = row["sha256"]
        for name in ("stdout", "stderr"):
            assert digest((folder / (name + ".log")).read_bytes()) == record[name + "_sha256"]
        imports = list((folder / "imports").glob("*.json"))
        imports += list((folder / "managed").rglob("imports.json"))
        for path in imports:
            for row in json.loads(path.read_text()):
                actual = str(Path(row["path"]).resolve()).casefold()
                if row["module"].startswith(("pyocd_debug_mcp", "tests.")):
                    assert actual in index, (folder.name, row)
                    assert index[actual] == row["sha256"], (folder.name, row)
                    report["checked_imports"] += 1
                else:
                    report["external_imports"] += 1
        text = (folder / "stderr.log").read_text(encoding="utf-8", errors="replace")
        count = re.search(r"Ran (\d+) tests? in ([\d.]+)s", text)
        report["commands"].append(
            dict(
                label=folder.name,
                raw_exit=record.get("raw_exit", record.get("exit_code")),
                argv=record["argv"],
                started_at=record["started_at"],
                finished_at=record["finished_at"],
                tests=int(count[1]) if count else None,
                seconds=float(count[2]) if count else None,
                unittest_tail=text[text.rfind("\nRan ") :].strip() if count else None,
                import_observation_files=len(imports),
            )
        )
    report["verified"] = True
    (evidence / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            dict(
                verified=True,
                commands=len(report["commands"]),
                snapshots=report["snapshots"],
                checked_imports=report["checked_imports"],
                external_imports=report["external_imports"],
            )
        )
    )


if __name__ == "__main__":
    main()
