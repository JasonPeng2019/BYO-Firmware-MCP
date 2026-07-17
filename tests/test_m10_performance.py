from __future__ import annotations

import math
import runpy
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


MEASUREMENT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/measure_m10_performance.py"
collect_performance = cast(
    Callable[..., dict[str, Any]],
    runpy.run_path(str(MEASUREMENT_SCRIPT))["collect_performance"],
)


def test_m10_performance_targets_are_measured_with_host_context(record_property) -> None:
    """Measure CC-10/11/12 without turning host speed into a CI pass condition."""

    result = collect_performance(samples=3)
    record_property("m10_host_context", result["host"])
    record_property("m10_performance_metrics", result["metrics"])

    assert result["non_ci_gating"] is True
    assert result["host"]["python"]
    assert result["host"]["platform"]
    assert set(result["metrics"]) == {
        "gate_and_freshness",
        "enumerate_eight_devices",
        "null_plan_and_handshake",
    }
    for name, metric in result["metrics"].items():
        assert metric["sample_count"] == 3
        assert 0 <= metric["median_seconds"] < math.inf
        assert 0 <= metric["max_seconds"] < math.inf
        if not metric["within_target"]:
            warnings.warn(
                f"M10 non-gating performance target missed for {name}: "
                f"{metric['max_seconds']:.6f}s > {metric['target_seconds']:.6f}s",
                stacklevel=1,
            )
