"""Read-only OS verification after a physical attempt, without target discovery."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import psutil

from pyocd_debug_mcp.kernel.processes import identity_matches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--physical", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    physical = json.loads(args.physical.read_text())
    endpoint = physical["endpoint"]
    rows = [endpoint] + physical["workers"] + physical.get("actual_child_exits", [])
    seen = set()
    checked = []
    for row in rows:
        key = row["pid"], row["token"]
        if key in seen:
            continue
        seen.add(key)
        checked.append(
            dict(
                pid=row["pid"], token=row["token"], exact_identity_absent=not identity_matches(*key)
            )
        )
    port = int(endpoint["selector"].rsplit(":", 1)[1])
    listeners = [
        c._asdict()
        for c in psutil.net_connections(kind="tcp")
        if c.status == psutil.CONN_LISTEN and c.laddr.port == port
    ]
    result = dict(
        at=datetime.now(timezone.utc).isoformat(),
        physical=str(args.physical.resolve()),
        identities=checked,
        port=port,
        listeners=listeners,
        qualification="Fresh OS absence check. Does not reconstruct unavailable historical exits or change the original failed command.",
    )
    result["clean"] = (
        bool(checked) and all(r["exact_identity_absent"] for r in checked) and not listeners
    )
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
