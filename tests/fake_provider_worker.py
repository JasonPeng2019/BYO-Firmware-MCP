"""Deterministic child process used only by process-isolation regression tests."""

from __future__ import annotations

import json
import os
import sys
import time

_TRACE = None


def send(payload: dict[str, object]) -> None:
    if _TRACE is not None:
        _TRACE.invoke("stdout.write", sys.stdout.write, json.dumps(payload, separators=(",", ":")) + "\n")
        _TRACE.invoke("stdout.flush", sys.stdout.flush)
        return
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    mode = sys.argv[1]
    if mode == "ready_hang":
        time.sleep(60)
        return
    if mode == "ready_then_hang":
        time.sleep(0.10)
    send({"version": 1, "ready": True})
    while line := sys.stdin.readline():
        request = json.loads(line)
        request_id = request["request_id"]
        if _TRACE is not None:
            _TRACE.record("request", request=request, raw_line=line)
        # These additional modes model the real runtime's persistent request
        # loop. The original reply-then-exit modes below deliberately stay abrupt.
        if mode in {"persistent_typed_error", "persistent_locked_error"} and request["operation"] != "close":
            locked = mode == "persistent_locked_error"
            send({"version": 1, "request_id": request_id, "ok": False,
                  "error": {"kind": "locked_target" if locked else "target_control",
                            "message": "fake locked target" if locked else "fake target error"}})
            continue
        if mode in {"open_hang", "close_hang", "partial_reply", "ready_then_hang"}:
            if mode == "partial_reply":
                sys.stdout.write('{"version":1')
                sys.stdout.flush()
            time.sleep(60)
            return
        if mode == "crash":
            raise SystemExit(7)
        if mode == "malformed_reply":
            sys.stdout.write("not-json\n")
            sys.stdout.flush()
            return
        if mode == "wrong_id":
            send({"version": 1, "request_id": request_id + 1, "ok": True, "result": "RUNNING"})
            return
        if mode == "typed_error":
            send(
                {
                    "version": 1,
                    "request_id": request_id,
                    "ok": False,
                    "error": {"kind": "target_control", "message": "fake target error"},
                }
            )
            return
        if mode == "locked_error":
            send(
                {
                    "version": 1,
                    "request_id": request_id,
                    "ok": False,
                    "error": {"kind": "locked_target", "message": "fake locked target"},
                }
            )
            return
        if request["operation"] == "close":
            send({"version": 1, "request_id": request_id, "ok": True, "result": None})
            return
        send({"version": 1, "request_id": request_id, "ok": True, "result": "RUNNING"})


if __name__ == "__main__":
    receipt = os.environ.get("BYO_FAKE_PROVIDER_RECEIPT")
    if receipt:
        from pathlib import Path
        from process_cleanup_observations import CleanupTrace, process_facts

        _TRACE = CleanupTrace("fake-provider-" + sys.argv[1], Path(receipt).parent)
        _TRACE.path = Path(receipt)
        _TRACE.record("helper-start", identity=process_facts(os.getpid()), argv=sys.argv)
        try:
            _TRACE.invoke("helper.main", main)
        finally:
            _TRACE.record("helper-finally", identity=process_facts(os.getpid()),
                          evidence_gaps=_TRACE.gaps,
                          boundary="function exit; parent wait establishes process exit")
    else:
        main()
