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
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import traceback
import unittest
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

if __package__:
    from .process_cleanup_observations import (
        CleanupTrace,
        exception_facts,
        popen_facts,
        process_facts,
        read_receipt,
        role_evidence,
    )
else:
    from process_cleanup_observations import (
        CleanupTrace,
        exception_facts,
        popen_facts,
        process_facts,
        read_receipt,
        role_evidence,
    )

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = {
    "test_monitor_codex_e2e": "Campaign authorization is established; actual isolated native-client/model/auth provisioning remains unresolved. Retain every declared method BLOCKED for SAME V; this source job must not launch agents or copy credentials."
}
U5_FILENAME = "Keil.STM32U5xx_DFP.3.2.0.pack"
U5_SHA256 = "e320687fe534f2fe6902e9bcdee981315abea26c4ca547142af9b9439e958be6"
U5_URL = "https://keilpack.azureedge.net/pack/" + U5_FILENAME


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


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def acquire_official_pack(path, evidence):
    """One Actions acquisition; retain the real failed receipt as well as success."""
    facts = {
        "source": U5_URL,
        "expected_sha256": U5_SHA256,
        "path": str(path.resolve()),
        "device": "STM32U575ZIT6Q",
        "pdsc_device": "STM32U575ZITxQ",
        "sha256": None,
        "size": None,
        "error": None,
        "started_unix": time.time(),
    }
    started = time.monotonic()
    try:
        if path.name != U5_FILENAME or path.exists():
            raise RuntimeError("Official pack requires the exact filename and a fresh owned path")
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(U5_URL, timeout=120) as response, path.open("xb") as out:
            facts["response_url"] = response.geturl()
            facts["http_status"] = response.status
            shutil.copyfileobj(response, out)
        facts.update(size=path.stat().st_size, sha256=digest(path))
        if facts["sha256"] != U5_SHA256 or facts["size"] != 8_821_594:
            raise RuntimeError("Official U5 pack differs from its exact pinned bytes")
        with zipfile.ZipFile(path) as archive:
            pdsc = [n for n in archive.namelist() if n.endswith(".pdsc")]
            if len(pdsc) != 1:
                raise RuntimeError("Official U5 pack must contain one PDSC")
            devices = {
                node.attrib[key]
                for node in ET.fromstring(archive.read(pdsc[0])).iter()
                for key in ("Dname", "Dvariant")
                if key in node.attrib
            }
            if "STM32U575ZITxQ" not in devices:
                raise RuntimeError("Pinned U5 PDSC omits the actual test device")
            facts["pdsc"] = pdsc[0]
        facts["outcome"] = "PASS"
    except Exception:
        facts.update(outcome="ERROR", error=traceback.format_exc())
        if path.is_file():
            facts.update(size=path.stat().st_size, sha256=digest(path))
    facts["seconds"] = time.monotonic() - started
    facts["completed_unix"] = time.time()
    write(evidence / "official-pack-acquisition.json", facts)
    return int(facts["outcome"] != "PASS")


