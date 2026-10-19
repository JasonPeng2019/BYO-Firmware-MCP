"""Abrupt public recovery/refresh child; only filesystem fault boundaries vary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--point", required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--action", choices=("restore", "deploy"), default="restore")
    args = parser.parse_args()
    os.environ.update(
        BYO_MCP_ARTIFACT_ROOT=str(args.project),
        PYOCD_PROJECT_DIR=str(args.project),
        BYO_MCP_TIER_FAULT=args.point,
        BYO_MCP_TEST_FAULTS="1",
    )
    from tests.tiered_acceptance_support import tiered_test_server
    from tests.m05_public_support import PublicSetup

    with tiered_test_server(args.project) as server:
        imports = []
        for name, module in sorted(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if name.startswith(("pyocd_debug_mcp", "tests.")) and file and Path(file).is_file():
                data = Path(file).read_bytes()
                imports.append(
                    dict(
                        module=name,
                        path=str(Path(file).resolve()),
                        bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                )
        args.proof.write_text(
            json.dumps(
                dict(
                    pid=os.getpid(),
                    argv=sys.argv,
                    cwd=str(Path.cwd()),
                    run_id=server.server_run.run_id,
                    imports=imports,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
        flow = PublicSetup(server)
        real_write = server._firm_store.atomic_write_json

        def interrupted_write(path, payload):
            selected = Path(path)
            if args.point == "before_profile_projection" and selected.parent.name == "boards":
                os._exit(86)
            if args.point == "before_map_projection" and selected.name == "memory_map.yaml":
                os._exit(86)
            return real_write(path, payload)

        if args.action == "deploy":
            from tests.b02_public_support import elf_fixture
            from tests.m14_independent_support import EraseProbe
            from tests.m05_public_support import NULL_PLAN
            from tests.test_m05_lite_validation import advertised_validation
            from tests.test_tiered_acceptance_lite import _setup_inventory

            server.configure_tiered_test_seams(backend=EraseProbe(args.project, "nrf52840"))
            with patch.object(server, "_hardware_inventory", _setup_inventory()):
                flow.overview("Independent M14 generic erase")
                assert advertised_validation(flow, args.board)["status"] == "validation_passed"
                # A second physical flash sector makes the real public deployment
                # extend its existing owned allocation and publish a different
                # generation. Reprogramming within an owned sector publishes none.
                artifact = elf_fixture(args.project / "alternate.elf", base=0x1000)
                flow.call("flash_application-plan", NULL_PLAN)
                plan = flow.plan("flash_application", args.board, dict(artifact=str(artifact)))
                assert plan["status"] == "plan_accepted", plan
                reply = flow.call(
                    "flash_application", dict(board_id=args.board, artifact=str(artifact))
                )
                raise AssertionError(f"Publication fault was not reached: {reply}")
        else:
            flow.json(
                "load_setup_tool", dict(board_id=args.board, tool_name="board_policy_restore")
            )
            with patch.object(server._firm_store, "atomic_write_json", interrupted_write):
                flow.json(
                    "board_policy_restore",
                    dict(board_id=args.board, generation_backup_path=str(args.backup)),
                )
    raise AssertionError("The requested abrupt boundary did not terminate the public call")


if __name__ == "__main__":
    main()
