"""LSP peer that answers initialize, then never reads stdin again.

argv: <ready-file>. After the initialize reply the peer writes its PID to the
ready file and sleeps, so any adapter write larger than the pipe buffer blocks
inside a synchronous WriteFile until the process is terminated.
"""

import json
import os
import sys
import time
from pathlib import Path


def read_frame() -> dict:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise SystemExit(0)
        if line == b"\r\n":
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.strip().lower() == "content-length":
            length = int(value.strip())
    return json.loads(sys.stdin.buffer.read(length or 0))


def send(value: dict) -> None:
    data = json.dumps(value).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()


message = read_frame()
send(
    {
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {
            "capabilities": {
                "positionEncoding": "utf-16",
                "textDocumentSync": {"openClose": True, "change": 1},
            }
        },
    }
)
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
while True:
    time.sleep(3600)