def built_native_inputs(bundle, installer):
    """Use the existing builder's validator and exact lock, never relax its consumer."""
    scripts = installer / "release/scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "fr05_analysis_tools", scripts / "analysis_tools.py"
    )
    tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tools)
    lock = tools.load_lock()
    manifest = tools.strict_json((bundle / "release-manifest.json").read_bytes())
    sbom = tools.strict_json((bundle / "sbom.cdx.json").read_bytes())
    if (manifest["platform"], manifest["architecture"]) != (lock["platform"], lock["architecture"]):
        raise RuntimeError("Native bundle does not match the actual host/lock")
    inventory = {r["path"]: r for r in manifest["files"]}
    if len(inventory) != len(manifest["files"]):
        raise RuntimeError("Duplicate built manifest paths")
    if not {"sbom.cdx.json", "analysis/runtime.json"} <= inventory.keys():
        raise RuntimeError("Built manifest omits immutable analysis metadata")
    measured = {}
    for relative, row in inventory.items():
        if relative.startswith("analysis/") or relative == "sbom.cdx.json":
            path = bundle / tools.safe_relative(relative)
            if path.stat().st_size != row["size"] or digest(path) != row["sha256"]:
                raise RuntimeError("Native fixture differs from built manifest: " + relative)
            measured[str(path)] = row["sha256"]
    actual = {
        p.relative_to(bundle).as_posix() for p in (bundle / "analysis").rglob("*") if p.is_file()
    }
    if actual != {p for p in inventory if p.startswith("analysis/")}:
        raise RuntimeError("Actual analysis files differ from built inventory")
    tools.validate_payload(manifest["files"], lambda p: (bundle / p).read_bytes(), sbom, lock)
    components = {c["bom-ref"]: c for c in sbom["components"]}
    provenance, payload = {}, []
    for name, program in lock["programs"].items():
        owner = "byo-analysis:" + name
        properties = {p["name"]: p["value"] for p in components[owner]["properties"]}
        provenance[name] = {
            key: properties["byo:analysis:" + key] for key in ("upstream", "recipe")
        }
        executable = bundle / program["executable"]
        if os.name == "nt":
            tools.pe_imports(executable.read_bytes())
        # RuntimeFixture consumes native payload records only on Windows. A built
        # Cppcheck binary has no upstream archive leaf: bind its real recipe output
        # explicitly, rather than pretending that it was copied from upstream.
        if os.name == "nt":
            for relative, row in sorted(inventory.items()):
                if not relative.startswith("analysis/" + name + "/"):
                    continue
                file_properties = {
                    p["name"]: p["value"]
                    for p in components["byo-analysis-file:" + relative]["properties"]
                }
                if "byo:analysis:upstream_path" in file_properties:
                    upstream = file_properties["byo:analysis:upstream_path"]
                    origin = "built SBOM byo:analysis:upstream_path"
                elif name == "cppcheck" and relative == program["executable"]:
                    upstream = json.loads(provenance[name]["recipe"])["output"]
                    origin = (
                        "built SBOM recipe.output; compiled output, not an upstream archive leaf"
                    )
                else:
                    raise RuntimeError(
                        "Native file has no actual upstream/recipe path: " + relative
                    )
                payload.append(
                    {
                        "source": str(bundle / relative),
                        "runtime_path": relative,
                        "sha256": row["sha256"],
                        "size": row["size"],
                        "owner": owner,
                        "upstream_path": upstream,
                        "upstream_path_origin": origin,
                    }
                )
    for path in [
        bundle / "release-manifest.json",
        installer / "release/analysis-tools.lock.json",
        scripts / "analysis_tools.py",
        scripts / "native_formats.py",
    ]:
        measured[str(path)] = digest(path)
    return lock, payload, provenance, measured


