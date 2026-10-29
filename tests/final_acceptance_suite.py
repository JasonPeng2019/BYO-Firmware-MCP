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
        descendant_facts,
        exception_facts,
        popen_facts,
        process_facts,
        read_receipt,
        terminal_identity,
    )
else:
    from process_cleanup_observations import (
        CleanupTrace,
        descendant_facts,
        exception_facts,
        popen_facts,
        process_facts,
        read_receipt,
        terminal_identity,
    )

ROOT = Path(__file__).resolve().parents[1]
BLOCKED = {
    "test_monitor_codex_e2e": "Actual isolated native-client/model/auth provisioning remains unresolved for the 17 real-request methods. The separate original deterministic pin body runs without auth. This source job must not launch agents or copy credentials."
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
    tools.validate_analysis(manifest["files"], lambda p: (bundle / p).read_bytes(), sbom, lock)
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
    # CPython still owns finalizers (including multiprocessing helpers) here.
    # Preserve the live pre-exit facts; only the external owner can judge leaks.
    identity = process_facts(os.getpid())
    snapshot = descendant_facts(
        os.getpid(),
        identity.get("creation_time"),
        "before ordinary interpreter exit",
        cleanup_trace.directory,
    )
    cleanup_trace.record("pre-interpreter-exit-snapshot", observation=snapshot)
    write(
        evidence / "cleanup.json",
        {
            "pre_exit_descendants": snapshot["descendants"],
            "errors": snapshot["evidence_gaps"],
            "observations": cleanup_trace.summary(),
            "boundary": "before ordinary interpreter exit; no survivor judgment or intervention",
            "scope": "actual pre-exit snapshot; authoritative survivor gate belongs to observing parent",
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
    return 0 if successful and not snapshot["evidence_gaps"] else 1


def module_exit_observations(directory, captured=(), capture_gaps=()):
    """Reobserve recorded identities without any wait, reap or signal authority.

    Missing suite receipts remain diagnostic gaps (the caller enforces required
    ledgers); unknown ownership/state can never pass the owned-survivor gate.
    """
    seen = dict(captured)
    gaps, identity_gaps = [], list(capture_gaps)
    paths = [directory / "cleanup.json", *sorted((directory / "children").glob("*/cleanup.json"))]
    for path in paths:
        try:
            cleanup = json.loads(path.read_text(encoding="utf-8"))
            identity_gaps.extend(cleanup["errors"])
            for row in cleanup["pre_exit_descendants"]:
                key = (row["pid"], row.get("creation_time"))
                entry = seen.setdefault(key, {"source": str(path), "prior": row})
                tree_owned = row.get(
                    "ownership"
                ) == "captured recursive descendant of exact observed root" and terminal_identity(
                    row.get("identity", {})
                ) in ("live", "terminal")
                entry["owned_by_command"] = entry.get("owned_by_command", False) or tree_owned
                entry["worker_pre_exit"] = row
        except (OSError, ValueError, KeyError) as exc:
            gaps.append({"path": str(path), "error": exception_facts(exc)})
        role_path = path.parent / "cleanup-observations/roles.jsonl"
        if role_path.is_file():
            roles = read_receipt(role_path)
            if "gap" in roles:
                identity_gaps.append(roles)
            for row in roles.get("records", []):
                key = (row["pid"], row.get("creation_time"))
                seen.setdefault(
                    key,
                    {
                        "source": str(role_path),
                        "prior": row,
                        "owned_by_command": False,
                    },
                )
    observations = []
    for (pid, birth), prior in seen.items():
        facts = (
            process_facts(pid, birth)
            if birth is not None
            else {"gap": "No birth identity was recorded; no PID-only attribution"}
        )
        state = terminal_identity(facts)
        if state == "unknown":
            identity_gaps.append({"pid": pid, "creation_time": birth, "identity": facts})
        if not prior.get("owned_by_command"):
            gaps.append(
                {
                    "pid": pid,
                    "creation_time": birth,
                    "gap": "Role receipt alone grants no command ownership or cleanup authority",
                }
            )
            if state == "live":
                identity_gaps.append(
                    {
                        "pid": pid,
                        "creation_time": birth,
                        "gap": "Live role-only identity lacks exact command-tree ownership",
                    }
                )
        observations.append(
            {
                "pid": pid,
                "creation_time": birth,
                **prior,
                "after_module_exit": facts,
                "state": state,
            }
        )
    survivors = [
        row for row in observations if row["state"] == "live" and row.get("owned_by_command")
    ]
    return {
        "at_monotonic": time.monotonic(),
        "seen_identities": observations,
        "survivors": survivors,
        "identity_gaps": identity_gaps,
        "evidence_gaps": gaps,
        "gate": "FAIL" if survivors else ("ERROR" if identity_gaps else "PASS"),
        "rule": "Exact post-exit judgment before intervention; unknown/reused facts never prove absence",
    }


def run_module(command, environment, directory, timeout):
    """Own the bounded real wait; judge descendants before emergency cleanup.

    Snapshot helpers have no reap/signal authority. This owner retains the
    existing emergency authority, exercised only after timeout or a failed gate.
    """
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
        outcome, normal_wait_completed = "ERROR", False
        wait_returned_at, finalization_started = None, None
        raw_capture_gaps = []
        captured, capture_gaps, interventions = {}, [], []
        descendants, immediate_post_exit = None, None
        finalization_gaps = set()
        try:
            deadline = started + timeout
            trace.record("module-normal-wait-start", timeout=timeout)
            while True:
                snapshot = descendant_facts(
                    child.pid,
                    leader.get("creation_time"),
                    "during module command",
                    directory / "cleanup-observations",
                )
                raw_capture_gaps.extend(snapshot["evidence_gaps"])
                # Root disappearance can race the real wait. Retain that raw
                # gap; lack of an initial birth cannot become ownership proof.
                if leader.get("creation_time") is None:
                    capture_gaps.extend(snapshot["evidence_gaps"])
                elif snapshot["root_identity"].get("identity") != "missing":
                    capture_gaps.extend(snapshot["evidence_gaps"])
                for row in snapshot["descendants"]:
                    key = (row["pid"], row["creation_time"])
                    entry = captured.setdefault(
                        key,
                        {
                            "source": "parent live tree",
                            "prior": row,
                            "owned_by_command": row["identity"].get("identity") == "same_birth",
                        },
                    )
                    entry["latest"] = row
                remaining = deadline - time.monotonic()
                try:
                    code = child.wait(timeout=max(0, min(0.1, remaining)))
                    normal_wait_completed = True
                    wait_returned_at = time.monotonic()
                    trace.record("module-normal-wait-return", exit=code)
                    break
                except subprocess.TimeoutExpired:
                    if remaining <= 0:
                        raise
            outcome = "PASS" if code == 0 else "FAIL"
        except subprocess.TimeoutExpired:
            outcome = "TIMEOUT"
            # Timeout is intervention, never a normal interpreter-exit claim.
            import psutil

            write(
                directory / "timeout-owned.json",
                [{"pid": pid, "creation_time": birth} for pid, birth in captured],
            )
            for pid, birth in reversed(captured):
                try:
                    facts = process_facts(pid, birth)
                    trace.record("timeout-before-emergency-cleanup", identity=facts)
                    item = psutil.Process(pid)
                    if (
                        captured[(pid, birth)]["owned_by_command"]
                        and terminal_identity(facts) == "live"
                        and item.create_time() == birth
                    ):
                        interventions.append({"kind": "timeout-descendant.kill", "identity": facts})
                        trace.invoke("timeout-descendant.kill", item.kill)
                except (OSError, psutil.Error) as exc:
                    capture_gaps.append(
                        {"pid": pid, "boundary": "timeout cleanup", "error": exception_facts(exc)}
                    )
            interventions.append({"kind": "timeout-module.kill", "identity": leader})
            trace.invoke("timeout-module.kill", child.kill)
            trace.invoke("timeout-module.wait", child.wait, timeout=15)
        finally:
            if child.returncode is not None:
                # The interpreter has really exited. Let its helpers observe
                # EOF/finalize naturally, bounded independently and without reap.
                finalization_started = time.monotonic()
                finalization_deadline = finalization_started + 5.0
                while True:
                    descendants = module_exit_observations(
                        directory, captured.items(), capture_gaps
                    )
                    if immediate_post_exit is None:
                        immediate_post_exit = descendants
                        trace.record("immediate-post-interpreter-exit", observation=descendants)
                    for row in descendants["seen_identities"]:
                        key = (row["pid"], row["creation_time"])
                        if row["state"] == "unknown" and key not in finalization_gaps:
                            finalization_gaps.add(key)
                            capture_gaps.append(
                                {
                                    "boundary": "passive post-exit observation",
                                    "identity": row["after_module_exit"],
                                }
                            )
                    if (
                        not normal_wait_completed
                        or descendants["gate"] == "PASS"
                        or time.monotonic() >= finalization_deadline
                    ):
                        break
                    time.sleep(0.05)
                trace.record("post-interpreter-survivor-judgment", observation=descendants)
                if outcome == "PASS" and descendants["gate"] != "PASS":
                    outcome = descendants["gate"]
                # Preserve the failed judgment and raw command exit even if this
                # emergency intervention subsequently removes a real survivor.
                for row in reversed(descendants["survivors"]):
                    before = process_facts(row["pid"], row["creation_time"])
                    emergency = {
                        "kind": "post-exit-emergency",
                        "before": before,
                        "at_monotonic": time.monotonic(),
                    }
                    interventions.append(emergency)
                    try:
                        import psutil

                        process = psutil.Process(row["pid"])
                        if (
                            terminal_identity(before) == "live"
                            and process.create_time() == row["creation_time"]
                        ):
                            trace.invoke("post-exit-emergency.kill", process.kill)
                            trace.invoke("post-exit-emergency.wait", process.wait, timeout=15)
                    except (OSError, psutil.Error) as exc:
                        emergency["error"] = exception_facts(exc)
                    emergency["after"] = process_facts(row["pid"], row["creation_time"])
                    row["emergency_cleanup"] = emergency
            write(
                directory / "module-exit.json",
                {
                    "outcome": outcome,
                    "elapsed": time.monotonic() - started,
                    "module_identity_at_start": leader,
                    "popen": popen_facts(child),
                    "wait_completed": child.returncode is not None,
                    "normal_wait_completed": normal_wait_completed,
                    "normal_wait_returned_monotonic": wait_returned_at,
                    "finalization_started_monotonic": finalization_started,
                    "finalization_allowance_seconds": 5.0,
                    "tree_observation_gaps": raw_capture_gaps,
                    "normal_wait_exit": code if normal_wait_completed else None,
                    "natural_eof0": (
                        normal_wait_completed
                        and code == 0
                        and not interventions
                        and descendants is not None
                        and descendants["gate"] == "PASS"
                    ),
                    "interventions": interventions,
                    "boundary": "parent after actual interpreter wait, then bounded passive finalization; cached returncode only",
                    "module_identity_after": (
                        process_facts(child.pid, leader["creation_time"])
                        if leader.get("creation_time") is not None
                        else {
                            "gap": "Original module birth unavailable; no PID-only post-exit attribution"
                        }
                    ),
                    "descendants": descendants,
                    "immediate_post_exit_descendants": immediate_post_exit,
                    "operations": trace.summary(),
                },
            )
        return child.returncode, outcome


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
            oracle = "tests.test_monitor_codex_e2e.ModelPinningIsEnforced.test_the_harness_refuses_a_run_on_another_model"
            assert oracle in declared
            oracle_directory = directory / "pin-oracle-command"
            oracle_directory.mkdir()
            oracle_command = [
                sys.executable,
                "-B",
                str(ROOT / "tests/prepared_model_method.py"),
                "--firmware-source",
                str(ROOT),
                "--pin-oracle",
                "--evidence",
                str(oracle_directory / "body"),
            ]
            oracle_record = {"method": oracle, "outcome": "UNRUN", "real_model_requests": 0}
            try:
                code, outcome = run_module(
                    oracle_command, environment, oracle_directory, args.module_timeout
                )
                result_path = oracle_directory / "body/result.json"
                if result_path.is_file():
                    oracle_record.update(json.loads(result_path.read_text(encoding="utf-8")))
                oracle_record.update(command=oracle_command, exit=code, command_outcome=outcome)
                if code != 0 or outcome != "PASS":
                    oracle_record["outcome"] = outcome if outcome != "PASS" else "FAIL"
            except Exception:
                oracle_record.update(outcome="ERROR", traceback=traceback.format_exc())
            record = {
                "module": module,
                "outcome": "BLOCKED",
                "reason": BLOCKED[module],
                "methods": {
                    name: oracle_record["outcome"] if name == oracle else "BLOCKED"
                    for name in declared
                },
                "deterministic_pin_oracle": oracle_record,
                "actual_model_requests": 0,
                "real_request_methods_blocked": len(declared) - 1,
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
                or outcome != "PASS"
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
