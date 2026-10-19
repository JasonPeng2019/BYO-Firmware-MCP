"""Fresh process M06 public revalidation and explicit-ELF contract."""

from pathlib import Path
import sys
from unittest.mock import patch

from tests.b02_public_support import ByteProbe, RAM_BYTES
from tests.m05_public_support import PublicSetup
from tests.test_b02_m06_symbols import scalar
from tests.test_m05_lite_validation import advertised_validation
from tests.test_tiered_acceptance_lite import _setup_inventory
from tests.tiered_acceptance_support import tiered_test_server


def main():
    root, board, elf = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    with tiered_test_server(root) as server:
        backend = ByteProbe()
        server.configure_tiered_test_seams(backend=backend)
        with patch.object(server, "_hardware_inventory", _setup_inventory()):
            flow = PublicSetup(server)
            flow.overview("B02 independent symbols")
            flow.call("connect", {"board_id": board})
            try:
                args = {"board_id": board, "symbol": "word", "elf_artifact": str(elf)}
                before = list(backend.calls)
                try:
                    flow.call("read_memory_symbol", args)
                except Exception as exc:
                    assert "not validated" in str(exc), str(exc)
                else:
                    raise AssertionError("Validation must not survive a fresh server")
                assert backend.calls == before
                assert advertised_validation(flow, board)["status"] == "validation_passed"
                assert "memory/symbol-artifact-unavailable" in flow.call("read_memory_symbol", {"board_id": board, "symbol": "word"})
                assert scalar(flow.call("read_memory_symbol", args)) == int.from_bytes(RAM_BYTES[:4], "little")
            finally:
                flow.call("disconnect", {"board_id": board})


if __name__ == "__main__":
    main()