def prepare_native_inputs(args, evidence, environment):
    """Collect independent prerequisites, keeping every error and usable real input."""
    facts, protected, errors = {}, {}, []
    facts["requested_inputs"] = {
        name: str(getattr(args, name))
        for name in ("native_bundle", "installer_source", "arm_receipt", "official_pack_receipt")
    }
    facts["metadata_before_validation"] = {}
    candidates = []
    if args.native_bundle:
        candidates += [
            args.native_bundle / name
            for name in ("release-manifest.json", "sbom.cdx.json", "analysis/runtime.json")
        ]
    if args.installer_source:
        candidates += [
            args.installer_source / "release" / name
            for name in ("analysis-tools.lock.json", "arm-test-toolchains.json")
        ]
    candidates += [p for p in (args.arm_receipt, args.official_pack_receipt) if p]
    candidates += [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for path in candidates:
        try:
            protected[str(path.resolve())] = digest(path)
            facts["metadata_before_validation"][str(path.resolve())] = {
                "sha256": digest(path),
                "size": path.stat().st_size,
            }
        except OSError as exc:
            facts["metadata_before_validation"][str(path)] = {
                "outcome": "UNAVAILABLE",
                "error": str(exc),
            }
    payload, provenance = None, None
    try:
        bundle = args.native_bundle.resolve(strict=True)
        installer = args.installer_source.resolve(strict=True)
        lock, payload, provenance, native_hashes = built_native_inputs(bundle, installer)
        protected.update(native_hashes)
        executable = str(bundle / lock["programs"]["clangd"]["executable"])
        environment.update(BYO_MCP_TEST_CLANGD_EXECUTABLE=executable, BYO_TEST_CLANGD=executable)
        facts["bundle"] = {
            "path": str(bundle),
            "provenance": provenance,
            "outcome": "PASS",
            "clangd": executable,
            "clangd_sha256": digest(executable),
        }
    except Exception:
        facts["bundle"] = {"outcome": "ERROR", "error": traceback.format_exc()}
        errors.append("bundle")
    try:
        receipt_path = args.arm_receipt.resolve(strict=True)
        receipt = json.loads(receipt_path.read_bytes())
        root = Path(receipt["root"]).resolve(strict=True)
        compiler = Path(receipt["compiler"]).resolve(strict=True)
        if not compiler.is_relative_to(root) or digest(compiler) != receipt["compiler_sha256"]:
            raise RuntimeError("Actual ARM compiler no longer matches its acquisition receipt")
        arm_lock = args.installer_source.resolve() / "release/arm-test-toolchains.json"
        source = json.loads(arm_lock.read_bytes())["targets"][receipt["target"]]
        system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]
        arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}[
            platform.machine().casefold()
        ]
        if (
            receipt["schema"] != "arm-test-toolchain-input/v1"
            or receipt["version"] != source["version"]
            or compiler != (root / source["compiler"]).resolve(strict=True)
            or receipt["target"] != system + "-" + arch
            or receipt["archive_layout"] != source["archive_layout"]
            or any(
                receipt["archive"][name] != source[name] for name in ("archive", "url", "sha256")
            )
        ):
            raise RuntimeError("ARM compiler receipt differs from its pinned toolchain contract")
        archive = root.parent.parent / source["archive"]
        if (
            digest(archive) != source["sha256"]
            or archive.stat().st_size != receipt["archive"]["size"]
        ):
            raise RuntimeError("Actual ARM archive differs from its pinned acquisition receipt")
        protected.update({str(p): digest(p) for p in (receipt_path, compiler, arm_lock)})
        environment["BYO_MCP_TEST_ARM_GNU_ROOT"] = str(root)
        facts["arm"] = {
            "receipt": receipt,
            "outcome": "PASS",
            "archive": str(archive),
            "archive_sha256": source["sha256"],
            "archive_size": archive.stat().st_size,
        }
    except Exception:
        facts["arm"] = {"outcome": "ERROR", "error": traceback.format_exc()}
        errors.append("arm")
    pack_acquisition = None
    try:
        receipt_path = args.official_pack_receipt.resolve(strict=True)
        receipt = json.loads(receipt_path.read_bytes())
        pack_acquisition = receipt
        path = Path(receipt["path"]).resolve(strict=True)
        if (
            receipt["outcome"] != "PASS"
            or receipt["source"] != U5_URL
            or path.name != U5_FILENAME
            or digest(path) != U5_SHA256
            or receipt["sha256"] != U5_SHA256
            or receipt["size"] != path.stat().st_size
        ):
            raise RuntimeError("Official U5 pack acquisition is absent, failed or inconsistent")
        protected.update({str(p): digest(p) for p in (receipt_path, path)})
        environment["BYO_MCP_OFFICIAL_U5_PACK"] = str(path)
        facts["official_pack"] = receipt
    except Exception:
        facts["official_pack"] = {
            "outcome": "ERROR",
            "error": traceback.format_exc(),
            "acquisition_receipt": pack_acquisition,
        }
        errors.append("official_pack")
    arm_inputs = {
        str(p.resolve()): digest(p)
        for p in (ROOT / "tests/code_analysis_fixtures/arm_firmware").rglob("*")
        if p.is_file()
    }
    protected.update(arm_inputs)
    if os.name == "nt" and payload is not None:
        record = evidence / "native-input-record.json"
        write(
            record,
            {
                "payload": payload,
                "provenance": provenance,
                "protected_inputs": protected,
                "frozen_arm_inputs": arm_inputs,
            },
        )
        environment["BYO_MANAGED_TEST_INPUT_RECORD"] = str(record)
        environment["BYO_MANAGED_TEST_INPUT_RECORD_SHA256"] = digest(record)
        protected[str(record)] = digest(record)
    facts.update(errors=errors, protected_inputs=protected, outcome="ERROR" if errors else "PASS")
    write(evidence / "native-prerequisites.json", facts)
    return facts


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
            "pid": os.getpid(),
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
        if isinstance(test, unittest.case._SubTest):
            self.emit(
                "subtest",
                test,
                "SKIP",
                parent=test.test_case.id(),
                reason=reason,
                observed_via="addSkip",
            )
            return
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
        self.outcomes[test.id()] = "XFAIL"
        self.emit(
            "expectedFailure", test, "XFAIL", traceback="".join(traceback.format_exception(*err))
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


def execution_identities():
    dependency, imports = {}, []
    for name in ("pyocd", "mcp", "anyio", "pyelftools", "psutil", "pyserial", "PyYAML"):
        try:
            dist = importlib.metadata.distribution(name)
            dependency[name] = {"version": dist.version, "origin": str(dist.locate_file(""))}
        except importlib.metadata.PackageNotFoundError as exc:
            dependency[name] = {"outcome": "UNAVAILABLE", "error": str(exc)}
    for name, module_object in list(sys.modules.items()):
        raw = getattr(module_object, "__file__", None)
        if raw and name.startswith(
            (
                "tests.",
                "test_",
                "pyocd_debug_mcp",
                "pyocd.",
                "mcp.",
                "anyio.",
                "elftools.",
                "psutil",
            )
        ):
            path = Path(raw)
            try:
                imports.append(
                    {"module": name, "path": str(path.resolve()), "sha256": digest(path)}
                )
            except OSError as exc:
                imports.append({"module": name, "path": str(path), "error": str(exc)})
    return {
        "dependency_versions": dependency,
        "imports": imports,
        "python": sys.executable,
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
    }


def worker(module, evidence, select=None):
    sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "src")]
    if module in BLOCKED:
        raise RuntimeError("blocked real-agent module must never be imported")
    evidence.mkdir(parents=True, exist_ok=True)
    # Selected nested children get their own directory before any test import.
    os.environ["BYO_FINAL_CLEANUP_EVIDENCE"] = str(evidence / "cleanup-observations")
    cleanup_trace = CleanupTrace("suite-worker-" + module)
    cleanup_trace.record("module-worker-start", identity=process_facts(os.getpid()))
    (evidence / "events.jsonl").touch(exist_ok=False)
    write(
        evidence / "worker-start.json",
        {
            "module": module,
            "select": select,
            "at_monotonic": time.monotonic(),
            **execution_identities(),
        },
    )
    write(evidence / "loaded.json", [])
    result, loaded, error = None, [], None
    try:
        name = select or "tests." + module
        if select and not select.startswith("tests." + module + "."):
            raise RuntimeError("Selected child must belong to the actual test module")
        suite = unittest.defaultTestLoader.loadTestsFromName(name)
        loaded_module = sys.modules.get("tests." + module)
        if (
            loaded_module is not None
            and Path(loaded_module.__file__).resolve()
            != (ROOT / "tests" / (module + ".py")).resolve()
        ):
            raise RuntimeError("Test module did not come from the frozen source: " + module)
        loaded = list(flatten(suite))
        write(evidence / "loaded.json", loaded)
        runner = unittest.TextTestRunner(
            verbosity=2,
            resultclass=lambda *a, **kw: LedgerResult(*a, ledger=evidence / "events.jsonl", **kw),
        )
        result = runner.run(suite)
    except BaseException:
        error = traceback.format_exc()
        write(evidence / "worker-error.json", {"outcome": "ERROR", "traceback": error})
    finally:
        try:
            actual_module = sys.modules.get("tests." + module)
            if actual_module is not None and hasattr(actual_module, "OBSERVATIONS"):
                write(evidence / "observations.json", actual_module.OBSERVATIONS)
        except BaseException:
            observation_error = traceback.format_exc()
            cleanup_trace.record("observation-export-error", traceback=observation_error)
            error = error or observation_error
    descendants, cleanup_errors = [], []
    try:
        import psutil

    except ImportError:
        cleanup_errors.append("Actual cleanup dependency unavailable: " + traceback.format_exc())
        psutil = None
    try:
        if psutil is None:
            raise RuntimeError("Actual cleanup dependency is unavailable")
        for process in psutil.Process().children(recursive=True):
            item = None
            try:
                birth = process.create_time()
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    item = {"pid": process.pid, "creation_time": birth, "survived_suite": True}
                    descendants.append(item)
                    item["before_emergency_cleanup"] = process_facts(process.pid, birth)
                    item["role_evidence"] = role_evidence(
                        process.pid, birth, cleanup_trace.directory
                    )
                    cleanup_trace.record(
                        "survived-suite-before-emergency-cleanup", observation=item.copy()
                    )
                    if process.is_running() and process.create_time() == birth:
                        cleanup_trace.invoke("suite-emergency.kill", process.kill)
                        cleanup_trace.invoke("suite-emergency.wait", process.wait, timeout=15)
                    item["cleanup"] = "gone"
                    item["after_emergency_cleanup"] = process_facts(process.pid, birth)
                    item["interpretation"] = (
                        "Raw survived_suite remains a module failure; emergency cleanup is not original cleanup PASS"
                    )
                    cleanup_trace.record("after-suite-emergency-cleanup", observation=item.copy())
            except psutil.NoSuchProcess as exc:
                cleanup_trace.record(
                    "snapshot-process-disappeared",
                    pid_observed=process.pid,
                    error=exception_facts(exc),
                )
                pass
            except (psutil.AccessDenied, psutil.TimeoutExpired) as exc:
                cleanup_trace.record(
                    "snapshot-or-cleanup-error",
                    pid_observed=process.pid,
                    error=exception_facts(exc),
                )
                cleanup_errors.append(str(exc))
            finally:
                if item is not None:
                    item["after_cleanup_attempt_even_on_error"] = process_facts(
                        process.pid, item["creation_time"]
                    )
                    cleanup_trace.record("snapshot-finally", observation=item.copy())
    except Exception as exc:
        cleanup_trace.record("snapshot-error", error=exception_facts(exc))
        cleanup_errors.append(str(exc))
    write(
        evidence / "cleanup.json",
        {
            "surviving_test_descendants": descendants,
            "errors": cleanup_errors,
            "observations": cleanup_trace.summary(),
            "scope": "actual end-of-module descendant snapshot; not exhaustive OS isolation evidence",
        },
    )
    raw_outcomes = {} if result is None else result.outcomes
    methods = {name: outcome for name, outcome in raw_outcomes.items() if name in loaded}
    successful = result is not None and result.wasSuccessful() and error is None
    write(
        evidence / "result.json",
        {
            "successful": successful,
            "testsRun": None if result is None else result.testsRun,
            "loaded": loaded,
            "methods": methods,
            "fixture_outcomes": {
                name: outcome for name, outcome in raw_outcomes.items() if name not in loaded
            },
            "counts": dict(Counter(methods.values())),
            "loaded_without_method_outcome": {
                name: "UNRUN; inspect actual fixture events"
                for name in loaded
                if name not in methods
            },
            "error": error,
            **execution_identities(),
            "note": "Imported module identities are parent/test-process evidence; actual child identities require their own receipts",
        },
    )
    return 0 if successful and not descendants and not cleanup_errors else 1


