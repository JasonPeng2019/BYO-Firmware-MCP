"""Measure the M10 local performance targets without making them CI gates."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import tempfile
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pyocd_debug_mcp import probe_inventory, serial_resolver
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.guardrails.gate import GateManager
from pyocd_debug_mcp.guardrails.plan_engine import PlanEngine
from pyocd_debug_mcp.kernel.registry import ToolRegistry
from pyocd_debug_mcp.kernel.run_state import create_server_run
from pyocd_debug_mcp.safety.enforce import SafetyPolicy
from pyocd_debug_mcp.safety.fingerprints import FingerprintInputs, FingerprintSource
from pyocd_debug_mcp.safety.map_build import (
    RegionContribution,
    SafetyArtifactRepository,
    SafetyMapBuilder,
    SafetySetupRequest,
)
from pyocd_debug_mcp.safety.regions import (
    AddressRange,
    Provenance,
    RegionKind,
    SafetyRegion,
    SourceAuthority,
)
from pyocd_debug_mcp.tools.handshake import build_initialization_guidance

TARGETS_SECONDS = {
    "gate_and_freshness": 0.250,
    "enumerate_eight_devices": 10.0,
    "null_plan_and_handshake": 2.0,
}


def _distribution(samples: list[float], target: float) -> dict[str, object]:
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "target_seconds": target,
        "sample_count": len(samples),
        "median_seconds": statistics.median(samples),
        "p95_seconds": ordered[p95_index],
        "max_seconds": max(samples),
        "within_target": max(samples) <= target,
    }


def _measure(operation: Callable[[], None], samples: int) -> list[float]:
    values: list[float] = []
    operation()  # Warm imports, filesystem metadata, and caches outside the samples.
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        values.append(time.perf_counter() - started)
    return values


def _fingerprint_inputs() -> FingerprintInputs:
    return FingerprintInputs(
        profile={"board_id": "performance_board"},
        part_target={"mcu_part_number": "PERFORMANCE-PART", "target": "performance"},
        pack={"id": "Performance.Pack", "version": "1"},
        evidence={"source": "M10 deterministic fixture"},
        application_artifacts={"configuration": "none"},
        bootloader_artifacts={"configuration": "none"},
        geometry={"erase_origin": 0x08000000, "erase_size": 0x800},
        schema={"memory_map": 1},
    )


def _gate_operation(project_root: Path) -> Callable[[], None]:
    store = FirmStore(project_root)
    inputs = _fingerprint_inputs()
    region = RegionContribution(
        SafetyRegion(
            "performance RAM",
            RegionKind.RAM,
            AddressRange(0x20000000, 0x20001000),
            (
                Provenance(
                    SourceAuthority.RECONCILED,
                    "m10-performance",
                    "Deterministic local performance fixture",
                ),
            ),
        ),
        (FingerprintSource.EVIDENCE,),
    )
    result = SafetyMapBuilder(store).build(
        SafetySetupRequest(
            "performance_board",
            "m10-performance",
            inputs,
            (region,),
        )
    )
    if result.aggregate_fingerprint is None:
        raise RuntimeError("performance safety fixture did not produce a fingerprint")
    policy = SafetyPolicy(
        SafetyArtifactRepository(store),
        live_inputs=lambda _board_id, _artifacts: inputs,
    )
    gate = GateManager()
    gate.stamp_validation(
        board_id="performance_board",
        connection_id="probe:performance",
        hardware_result="validation_passed_uart_not_configured",
        probe_identity="PERFORMANCE-PROBE",
        aggregate_fingerprint=result.aggregate_fingerprint,
    )

    def operation() -> None:
        current = policy.current_aggregate("performance_board")
        gate.require_write("performance_board", "probe:performance", current)

    return operation


def _enumeration_operation() -> Callable[[], None]:
    probes = [
        probe_inventory.ProbeInfo(
            uid=f"PERF-{index:02d}",
            description=f"Performance probe {index}",
            raw=f"fixture probe {index}",
        )
        for index in range(8)
    ]
    ports = [
        SimpleNamespace(
            device=f"COM{index + 20}",
            description=f"Performance UART {index}",
            manufacturer="Fixture",
            product="UART",
            interface="CDC",
            hwid=f"USB PERF:{index}",
            serial_number=f"PERF-{index:02d}",
            location=f"fixture-{index}",
            vid=0x1234,
            pid=0x5678,
        )
        for index in range(8)
    ]

    def operation() -> None:
        original_probes = probe_inventory._list_connected_probes_via_pyocd_api
        from serial.tools import list_ports  # type: ignore[import-untyped]

        original_ports = list_ports.comports
        try:
            probe_inventory._list_connected_probes_via_pyocd_api = lambda: probes
            list_ports.comports = lambda: ports
            found_probes = probe_inventory.list_connected_probes(
                lambda _argv: (1, "", "disabled"),
                allow_subprocess_fallback=False,
            )
            found_ports = serial_resolver.list_serial_ports()
            if len(found_probes) != 8 or found_ports is None or len(found_ports) != 8:
                raise RuntimeError("eight-device enumeration fixture was not preserved")
        finally:
            probe_inventory._list_connected_probes_via_pyocd_api = original_probes
            list_ports.comports = original_ports

    return operation


def _guidance_operation() -> Callable[[], None]:
    registry = ToolRegistry()
    registry.register("initialization_handshake")
    registry.register("read_serial-plan")
    registry.register("read_serial", hidden=True, locked=True)
    engine = PlanEngine(create_server_run(), registry)

    def operation() -> None:
        result = engine.null_response("read_serial-plan")
        guidance = build_initialization_guidance(registry)
        if result.status != "initialized" or "Currently visible tools" not in guidance:
            raise RuntimeError("NULL-plan or handshake performance fixture failed")

    return operation


def _package_versions() -> dict[str, str]:
    packages = ("mcp", "pyocd", "pyserial", "pyelftools", "pyyaml")
    result: dict[str, str] = {}
    for package in packages:
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "not-installed"
    return result


def host_context() -> dict[str, object]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "processor": platform.processor() or "unreported",
        "logical_cpu_count": os.cpu_count(),
        "ci_environment": bool(os.environ.get("CI")),
        "package_versions": _package_versions(),
    }


def collect_performance(*, samples: int = 7) -> dict[str, Any]:
    """Collect local measurements; callers decide how to report target misses."""

    if samples < 1:
        raise ValueError("samples must be positive")
    with tempfile.TemporaryDirectory(prefix="firm-m10-performance-") as temporary:
        gate = _measure(_gate_operation(Path(temporary)), samples)
    enumeration = _measure(_enumeration_operation(), samples)
    guidance = _measure(_guidance_operation(), samples)
    return {
        "schema_version": 1,
        "non_ci_gating": True,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": host_context(),
        "metrics": {
            "gate_and_freshness": _distribution(
                gate, TARGETS_SECONDS["gate_and_freshness"]
            ),
            "enumerate_eight_devices": _distribution(
                enumeration, TARGETS_SECONDS["enumerate_eight_devices"]
            ),
            "null_plan_and_handshake": _distribution(
                guidance, TARGETS_SECONDS["null_plan_and_handshake"]
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    document = collect_performance(samples=arguments.samples)
    rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
