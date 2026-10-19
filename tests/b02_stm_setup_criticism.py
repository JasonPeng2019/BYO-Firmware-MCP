"""Read actual public STM FULL failure artifacts; no hardware or state mutation."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import yaml


def body(reply):
    return json.loads(reply.splitlines()[0])


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    root = args.evidence.resolve()
    directory = root / "physical-stm-04"
    full = json.loads((directory / "full.json").read_text())
    retry = json.loads((directory / "full-retry-2.json").read_text())
    restore = json.loads((directory / "restore.json").read_text())
    assert full["verdict"] == retry["verdict"] == "FAIL"
    assert restore["verdict"] == "PASS" and restore["cleanup_errors"] == []
    profile_path = directory / "full-project/.firm/boards/b02_physical_stm32l476.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    support = profile["device_support"]
    assert support["kind"] == "resolved_pack" and support["pdsc_device"] == "STM32L476RGTx"
    answer = next(c for c in full["calls"] if c["tool"] == "continue_setup")
    response = answer["arguments"]["response"]
    required = {"pack_id", "version", "filename", "url", "source_path", "official_sha256", "evidence", "reasoning_summary"}
    assert set(response) == required
    assert body(answer["reply"])["status"] == "setup_continuation_accepted"
    assert sha(Path(response["source_path"])) == response["official_sha256"] == support["pack_sha256"]
    populated = next(c for c in full["calls"] if c["tool"] == "board_setup-plan" and c["arguments"].get("action_parameters"))
    assert populated["arguments"]["action_parameters"]["target_tier"] == "setup-full"
    batch = body(full["calls"][-1]["reply"])
    pending = batch["failure"]["result"]
    pending = body(pending if isinstance(pending, str) else pending[0]["text"])
    assert pending["status"] == "setup_validation_failed" and pending["first_unverified_phase"] == "validation"
    assert "worker did not reply before the deadline" in pending["agent_prompt"]
    opens = [r for r in full["rpc"] if r["operation"] == "open"]
    assert opens
    for row in opens:
        parameters = row["arguments"]
        assert parameters["target"] == support["pyocd_target"]
        assert parameters["pdsc_device"] == support["pdsc_device"]
        assert parameters["pack_sha256"] == support["pack_sha256"]
        assert sha(Path(parameters["pack_path"])) == support["pack_sha256"]
    retry_opens = [r for r in retry["rpc"] if r["operation"] == "open"]
    assert retry_opens and all(r["arguments"]["target"] is None for r in retry_opens)
    assert restore["outside_sector"]["actual_sha256"] == sha(directory / "sector-original.bin")
    record = dict(at=datetime.now(timezone.utc).isoformat(), criticism_id="B02-STM-FULL-01",
                  status="NEEDS_ROOT_TRIAGE", public_plan=populated, public_answer=answer,
                  actual_failure=pending, profile_path=str(profile_path), profile_sha256=sha(profile_path),
                  persisted_device_support=support, actual_worker_opens=opens,
                  retry_qualification="The incomplete-policy raw reconnect omitted explicit target. Its null target/pack inputs and unrecognized-target error are a client-selection error, not a production criticism.",
                  evidence_qualification="Fresh full04 had no pre-staged pack registry/config pack entry; the public official-pack response installed this binding. Earlier full01/full02/full03 are separate failed client fixtures.",
                  restored_sector=restore["outside_sector"],
                  root_action_needed="Triage fresh public FULL validation timeout with correct pack inputs; provide a supported recovery route or authorize the necessary prerequisite repair. Required STM M06 symbol equality and M13 verified deployment/breakpoint remain unexecuted. No source change or private worker-argument patch by B02.")
    output = root / "stm-full-prerequisite-criticism.json"
    assert not output.exists()
    output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(dict(status=record["status"], criticism_id=record["criticism_id"],
                         worker_opens=len(opens), restored=True)))


if __name__ == "__main__":
    main()