def module_exit_observations(directory):
    """Reobserve only identities actually recorded, after the real module wait.

    The worker retains its existing kill policy. Absence after that intervention
    cannot prove natural interpreter cleanup or erase the survived-suite failure.
    """
    seen, gaps = {}, []
    path = directory / "cleanup.json"
    try:
        cleanup = json.loads(path.read_text(encoding="utf-8"))
        for row in cleanup["surviving_test_descendants"]:
            seen[(row["pid"], row["creation_time"])] = {"source": str(path), "prior": row}
    except (OSError, ValueError, KeyError) as exc:
        gaps.append({"path": str(path), "error": exception_facts(exc)})
    roles = read_receipt(directory / "cleanup-observations/roles.jsonl")
    if "gap" in roles:
        gaps.append(roles)
    for row in roles.get("records", []):
        key = (row["pid"], row.get("creation_time"))
        if key not in seen:
            seen[key] = {"source": roles["path"], "prior": row}
    observations = []
    for (pid, birth), prior in list(seen.items())[:512]:
        if birth is None:
            observations.append(
                {
                    "pid": pid,
                    **prior,
                    "gap": "No birth identity was recorded; no PID-only attribution",
                }
            )
        else:
            observations.append(
                {
                    "pid": pid,
                    "creation_time": birth,
                    **prior,
                    "after_module_exit": process_facts(pid, birth),
                }
            )
    if len(seen) > 512:
        gaps.append("More than 512 seen identities; post-exit observation cap reached")
    return {
        "seen_identities": observations,
        "evidence_gaps": gaps,
        "rule": "Post-exit state is separate from natural cleanup and earlier emergency intervention",
    }


