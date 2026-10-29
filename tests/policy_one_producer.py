"""Produce a genuine old map from the complete, unmodified policy-1 source.

Actions only. The child reads the registered pack/profile/datasheet from the
test's private project. An empty separate compatibility-map repository prevents
the current map from becoming an input to the historical producer. No device is
opened and no current product module may satisfy an old-source import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Historical producer executes only in GitHub Actions")
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    fixture = Path(__file__).parent / "fixtures/policy-one-producer"
    provenance = json.loads((fixture / "PROVENANCE.json").read_text(encoding="utf-8"))
    source = args.output / "source"
    archive = fixture / "source.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == provenance["archive"]["sha256"]
    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == sorted(row["path"] for row in provenance["files"])
        for row in provenance["files"]:
            relative = Path(row["path"])
            assert not relative.is_absolute() and ".." not in relative.parts
            raw = bundle.read(row["path"])
            assert len(raw) == row["bytes"]
            assert hashlib.sha256(raw).hexdigest() == row["sha256"]
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
    sys.path.insert(0, str(source))
    os.environ["BYO_MCP_ARTIFACT_ROOT"] = str(args.project.resolve(strict=True))
    os.environ["BYO_MCP_MONITOR_ROOT"] = str(args.output / "monitor")
    from pyocd_debug_mcp import server
    from pyocd_debug_mcp.firmstore.store import FirmStore
    from pyocd_debug_mcp.safety.map_build import SafetyMapRepository

    try:
        profile_document = json.loads(args.profile.read_text(encoding="utf-8"))
        board_id = profile_document["board_id"]
        profile = server._profile_repository.from_snapshot(board_id, profile_document)
        server._safety_repository = SafetyMapRepository(FirmStore(args.output / "empty-prior"))
        document = server._derive_safety_map(board_id, profile=profile)
        imports = {}
        for name, module in tuple(sys.modules.items()):
            if name == "pyocd_debug_mcp" or name.startswith("pyocd_debug_mcp."):
                path = Path(module.__file__).resolve(strict=True)
                assert path.is_relative_to(source.resolve(strict=True)), (name, path)
                relative = path.relative_to(source).as_posix()
                expected = next(row for row in provenance["files"] if row["path"] == relative)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                assert actual == expected["sha256"], name
                imports[name] = {"path": relative, "sha256": actual}
        (args.output / "produced.json").write_text(
            json.dumps({"document": document.to_document(), "imports": imports,
                        "native_source": provenance["native_commit"],
                        "source_archive_sha256": provenance["archive"]["sha256"]}, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        server._monitor.closeout("original-policy-one-producer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
