"""Copy sealed genuine FULL setup metadata, excluding all live/run authority."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    prior = args.prior.resolve()
    manifest = json.loads((prior / "manifest.json").read_text())
    sealed = {row["path"]: row for row in manifest["files"]}
    proof = prior / "physical-04/live/physical.json"
    assert sha(proof) == sealed[proof.relative_to(prior).as_posix()]["sha256"]
    physical = json.loads(proof.read_text())
    assert physical["setup_result"]["status"] == "setup_completed"
    source = Path(physical["project"]).resolve()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    paths = [source / "pyocd.yaml"]
    for directory in ("boards", "capabilities", "evidence", "packs", "safety"):
        paths.extend(p for p in (source / ".firm" / directory).rglob("*") if p.is_file())
    copied = []
    for path in sorted(paths):
        relative = path.relative_to(source)
        expected = sealed[path.relative_to(prior).as_posix()]
        assert sha(path) == expected["sha256"] and path.stat().st_size == expected["bytes"]
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        assert sha(target) == sha(path)
        copied.append(
            dict(
                source=str(path),
                destination=str(target),
                sha256=sha(target),
                bytes=target.stat().st_size,
            )
        )
    reference = prior / "physical-04/live/normal-profile-original.bin"
    assert sha(reference) == sealed[reference.relative_to(prior).as_posix()]["sha256"]
    profile = destination / ".firm/boards/m12_owned_stm_remote_recovery.yaml"
    assert profile.read_bytes() == reference.read_bytes()
    assert not (destination / ".agent-workspace").exists()
    assert not any(
        (destination / ".firm" / name).exists() for name in ("runs", "setup", "validation")
    )
    report = dict(
        at=datetime.now(timezone.utc).isoformat(),
        source=str(source),
        destination=str(destination),
        genuine_full_proof=str(proof),
        genuine_full_sha256=sha(proof),
        prior_manifest_sha256=sha(prior / "manifest.json"),
        profile_sha256=sha(profile),
        copied=copied,
        qualification="Only sealed durable setup metadata copied. No grants, run/session records, validation reports, readiness or remote registrations copied; live validation must be fresh.",
    )
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(dict(copied=len(copied), profile_sha256=sha(profile), genuine_public_full=True))
    )


if __name__ == "__main__":
    main()