def run_module(command, environment, directory, timeout):
    """Retain dispatch/streams even when no worker can produce a final ledger."""
    write(
        directory / "command-start.json",
        {"argv": command, "cwd": str(ROOT), "at_monotonic": time.monotonic()},
    )
    with (
        (directory / "stdout.log").open("wb") as stdout,
        (directory / "stderr.log").open("wb") as stderr,
    ):
        child = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr)
        trace = CleanupTrace("module-parent", directory / "cleanup-observations")
        leader = process_facts(child.pid)
        trace.record("module-start", popen=popen_facts(child), identity=leader)
        started = time.monotonic()
        outcome = "ERROR"
        try:
            code = trace.invoke("module.wait", child.wait, timeout=timeout)
            outcome = "PASS" if code == 0 else "FAIL"
            return code, "PASS" if code == 0 else "FAIL"
        except subprocess.TimeoutExpired:
            outcome = "TIMEOUT"
            # Preserve the existing identity-bound timeout cleanup and limits.
            import psutil

            process = psutil.Process(child.pid)
            owned = [(p, p.create_time()) for p in process.children(recursive=True)]
            for item, birth in reversed(owned):
                try:
                    trace.record(
                        "timeout-before-emergency-cleanup", identity=process_facts(item.pid, birth)
                    )
                    if item.is_running() and item.create_time() == birth:
                        trace.invoke("timeout-descendant.kill", item.kill)
                except psutil.NoSuchProcess:
                    pass
            trace.invoke("timeout-module.kill", child.kill)
            trace.invoke("timeout-module.wait", child.wait, timeout=15)
            write(
                directory / "timeout-owned.json",
                [{"pid": p.pid, "creation_time": birth} for p, birth in owned],
            )
            return child.returncode, "TIMEOUT"
        finally:
            write(
                directory / "module-exit.json",
                {
                    "outcome": outcome,
                    "elapsed": time.monotonic() - started,
                    "module_identity_at_start": leader,
                    "popen": popen_facts(child),
                    "wait_completed": child.returncode is not None,
                    "boundary": "parent after actual wait/timeout cleanup; cached returncode only",
                    "module_identity_after": (
                        process_facts(child.pid, leader["creation_time"])
                        if leader.get("creation_time") is not None
                        else {
                            "gap": "Original module birth unavailable; no PID-only post-exit attribution"
                        }
                    ),
                    "descendants": module_exit_observations(directory),
                    "operations": trace.summary(),
                },
            )


