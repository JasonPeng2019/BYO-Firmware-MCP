"""Small LSP peer for error replies and negotiated Unicode coordinates.

Normal analysis uses real clangd. This peer supplies only deterministic protocol
responses which a native analyzer cannot reliably be made to emit on demand.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def receive():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, value = line.split(b":", 1)
        headers[name.strip().lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers[b"content-length"])))


def send(message):
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()


def expand(value, request):
    if value == "$request_uri":
        return request["params"]["textDocument"]["uri"]
    if value == "$request_position":
        return request["params"]["position"]
    if isinstance(value, dict):
        return {key: expand(item, request) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, request) for item in value]
    return value


def main():
    scenario = json.loads(
        Path(sys.argv[sys.argv.index("--scenario") + 1]).read_text(encoding="utf-8")
    )

    def record(event, **fields):
        with Path(scenario["log"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid(), **fields}) + "\n")

    if "--version" in sys.argv:
        record("version_probe")
        print("clangd version 23.1.0-fake (code-analysis contract peer)")
        return
    record("start")
    while (request := receive()) is not None:
        record("received", message=request)
        method = request.get("method")
        if method == "exit":
            return
        if "id" not in request:
            continue
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            response["result"] = {
                "capabilities": {
                    "positionEncoding": scenario.get("position_encoding") or "utf-16",
                    "textDocumentSync": 2,
                    "definitionProvider": True,
                    "referencesProvider": True,
                }
            }
        elif method == "shutdown":
            response["result"] = None
        else:
            response.update(
                expand(
                    scenario.get("responses", {}).get(
                        method,
                        {
                            "error": {"code": -32603, "message": "boom"},
                        },
                    ),
                    request,
                )
            )
        send(response)


if __name__ == "__main__":
    main()
