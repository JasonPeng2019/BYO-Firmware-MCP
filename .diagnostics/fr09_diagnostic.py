"""Actions-only UART diagnostic; independent unittest observation, no physical I/O.

This is a finite ROOT diagnostic artifact, never product/harness implementation.
Bind to an actual completed source tip before publication; no local execution.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def flatten(suite):
    if isinstance(suite, unittest.TestSuite):
        for item in suite:
            yield from flatten(item)
    else:
        yield suite.id()


def child(args):
    source = args.source.resolve(strict=True)
    sys.path[:0] = [str(source / "src"), str(source)]
    events = []
    outcomes = {}
    suite = unittest.defaultTestLoader.loadTestsFromNames(args.names)
    loaded = list(flatten(suite))

    class ObservedResult(unittest.TextTestResult):
        def record(self, kind, test, **facts):
            events.append({"kind": kind, "id": test.id(), "monotonic": time.monotonic(), **facts})

        def startTest(self, test):
            self.record("start", test)
            super().startTest(test)

        def stopTest(self, test):
            self.record("stop", test, outcome=outcomes.get(test.id(), "UNKNOWN"))
            super().stopTest(test)

        def addSuccess(self, test):
            outcomes[test.id()] = "PASS"
            self.record("outcome", test, outcome="PASS")
            super().addSuccess(test)

        def addFailure(self, test, err):
            outcomes[test.id()] = "FAIL"
            self.record("outcome", test, outcome="FAIL", traceback=self._exc_info_to_string(err, test))
            super().addFailure(test, err)

        def addError(self, test, err):
            outcomes[test.id()] = "ERROR"
            self.record("outcome", test, outcome="ERROR", traceback=self._exc_info_to_string(err, test))
            super().addError(test, err)

        def addSkip(self, test, reason):
            parent = getattr(test, "test_case", None)
            if parent is None:
                outcomes[test.id()] = "SKIP"
            self.record("subtest" if parent else "outcome", test, outcome="SKIP", reason=reason,
                        parent=parent.id() if parent else None)
            super().addSkip(test, reason)

        def addSubTest(self, test, subtest, err):
            outcome = "PASS" if err is None else ("FAIL" if issubclass(err[0], test.failureException) else "ERROR")
            if err is not None:
                outcomes[test.id()] = outcome
            self.record("subtest", subtest, parent=test.id(), outcome=outcome,
                        traceback=self._exc_info_to_string(err, test) if err else None)
            super().addSubTest(test, subtest, err)

    result = unittest.TextTestRunner(verbosity=2, resultclass=ObservedResult).run(suite)
    observations = {}
    for name in args.names:
        module = sys.modules.get(name)
        if module is not None:
            observations[name] = getattr(module, "OBSERVATIONS", {})
    missing = [name for name in loaded if name not in outcomes]
    summary = {
        "at": datetime.now(timezone.utc).isoformat(), "source": str(source),
        "pid": os.getpid(), "python": sys.version, "executable": sys.executable,
        "platform": platform.platform(), "machine": platform.machine(),
        "requested_modules": args.names, "loaded": loaded, "testsRun": result.testsRun,
        "outcomes": outcomes, "loaded_without_outcome": missing,
        "failure_ids": [t.id() for t, _ in result.failures],
        "error_ids": [t.id() for t, _ in result.errors],
        "skip_ids": [t.id() for t, _ in result.skipped],
        "unexpected_successes": [t.id() for t in result.unexpectedSuccesses],
        "expected_failure_ids": [t.id() for t, _ in result.expectedFailures],
        "observations": observations,
        "dependencies": {name: importlib.metadata.version(name) for name in ("pyocd", "mcp", "pyserial", "psutil")},
        "scope": "Actual source unit/managed callbacks with simulated discovery/vendor/transport; no physical UART or hardware acceptance",
    }
    write(args.evidence / "result.json", summary)
    args.evidence.joinpath("events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return 0 if result.wasSuccessful() and not missing else 1


def driver(args):
    source = args.source.resolve(strict=True)
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    binding = json.loads((source / ".diagnostics/fr09-binding.json").read_bytes())
    source_checks = []
    original_input_bytes = {}
    for item in binding["firmware_files"]:
        path = source / item["path"]
        if item["path"] == ".github/workflows/code-analysis-portability.yml":
            path = source / ".diagnostics/original-workflow.yml"
        source_checks.append({"path": item["path"], "expected": item["sha256"],
                              "actual": digest(path), "ok": digest(path) == item["sha256"]})
        original_input_bytes[item["path"]] = path.read_bytes()
    write(evidence / "source-binding.json", {"binding": binding, "checks": source_checks})
    if not all(x["ok"] for x in source_checks):
        write(evidence / "summary.json", {"outcome": "ERROR", "reason": "Source binding mismatch; no tests executed"})
        return 1
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    commands = []

    def snapshot(label):
        rows = []
        for item in binding["firmware_files"]:
            actual_path = source / item["path"]
            if item["path"] == ".github/workflows/code-analysis-portability.yml":
                actual_path = source / ".diagnostics/original-workflow.yml"
            raw = actual_path.read_bytes() if actual_path.exists() else None
            actual_hash = hashlib.sha256(raw).hexdigest() if raw is not None else None
            row = {"path": item["path"], "expected_sha256": item["sha256"],
                   "actual_sha256": actual_hash, "bytes": len(raw) if raw is not None else None,
                   "equal_initial": actual_hash == item["sha256"]}
            if not row["equal_initial"] and raw is not None:
                copy = evidence / "changed-bound-files" / label / item["path"]
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(raw)
                row["captured_changed_bytes"] = str(copy.relative_to(evidence))
            rows.append(row)
        output = evidence / "source-snapshots" / (label + ".json")
        write(output, {"label": label, "source": str(source), "checks": rows})
        return {"path": str(output.relative_to(evidence)), "sha256": digest(output),
                "checked": len(rows), "changed": [r for r in rows if not r["equal_initial"]]}

    initial_snapshot = snapshot("initial")

    def run(label, argv, cwd=source):
        target = evidence / label
        target.mkdir(exist_ok=True)
        record = {"label": label, "argv": argv, "cwd": str(cwd), "start": time.monotonic()}
        record["source_before"] = snapshot(label + "-before")
        try:
            completed = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, timeout=240)
            record.update(exit_code=completed.returncode, timeout=False)
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            record.update(exit_code=None, timeout=True)
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
        target.joinpath("stdout.log").write_bytes(stdout)
        target.joinpath("stderr.log").write_bytes(stderr)
        record["end"] = time.monotonic()
        record["source_after"] = snapshot(label + "-after")
        commands.append(record)
        write(evidence / "commands.json", commands)
        print(json.dumps(record), flush=True)
        print(stdout.decode("utf-8", "replace"), flush=True)
        print(stderr.decode("utf-8", "replace"), flush=True)
        return record

    changed = ["src/pyocd_debug_mcp/serial_resolver.py", "tests/test_serial_resolver_identity.py"]
    lint = run("ruff-check", [sys.executable, "-m", "ruff", "check", *changed])
    formatting = run("ruff-format", [sys.executable, "-m", "ruff", "format", "--check", *changed])
    runner = str(Path(__file__).resolve())
    candidate = run("candidate", [sys.executable, "-B", runner, "--child", "--source", str(source),
        "--evidence", str(evidence / "candidate"), "--names", "tests.test_serial_resolver_identity", "tests.test_probe_selection_records"])
    owned_scratch = Path(os.environ["RUNNER_TEMP"]) / "MCP_Server_Dev/development/fr09-diagnostic"
    owned_scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="baseline-", dir=owned_scratch) as temporary:
        baseline = Path(temporary).resolve()
        assert baseline.is_relative_to(owned_scratch.resolve())
        for item in binding["firmware_files"]:
            copied = baseline / item["path"]
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(original_input_bytes[item["path"]])
        resolver = baseline / "src/pyocd_debug_mcp/serial_resolver.py"
        shutil.copyfile(source / ".diagnostics/baseline-serial-resolver.py", resolver)
        assert digest(resolver) == binding["baseline_resolver_sha256"]
        baseline_checks = []
        for item in binding["firmware_files"]:
            expected_hash = binding["baseline_resolver_sha256"] if item["path"] == "src/pyocd_debug_mcp/serial_resolver.py" else item["sha256"]
            actual_hash = digest(baseline / item["path"])
            baseline_checks.append({"path": item["path"], "expected": expected_hash,
                                    "actual": actual_hash, "ok": actual_hash == expected_hash})
        write(evidence / "baseline-input-binding.json", {"checks": baseline_checks,
              "cwd": str(baseline), "source": "Frozen INITIAL candidate bytes, only parent resolver replaced"})
        assert all(row["ok"] for row in baseline_checks)

        before = run("baseline-resolver", [sys.executable, "-B", runner, "--child", "--source", str(baseline),
            "--evidence", str(evidence / "baseline-resolver"), "--names", "tests.test_serial_resolver_identity"], cwd=baseline)
    candidate_result_path = evidence / "candidate/result.json"
    baseline_result_path = evidence / "baseline-resolver/result.json"
    actual = json.loads(candidate_result_path.read_bytes()) if candidate_result_path.exists() else None
    prior = json.loads(baseline_result_path.read_bytes()) if baseline_result_path.exists() else None
    expected = set(binding["baseline_expected_failing_methods"])
    assert expected, "Baseline sensitivity needs concrete independently chosen method IDs"
    failed_parents = {k for k, value in prior["outcomes"].items() if value == "FAIL"} if prior else set()
    candidate_ok = bool(actual and candidate["exit_code"] == 0 and actual["testsRun"] == len(actual["loaded"])
                        and not actual["skip_ids"] and not actual["loaded_without_outcome"]
                        and set(actual["outcomes"].values()) == {"PASS"})
    sensitivity = bool(prior and before["exit_code"] == 1 and failed_parents == expected
                       and not prior["error_ids"] and not prior["skip_ids"] and not prior["loaded_without_outcome"])
    final_source = all(digest(source / item["path"]) == item["sha256"] for item in binding["firmware_files"]
                       if item["path"] != ".github/workflows/code-analysis-portability.yml")
    summary = {"at": datetime.now(timezone.utc).isoformat(), "candidate_pass": candidate_ok,
        "baseline_expected_failure_methods": sorted(expected), "baseline_actual_failure_methods": sorted(failed_parents),
        "baseline_sensitivity_matches": sensitivity, "original_candidate_source_unchanged": final_source,
        "lint_exit": lint["exit_code"], "format_exit": formatting["exit_code"], "commands": commands,
        "scope": "Finite actual source UART diagnostic/quality only; original source/installed/native/model/physical full acceptance remains required"}
    ok = candidate_ok and sensitivity and final_source and lint["exit_code"] == formatting["exit_code"] == 0
    summary["outcome"] = "PASS" if ok else "FAIL"
    summary["initial_source_snapshot"] = initial_snapshot
    summary["final_source_snapshot"] = snapshot("final-before-format-material")
    material = evidence / "format-material" / "test_serial_resolver_identity.py"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_bytes(original_input_bytes["tests/test_serial_resolver_identity.py"])
    formatting_material = run("prepare-format-material", [sys.executable, "-m", "ruff", "format", "--no-cache",
        "--config", str(source / "pyproject.toml"), str(material)])
    same_ast = ast.dump(ast.parse(original_input_bytes["tests/test_serial_resolver_identity.py"]), include_attributes=False) == ast.dump(ast.parse(material.read_bytes()), include_attributes=False)
    material_proof = {"source_ROOT": binding["ROOT_candidate"], "original_sha256": hashlib.sha256(original_input_bytes["tests/test_serial_resolver_identity.py"]).hexdigest(),
        "formatted_sha256": digest(material), "formatted_bytes": material.stat().st_size,
        "whole_AST_equal": same_ast, "ruff_format_exit": formatting_material["exit_code"],
        "ruff_distribution_version": importlib.metadata.version("ruff"),
        "valid_material": same_ast and formatting_material["exit_code"] == 0,
        "scope": "Actual Actions-generated formatter bytes only; product/source unchanged by formatter; not acceptance"}
    write(material.parent / "proof.json", material_proof)
    summary["format_material"] = material_proof
    summary["final_source_snapshot"] = snapshot("final")
    write(evidence / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return 0 if ok else 1


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This diagnostic executes only in GitHub Actions")
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--names", nargs="+")
    args = parser.parse_args()
    return child(args) if args.child else driver(args)


if __name__ == "__main__":
    raise SystemExit(main())