def python_child_evidence(directory):
    records, gaps = [], []
    for path in sorted((directory / "children").glob("*/command.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            gaps.append({"path": str(path), "error": str(exc)})
            continue
        record["receipt"] = str(path)
        missing = [
            name
            for name in (
                "worker-start.json",
                "loaded.json",
                "events.jsonl",
                "cleanup.json",
                "result.json",
                "stdout.log",
                "stderr.log",
            )
            if not (path.parent / name).is_file()
        ]
        record["required_evidence_gaps"] = missing
        if missing or record.get("phase") != "completed":
            gaps.append({"path": str(path), "missing": missing, "phase": record.get("phase")})
        records.append(record)
    return {
        "records": records,
        "evidence_gaps": gaps,
        "rule": "Outer forwarding and actual child bodies are distinct observations of the same coverage",
    }


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
    if args.installer_source:
        environment["BYO_FINAL_INSTALLER_SOURCE"] = str(args.installer_source.resolve())
    for name in (
        "BYO_MCP_TEST_CLANGD_EXECUTABLE",
        "BYO_TEST_CLANGD",
        "BYO_MCP_TEST_ARM_GNU_ROOT",
        "BYO_MCP_OFFICIAL_U5_PACK",
        "BYO_MANAGED_TEST_INPUT_RECORD",
        "BYO_MANAGED_TEST_INPUT_RECORD_SHA256",
        "VI01A_FULL_PROJECT",
    ):
        environment.pop(name, None)
    native_input_error = None
    native_inputs = {"protected_inputs": {}}
    try:
        native_inputs = prepare_native_inputs(args, evidence, environment)
        native_input_error = native_inputs["errors"] or None
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
            reason = None
            try:
                exit_code, outcome = run_module(
                    command,
                    {
                        **environment,
                        "BYO_TEST_STDIO_IDENTITIES": str(directory / "child-imports"),
                        "BYO_MANAGED_TEST_EVIDENCE": str(directory / "managed"),
                        "BYO_WINDOWS_REGRESSION_EVIDENCE": str(directory / "windows-regressions"),
                        "BYO_WINDOWS_ACCEPTANCE_EVIDENCE": str(directory / "windows-acceptance"),
                        "BYO_MCP_MONITOR_ROOT": str(directory / "monitor"),
                        "PYOCD_MCP_RUNS_ROOT": str(directory / "runs"),
                        "BYO_FINAL_CASE_EVIDENCE": str(directory / "case-evidence"),
                        "VI01A_OBSERVATIONS": str(directory / "vi01a-observations.jsonl"),
                        "BYO_FINAL_CHILD_EVIDENCE": str(directory / "children"),
                    },
                    directory,
                    args.module_timeout,
                )
            except Exception:
                exit_code, outcome, reason = None, "ERROR", traceback.format_exc()
            record = {
                "module": module,
                "command": command,
                "cwd": str(ROOT),
                "exit": exit_code,
                "outcome": outcome,
                "seconds": time.monotonic() - started,
                "declared_methods": declared,
                "result_present": (directory / "result.json").is_file(),
                "reason": reason,
                "children": python_child_evidence(directory),
                "completed_monotonic": time.monotonic(),
                "stdout_sha256": digest(directory / "stdout.log"),
                "stderr_sha256": digest(directory / "stderr.log"),
            }
            record["required_evidence_gaps"] = [
                name
                for name in (
                    "worker-start.json",
                    "loaded.json",
                    "events.jsonl",
                    "cleanup.json",
                    "result.json",
                )
                if not (directory / name).is_file()
            ]
            record["cleanup_diagnostic_gaps"] = [
                name for name in ("module-exit.json",) if not (directory / name).is_file()
            ]
            if not record["result_present"]:
                record["unaccounted"] = (
                    "No final worker summary; retain loaded/events/raw streams and mark all missing method stops UNRUN"
                )
            failed |= (
                exit_code != 0
                or bool(record["required_evidence_gaps"])
                or bool(record["children"]["evidence_gaps"])
            )
        source_current = {name: hashes(ROOT / name) for name in source_before}
        write(directory / "source-after.json", source_current)
        variant_current = hashes(professional)
        write(directory / "professional-after.json", variant_current)
        record["source_still_matches_before_pass"] = source_current == source_before
        record["professional_still_matches_before_pass"] = variant_current == variant_before
        current_inputs = {}
        for name in native_inputs["protected_inputs"]:
            try:
                current_inputs[name] = digest(name)
            except OSError as exc:
                current_inputs[name] = "UNAVAILABLE: " + str(exc)
        write(directory / "protected-inputs-after.json", current_inputs)
        record["protected_inputs_unchanged"] = current_inputs == native_inputs["protected_inputs"]
        failed |= (
            not record["source_still_matches_before_pass"]
            or not record["professional_still_matches_before_pass"]
            or not record["protected_inputs_unchanged"]
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
            "remaining_acceptance": "SAME V must provision safe real native-client/model/auth isolation for every real-model declaration; U1 trust/version, physical/clone/replug/active-UART remain separate requirements.",
        },
    )
    return int(failed or not unchanged or not variant_unchanged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--workspace-source", type=Path)
    parser.add_argument("--worker")
    parser.add_argument("--select", help="Actual unittest ID for an isolated fresh-project child")
    parser.add_argument("--native-bundle", type=Path)
    parser.add_argument("--arm-receipt", type=Path)
    parser.add_argument("--installer-source", type=Path)
    parser.add_argument("--official-pack-receipt", type=Path)
    parser.add_argument("--acquire-official-pack", type=Path)
    parser.add_argument("--module-timeout", type=int, default=900)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Final source execution is Actions-only after ROOT freeze")
    if args.acquire_official_pack:
        return acquire_official_pack(args.acquire_official_pack, args.evidence)
    if args.worker:
        return worker(args.worker, args.evidence, args.select)
    if args.workspace_source is None or not args.workspace_source.is_dir():
        raise SystemExit("Actual frozen AgentWorkspace checkout is required")
    return parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
