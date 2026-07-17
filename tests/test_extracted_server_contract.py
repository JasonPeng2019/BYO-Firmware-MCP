from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).parent / "contracts" / "source-server-tools.json"


def _source_contract() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_extraction_named_snapshot_remains_well_formed_historical_evidence() -> None:
    snapshot = _source_contract()

    assert snapshot["contract_version"] == 17
    assert snapshot["tool_contract_sha256"]
    assert snapshot["implementation_module_sha256"]
    for digest in (
        *snapshot["tool_contract_sha256"].values(),
        *snapshot["implementation_module_sha256"].values(),
    ):
        assert len(digest) == 64
        int(digest, 16)
    assert set(snapshot["excluded_tools"]).isdisjoint(
        snapshot["tool_contract_sha256"]
    )


def test_historical_snapshot_paths_still_name_real_implementation_owners() -> None:
    snapshot = _source_contract()
    for relative_path in snapshot["implementation_module_sha256"]:
        assert (FIXTURE_PATH.parents[2] / relative_path).is_file()
