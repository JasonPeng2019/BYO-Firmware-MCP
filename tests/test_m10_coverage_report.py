from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "docs/evidence/m10-software-boundary-2026-07-17.json"
PERFORMANCE_PATH = PROJECT_ROOT / "docs/evidence/m10-performance-2026-07-17.json"
PLAN_PATH = PROJECT_ROOT / "Implementation_Plan.md"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_m10_report_covers_every_ac_and_cc_exactly_once() -> None:
    report = _load(REPORT_PATH)
    expected_ac = set(re.findall(r"AC-\d+\.\d+", PLAN_PATH.read_text(encoding="utf-8")))
    rows = report["ac_coverage"]
    assert isinstance(rows, list)
    actual_ac = [criterion for row in rows for criterion in row["criterion_ids"]]

    assert len(expected_ac) == 122
    assert len(actual_ac) == len(set(actual_ac))
    assert set(actual_ac) == expected_ac

    cc_rows = report["cc_coverage"]
    assert isinstance(cc_rows, list)
    actual_cc = [row["id"] for row in cc_rows]
    assert actual_cc == [f"CC-{index}" for index in range(1, 23)]


def test_m10_report_leaves_only_hardware_or_manual_task20_items() -> None:
    report = _load(REPORT_PATH)
    scope = report["scope"]
    assert scope == {
        "complete_pytest_run": "deferred_to_task_20",
        "whole_repository_ruff": "deferred_to_task_20",
        "whole_repository_pyright": "deferred_to_task_20",
        "hardware_actions_performed": False,
        "product_behavior_changed": False,
    }

    remaining = report["task20_remaining"]
    assert isinstance(remaining, list) and remaining
    for item in remaining:
        item_type = item["type"]
        assert isinstance(item_type, str)
        assert item_type.startswith("hardware") or item_type in {
            "simultaneous_hardware",
            "manual_procedural",
        }
        assert item["remaining_evidence"]


def test_m10_report_preserves_recorded_performance_maxima() -> None:
    report = _load(REPORT_PATH)
    performance = _load(PERFORMANCE_PATH)
    report_metrics = report["performance"]["metrics"]
    measured_metrics = performance["metrics"]

    assert report_metrics["gate_and_freshness"]["max_seconds"] == measured_metrics[
        "gate_and_freshness"
    ]["max_seconds"]
    assert report_metrics["enumerate_eight_probes_and_eight_ports"]["max_seconds"] == (
        measured_metrics["enumerate_eight_devices"]["max_seconds"]
    )
    assert report_metrics["null_plan_and_handshake"]["max_seconds"] == measured_metrics[
        "null_plan_and_handshake"
    ]["max_seconds"]
