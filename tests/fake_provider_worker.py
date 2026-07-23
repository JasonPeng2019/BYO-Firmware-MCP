"""Deterministic child process used only by process-isolation regression tests."""

from __future__ import annotations

import json
import sys
import time


def send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    mode = sys.argv[1]
    if mode == "ready_hang":
        time.sleep(60)
        return
    if mode == "ready_then_hang":
        time.sleep(0.10)
    send({"version": 4, "ready": True})
    while line := sys.stdin.readline():
        request = json.loads(line)
        request_id = request["request_id"]
        if mode in {"open_hang", "close_hang", "partial_reply", "ready_then_hang"}:
            if mode == "partial_reply":
                sys.stdout.write('{"version":4')
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
            send({"version": 4, "request_id": request_id + 1, "ok": True, "result": "RUNNING"})
            return
        if mode == "typed_error":
            send(
                {
                    "version": 4,
                    "request_id": request_id,
                    "ok": False,
                    "error": {"kind": "target_control", "message": "fake target error"},
                }
            )
            return
        if mode == "locked_error":
            send(
                {
                    "version": 4,
                    "request_id": request_id,
                    "ok": False,
                    "error": {"kind": "locked_target", "message": "fake locked target"},
                }
            )
            return
        if mode == "cleanup_error":
            send(
                {
                    "version": 4,
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "kind": "target_connection_cleanup",
                        "message": "worker preserved cleanup uncertainty",
                        "primary": {"type": "RuntimeError", "message": "open failed"},
                        "cleanup_diagnostics": [
                            {
                                "stage": "reset_release",
                                "status": "unconfirmed",
                                "error_type": "OSError",
                                "error_message": "release denied",
                                "recovery": "Disconnect, power-cycle, reconnect, and revalidate.",
                            },
                            {
                                "stage": "session_close",
                                "status": "unconfirmed",
                                "error_type": "OSError",
                                "error_message": "close denied",
                                "recovery": "Disconnect, power-cycle, reconnect, and revalidate.",
                            },
                        ],
                    },
                }
            )
            return
        if mode == "malformed_cleanup_error":
            send(
                {
                    "version": 4,
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "kind": "target_connection_cleanup",
                        "message": "bad cleanup",
                        "primary": {"type": "RuntimeError", "message": "open failed"},
                        "cleanup_diagnostics": [{"stage": "session_close"}],
                    },
                }
            )
            return
        if request["operation"] == "close":
            send({"version": 4, "request_id": request_id, "ok": True, "result": None})
            return
        send({"version": 4, "request_id": request_id, "ok": True, "result": "RUNNING"})


if __name__ == "__main__":
    main()
