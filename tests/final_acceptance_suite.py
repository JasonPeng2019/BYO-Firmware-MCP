"""Finite Actions-only complete source pass with per-method/subtest accounting.

No test module is imported by the parent. The real-agent module is enumerated
from AST and blocked before import. Each other module runs once in a fresh
dependency interpreter, using unittest's actual loader/result implementations.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = {
    "test_monitor_codex_e2e": "18-method real-model module requires separately authorized isolated model/client/credential provisioning; no agent execution or credential copying authorized"
}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and "__pycache__" not in p.parts
        and ".git" not in p.parts
        and p.suffix != ".pyc"
    }


def prepare_native_inputs(args, evidence, environment):
    """Bind optional native prerequisites to the actual built bundle/ARM receipt."""
    facts = {"bundle": str(args.native_bundle), "arm_receipt": str(args.arm_receipt)}
    bundle = args.native_bundle.resolve() if args.native_bundle else None
    if bundle is not None and (bundle / "release-manifest.json").is_file():
        manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
        facts["manifest_sha256"] = hashlib.sha256(
            (bundle / "release-manifest.json").read_bytes()
        ).hexdigest()
        inventory = {row["path"]: row for row in manifest["files"]}
        payload = []
        for path in sorted((bundle / "analysis").rglob("*")):
            if not path.is_file() or path.name == "runtime.json":
                continue
            relative = path.relative_to(bundle).as_posix()
            measured = hashlib.sha256(path.read_bytes()).hexdigest()
            if inventory[relative]["sha256"] != measured:
                raise RuntimeError("native fixture does not match built manifest: " + relative)
            payload.append(
                {
                    "source": str(path),
                    "runtime_path": relative,
                    "sha256": measured,
                    "size": path.stat().st_size,
                }
            )
        executable = bundle / (
            "analysis/clangd/bin/clangd.exe" if os.name == "nt" else "analysis/clangd/bin/clangd"
        )
        if executable.is_file():
            environment["BYO_MCP_TEST_CLANGD_EXECUTABLE"] = str(executable)
        protected = {
            str(bundle / name): hashlib.sha256((bundle / name).read_bytes()).hexdigest()
            for name in ("release-manifest.json", "analysis/runtime.json")
        }
        arm_inputs = {
            str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "tests/code_analysis_fixtures/arm_firmware").rglob("*")
            if p.is_file()
        }
        if args.arm_receipt and args.arm_receipt.is_file():
            receipt = json.loads(args.arm_receipt.read_text(encoding="utf-8"))
            compiler = Path(receipt["compiler"])
            if hashlib.sha256(compiler.read_bytes()).hexdigest() != receipt["compiler_sha256"]:
                raise RuntimeError("actual ARM compiler no longer matches acquisition receipt")
            environment["BYO_MCP_TEST_ARM_GNU_ROOT"] = str(Path(receipt["root"]).resolve())
            protected[str(compiler)] = receipt["compiler_sha256"]
            facts["arm_receipt_sha256"] = hashlib.sha256(args.arm_receipt.read_bytes()).hexdigest()
        else:
            facts["arm_prerequisite"] = "UNMET; explicit ARM-dependent tests retain SKIP/BLOCKED"
        if os.name == "nt":
            record = evidence / "native-input-record.json"
            write(
                record,
                {
                    "payload": payload,
                    "protected_inputs": protected,
                    "frozen_arm_inputs": arm_inputs,
                },
            )
            environment["BYO_MANAGED_TEST_INPUT_RECORD"] = str(record)
            environment["BYO_MANAGED_TEST_INPUT_RECORD_SHA256"] = hashlib.sha256(
                record.read_bytes()
            ).hexdigest()
    else:
        facts["bundle_prerequisite"] = (
            "UNMET; source-only independent tests still run; native/installed dependent checks retain missing-prerequisite outcomes"
        )
    write(evidence / "native-prerequisites.json", facts)


def declarations(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return [
        f"tests.{path.stem}.{node.name}.{method.name}"
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for method in node.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        and method.name.startswith("test")
    ]


class LedgerResult(unittest.TextTestResult):
    def __init__(self, *args, ledger, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = ledger
        self.outcomes = {}

    def emit(self, kind, test, outcome=None, **extra):
        record = {
            "kind": kind,
            "id": test.id(),
            "outcome": outcome,
            "at_monotonic": time.monotonic(),
            **extra,
        }
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=str) + "\n")

    def startTest(self, test):
        super().startTest(test)
        self.outcomes[test.id()] = "UNRUN"
        self.emit("start", test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[test.id()] = "PASS"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[test.id()] = "FAIL"
        self.emit("failure", test, "FAIL", traceback="".join(traceback.format_exception(*err)))

    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes[test.id()] = "ERROR"
        self.emit("error", test, "ERROR", traceback="".join(traceback.format_exception(*err)))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = "SKIP"
        self.emit("skip", test, "SKIP", reason=reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        outcome = (
            "PASS"
            if err is None
            else ("FAIL" if issubclass(err[0], test.failureException) else "ERROR")
        )
        if err is not None:
            self.outcomes[test.id()] = outcome
        self.emit(
            "subtest",
            subtest,
            outcome,
            parent=test.id(),
            traceback=None if err is None else "".join(traceback.format_exception(*err)),
        )

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.outcomes[test.id()] = "SKIP"
        self.emit(
            "expectedFailure", test, "SKIP", traceback="".join(traceback.format_exception(*err))
        )

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.outcomes[test.id()] = "FAIL"
        self.emit("unexpectedSuccess", test, "FAIL")

    def stopTest(self, test):
        self.emit("stop", test, self.outcomes.get(test.id(), "UNRUN"))
        super().stopTest(test)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item.id()


def worker(module, evidence):
    sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "src")]
    if module in BLOCKED:
        raise RuntimeError("blocked real-agent module must never be imported")
    suite = unittest.defaultTestLoader.loadTestsFromName("tests." + module)
    loaded_module = sys.modules.get("tests." + module)
    if (
        loaded_module is not None
        and Path(loaded_module.__file__).resolve() != (ROOT / "tests" / (module + ".py")).resolve()
    ):
        raise RuntimeError("test module did not come from the frozen source: " + module)
    loaded = list(flatten(suite))
    write(evidence / "loaded.json", loaded)
    runner = unittest.TextTestRunner(
        verbosity=2,
        resultclass=lambda *a, **kw: LedgerResult(*a, ledger=evidence / "events.jsonl", **kw),
    )
    result = runner.run(suite)
    actual_module = sys.modules.get("tests." + module)
    if actual_module is not None and hasattr(actual_module, "OBSERVATIONS"):
        write(evidence / "observations.json", actual_module.OBSERVATIONS)
    import psutil

    descendants, cleanup_errors = [], []
    try:
        for process in psutil.Process().children(recursive=True):
            try:
                birth = process.create_time()
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    item = {"pid": process.pid, "creation_time": birth, "survived_suite": True}
                    descendants.append(item)
                    if process.is_running() and process.create_time() == birth:
                        process.kill()
                        process.wait(timeout=15)
                    item["cleanup"] = "gone"
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, psutil.TimeoutExpired) as exc:
                cleanup_errors.append(str(exc))
    except psutil.Error as exc:
        cleanup_errors.append(str(exc))
    write(
        evidence / "cleanup.json",
        {
            "surviving_test_descendants": descendants,
            "errors": cleanup_errors,
            "scope": "actual end-of-module descendant snapshot; not exhaustive OS isolation evidence",
        },
    )
    dependency = {}
    for name in ("pyocd", "mcp", "anyio", "pyelftools", "psutil", "pyserial", "PyYAML"):
        dependency[name] = importlib.metadata.version(name)
    imports = []
    for name, module_object in list(sys.modules.items()):
        raw = getattr(module_object, "__file__", None)
        if raw and name.startswith(
            ("pyocd_debug_mcp", "pyocd.", "mcp.", "anyio.", "elftools.", "psutil")
        ):
            path = Path(raw)
            if path.is_file():
                imports.append(
                    {
                        "module": name,
                        "path": str(path.resolve()),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
    write(
        evidence / "result.json",
        {
            "successful": result.wasSuccessful(),
            "testsRun": result.testsRun,
            "loaded": loaded,
            "methods": result.outcomes,
            "counts": dict(Counter(result.outcomes.values())),
            "dependency_versions": dependency,
            "imports": imports,
            "python": sys.executable,
            "note": "Imported module identities are parent/test-process evidence; actual child identities require their own receipts",
        },
    )
    return 0 if result.wasSuccessful() and not descendants and not cleanup_errors else 1


def parent(args):
    evidence = args.evidence.resolve()
    if evidence.exists():
        raise RuntimeError("evidence directory already exists; do not overwrite or retry a pass")
    evidence.mkdir(parents=True)
    source_before = {name: hashes(ROOT / name) for name in ("src", "tests", ".github")}
    # One declared preprepared professional variant; the personal source never
    # changes. Record all variant bytes before any acceptance command begins.
    professional = evidence / "fixtures/professional/src"
    shutil.copytree(ROOT / "src", professional, ignore=shutil.ignore_patterns("__pycache__"))
    profile = professional / "pyocd_debug_mcp/monitor/build_profile.py"
    original = profile.read_bytes()
    if original.count(b"NARRATIVE_LOGGING: bool = True") != 1:
        raise RuntimeError("professional fixture toggle no longer matches source")
    profile.write_bytes(
        original.replace(b"NARRATIVE_LOGGING: bool = True", b"NARRATIVE_LOGGING: bool = False")
    )
    variant_before = hashes(professional)
    write(
        evidence / "freeze-inputs.json",
        {
            "personal": source_before,
            "professional": variant_before,
            "transform": "monitor/build_profile.py one True-to-False literal only",
        },
    )
    environment = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]),
        BYO_TEST_PROFESSIONAL_SRC=str(professional),
        BYO_MCP_TEST_WORKSPACE_ROOT=str(args.workspace_source.resolve()),
    )
    native_input_error = None
    try:
        prepare_native_inputs(args, evidence, environment)
    except Exception:
        native_input_error = traceback.format_exc()
        write(
            evidence / "native-prerequisite-error.json",
            {"outcome": "ERROR", "traceback": native_input_error},
        )
    modules = []
    failed = native_input_error is not None
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        module = path.stem
        directory = evidence / module
        directory.mkdir()
        declared = declarations(path)
        write(directory / "declared.json", declared)
        if module in BLOCKED:
            record = {
                "module": module,
                "outcome": "BLOCKED",
                "reason": BLOCKED[module],
                "methods": {name: "BLOCKED" for name in declared},
                "exit": None,
            }
            failed = True
        else:
            command = [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--worker",
                module,
                "--evidence",
                str(directory),
            ]
            started = time.monotonic()
            with (
                (directory / "stdout.log").open("wb") as stdout,
                (directory / "stderr.log").open("wb") as stderr,
            ):
                child = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env={
                        **environment,
                        "BYO_TEST_STDIO_IDENTITIES": str(directory / "child-imports"),
                        "BYO_MANAGED_TEST_EVIDENCE": str(directory / "managed"),
                        "BYO_WINDOWS_REGRESSION_EVIDENCE": str(directory / "windows-regressions"),
                        "BYO_WINDOWS_ACCEPTANCE_EVIDENCE": str(directory / "windows-acceptance"),
                        "BYO_MCP_MONITOR_ROOT": str(directory / "monitor"),
                        "PYOCD_MCP_RUNS_ROOT": str(directory / "runs"),
                        "BYO_FINAL_CASE_EVIDENCE": str(directory / "case-evidence"),
                        "VI01A_OBSERVATIONS": str(directory / "vi01a-observations.jsonl"),
                    },
                    stdout=stdout,
                    stderr=stderr,
                )
                try:
                    exit_code = child.wait(timeout=args.module_timeout)
                    outcome = "PASS" if exit_code == 0 else "FAIL"
                except subprocess.TimeoutExpired:
                    # psutil is an actual locked dependency. Kill only identities
                    # captured under this still-live Popen anchor.
                    import psutil

                    process = psutil.Process(child.pid)
                    owned = [(p, p.create_time()) for p in process.children(recursive=True)]
                    for item, birth in reversed(owned):
                        try:
                            if item.is_running() and item.create_time() == birth:
                                item.kill()
                        except psutil.NoSuchProcess:
                            pass
                    child.kill()
                    child.wait(timeout=15)
                    exit_code, outcome = child.returncode, "TIMEOUT"
            record = {
                "module": module,
                "command": command,
                "cwd": str(ROOT),
                "exit": exit_code,
                "outcome": outcome,
                "seconds": time.monotonic() - started,
                "declared_methods": declared,
                "result_present": (directory / "result.json").is_file(),
            }
            if not record["result_present"]:
                record["unaccounted"] = (
                    "No final worker summary; retain loaded/events/raw streams and mark all missing method stops UNRUN"
                )
            failed |= exit_code != 0 or not record["result_present"]
        source_current = {name: hashes(ROOT / name) for name in source_before}
        record["source_still_matches_before_pass"] = source_current == source_before
        record["professional_still_matches_before_pass"] = hashes(professional) == variant_before
        failed |= (
            not record["source_still_matches_before_pass"]
            or not record["professional_still_matches_before_pass"]
        )
        write(directory / "command.json", record)
        modules.append(record)
        print(
            json.dumps({"module": module, "outcome": record["outcome"], "exit": record["exit"]}),
            flush=True,
        )
    unchanged = source_before == {name: hashes(ROOT / name) for name in source_before}
    variant_unchanged = variant_before == hashes(professional)
    write(
        evidence / "summary.json",
        {
            "modules": modules,
            "source_unchanged": unchanged,
            "professional_unchanged": variant_unchanged,
            "outcome": "FAIL" if failed or not unchanged or not variant_unchanged else "PASS",
            "blocked_agent_policy": BLOCKED,
        },
    )
    return int(failed or not unchanged or not variant_unchanged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--workspace-source", type=Path)
    parser.add_argument("--worker")
    parser.add_argument("--native-bundle", type=Path)
    parser.add_argument("--arm-receipt", type=Path)
    parser.add_argument("--module-timeout", type=int, default=900)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Final source execution is Actions-only after ROOT freeze")
    if args.worker:
        return worker(args.worker, args.evidence)
    if args.workspace_source is None or not args.workspace_source.is_dir():
        raise SystemExit("Actual frozen AgentWorkspace checkout is required")
    return parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
