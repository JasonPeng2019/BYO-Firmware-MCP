"""Run one unchanged real-model method locally, or the deterministic pin oracle.

The pin oracle executes the original deterministic body with zero model requests
and may run anywhere, including CI. Each of the 17 genuine request methods runs
LOCALLY only -- never in GitHub Actions or on a self-hosted runner -- on the
operator's existing supported codex sign-in, with the user config ignored and the
suite's own frozen ``-c`` overrides applied. Nothing here reads or copies
authentication or opens a codex config, and no product decision, fixture or
assertion is replaced. The module's own fixtures snapshot and restore the
effective monitoring store byte for byte. Run all eighteen selectors in
CASE-MATRIX.json exactly once in the fixed pass.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
from ctypes import wintypes
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import traceback
import unittest


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def native_profile():
    if os.name != "nt":
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    token = wintypes.HANDLE()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size.value)
        if not userenv.GetUserProfileDirectoryW(token, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value).resolve()
    finally:
        kernel.CloseHandle(token)


def pin_oracle(source, evidence):
    """Execute the original deterministic body without request/auth/store fixtures.

    The body and deciding assertion are imported unchanged. Only the real-agent
    class/module setup is inapplicable: this body constructs two CodexResults and
    never starts a client, resolves a monitoring store, or needs credentials.
    """
    source = source.resolve(strict=True)
    evidence.mkdir(parents=True, exist_ok=False)
    sys.path[:0] = [str(source), str(source / "src"), str(source / "tests")]
    module = importlib.import_module("tests.test_monitor_codex_e2e")
    harness = importlib.import_module("tests.codex_harness")
    method = "test_the_harness_refuses_a_run_on_another_model"
    original = getattr(module.ModelPinningIsEnforced, method)
    deciding = harness.CodexAgentTestCase._assert_model_was_honoured
    calls = []

    def observed(self, result):
        calls.append({"stdout": result.stdout, "stderr": result.stderr})
        return deciding(self, result)

    oracle = type(
        "DeterministicOriginalPinBody",
        (unittest.TestCase,),
        {
            "__module__": __name__,
            method: original,
            "_assert_model_was_honoured": observed,
        },
    )
    with (evidence / "unittest.stderr.txt").open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(oracle(method))
    record = {
        "schema": "original-model-pin-oracle/v1",
        "method": "tests.test_monitor_codex_e2e.ModelPinningIsEnforced." + method,
        "loaded": 1,
        "testsRun": result.testsRun,
        "real_model_requests": 0,
        "original_body": original.__qualname__,
        "original_assertion": deciding.__qualname__,
        "source_hashes": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("tests/test_monitor_codex_e2e.py", "tests/codex_harness.py")
        },
        "fixture_scope": "Original deterministic body/assertion; real-agent class/module setup unused",
        "synthetic_header_checks": calls,
        "failures": [(test.id(), reason) for test, reason in result.failures],
        "errors": [(test.id(), reason) for test, reason in result.errors],
        "skipped": [(test.id(), reason) for test, reason in result.skipped],
        "outcome": "PASS"
        if result.wasSuccessful()
        and result.testsRun == 1
        and not result.skipped
        and len(calls) == 2
        else "FAIL",
    }
    dump(evidence / "result.json", record)
    print(json.dumps(record), flush=True)
    return int(record["outcome"] != "PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--firmware-source", type=Path, required=True)
    parser.add_argument("--method")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--pin-oracle", action="store_true")
    args = parser.parse_args()
    if args.pin_oracle:
        if args.qualification or args.method or args.execute:
            parser.error("pin oracle is independent of authenticated model execution")
        return pin_oracle(args.firmware_source, args.evidence)
    if not args.qualification or not args.method or not args.execute:
        parser.error("real model method requires qualification, method and explicit execute")
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("RUNNER_ENVIRONMENT"):
        raise SystemExit("Real model methods run locally only, never in GitHub Actions")
    # Metadata-only checks precede every dependency/product/test import.
    qualification = json.loads(args.qualification.read_text(encoding="utf-8"))
    if qualification.get("schema") != "v-final-model-boundary/v3":
        raise RuntimeError("local model-boundary qualification is missing")
    if qualification.get("acceptance_execution_authorized") is not True:
        raise RuntimeError("ROOT exact freeze/execution authority is required")
    for key in (
        "freeze_commit",
        "execution_card_sha256",
        "native_knownfolder_proof_sha256",
    ):
        if not qualification.get(key):
            raise RuntimeError("missing ROOT boundary evidence: " + key)
    if qualification.get("model") != "gpt-5.6-luna":
        raise RuntimeError("preserve the test model pin; no silent migration")
    if qualification.get("codex_sign_in") != "existing-supported":
        raise RuntimeError("use the operator's existing supported codex sign-in")
    private = Path(qualification["private_root"]).resolve(strict=True)
    profile = native_profile()
    if profile != Path(qualification["native_profile"]).resolve(strict=True):
        raise RuntimeError("actual OS token/passwd profile does not match the qualification")
    if not Path(tempfile.gettempdir()).resolve().is_relative_to(private):
        raise RuntimeError("temporary projects would escape the private boundary")
    configured_monitor = os.environ.get("BYO_MCP_MONITOR_ROOT", "").strip()
    if not configured_monitor or not Path(configured_monitor).is_absolute():
        raise RuntimeError("prepared local requests require an absolute scoped monitor root")
    monitor_root = Path(configured_monitor).resolve()
    if monitor_root != Path(qualification["monitor_root"]).resolve():
        raise RuntimeError("effective monitor root differs from the frozen qualification")
    if monitor_root == private or not monitor_root.is_relative_to(private):
        raise RuntimeError("monitor root must be a dedicated child of the private boundary")
    if monitor_root.exists():
        raise RuntimeError("each real method requires a fresh owned monitor root")
    source = args.firmware_source.resolve(strict=True)
    target = source / "tests/test_monitor_codex_e2e.py"
    for relative, expected in qualification["source_hashes"].items():
        if hashlib.sha256((source / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError("model fixture source changed: " + relative)
    required = {
        "tests/test_monitor_codex_e2e.py",
        "tests/codex_harness.py",
        "tests/store_cleanup.py",
        "uv.lock",
        "pyproject.toml",
    }
    required.update(p.relative_to(source).as_posix() for p in (source / "src").rglob("*.py"))
    if not required <= set(qualification["source_hashes"]):
        raise RuntimeError("qualification omits deciding model fixtures")
    tree = ast.parse(target.read_text(encoding="utf-8"))
    selectors = {
        "tests.test_monitor_codex_e2e." + cls.name + "." + method.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for method in cls.body
        if isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
    }
    if len(selectors) != 18 or args.method not in selectors:
        raise RuntimeError("reconcile current model discovery before execution")
    if ".ModelPinningIsEnforced." in args.method:
        raise RuntimeError(
            "run the deterministic original body with --pin-oracle; no auth required"
        )
    methods = qualification["methods"]
    if args.method not in methods or set(methods) != {
        s for s in selectors if ".ModelPinningIsEnforced." not in s
    }:
        raise RuntimeError("qualification must list exactly the 17 genuine request methods")
    # The existing sign-in's home, by path only; it is never opened.
    codex_home = Path(os.environ.get("CODEX_HOME") or profile / ".codex").resolve()
    if args.evidence.exists():
        raise RuntimeError("do not overwrite/rerun a reserved method result")
    args.evidence.mkdir(parents=True)
    sys.path[:0] = [str(source), str(source / "src"), str(source / "tests")]
    # Dependencies are real; no shim or product monkeypatch is installed.
    from platformdirs import user_data_dir

    app_data = Path(user_data_dir("BYO", appauthor=False, roaming=False)).resolve()
    if app_data != Path(qualification["native_app_data"]).resolve():
        raise RuntimeError("actual native app-data selector differs from KnownFolder qualification")
    # Native default selection is qualified separately. Real-model traffic uses
    # the supported exclusive launch root; no shared application-data content is
    # read, copied or restored by these fixtures.
    if codex_home.is_relative_to(app_data) or app_data.is_relative_to(codex_home):
        raise RuntimeError("credential home and monitoring store must be disjoint")
    if (
        codex_home.is_relative_to(monitor_root)
        or monitor_root.is_relative_to(codex_home)
        or monitor_root.is_relative_to(app_data)
        or app_data.is_relative_to(monitor_root)
    ):
        raise RuntimeError("scoped store, native shared store and credential home must be disjoint")
    from tests import codex_harness

    # Pin the same single source MCP surface as the test: the frozen overrides
    # are the run's entire codex configuration.
    overrides = codex_harness.codex_overrides()
    settings = {}
    for pair in overrides:
        key, _, value = pair.partition("=")
        *parents, leaf = key.split(".")
        node = settings
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = tomllib.loads("value = " + value)["value"]
    if settings.get("model") != "gpt-5.6-luna":
        raise RuntimeError("codex overrides changed the required model")
    if settings.get("model_reasoning_effort") != "medium":
        raise RuntimeError("preserve the existing model fixture's effort setting")
    servers = settings.get("mcp_servers", {})
    expected_args = [
        "run",
        "--no-sync",
        "--no-build",
        "--project",
        str(source).replace("\\", "/"),
        "pyocd-debug-mcp",
    ]
    if set(servers) != {"byo_monitor_itest"}:
        raise RuntimeError("model config must expose only the frozen test server")
    selected = servers["byo_monitor_itest"]
    if selected.get("command") != "uv" or selected.get("args") != expected_args:
        raise RuntimeError("model fixture does not launch the exact source interface")
    if Path(selected.get("cwd", "")).resolve() != source:
        raise RuntimeError("preserve the source fixture's MCP cwd")
    expected_cadences = {
        codex_harness.SNAPSHOT_CADENCE_ENV: str(codex_harness.E2E_SNAPSHOT_CADENCE),
        codex_harness.CHECKIN_CADENCE_ENV: str(codex_harness.E2E_CHECKIN_CADENCE),
        "BYO_MCP_MONITOR_ROOT": str(monitor_root),
    }
    runtime = Path(qualification["prebuilt_environment"]).resolve(strict=True)
    if not runtime.is_relative_to(private) or runtime == private:
        raise RuntimeError("prebuilt environment must be an owned private child")
    if os.environ.get("UV_PROJECT_ENVIRONMENT") != str(runtime):
        raise RuntimeError("uv must select the qualified prebuilt environment")
    expected_cadences["UV_PROJECT_ENVIRONMENT"] = str(runtime)
    console = runtime / (
        "Scripts/pyocd-debug-mcp.exe" if os.name == "nt" else "bin/pyocd-debug-mcp"
    )
    if hashlib.sha256(console.read_bytes()).hexdigest() != qualification["console_sha256"]:
        raise RuntimeError("exact Actions-wheel console is missing or changed")
    package_receipt = Path(qualification["prebuilt_package_receipt"]).resolve(strict=True)
    if (
        hashlib.sha256(package_receipt.read_bytes()).hexdigest()
        != qualification["prebuilt_package_receipt_sha256"]
    ):
        raise RuntimeError("Actions wheel qualification changed")
    package = json.loads(package_receipt.read_bytes())
    if (
        package.get("outcome") != "PASS"
        or package.get("schema") != "actions-prebuilt-source-console/v1"
    ):
        raise RuntimeError("exact successful Actions source-wheel proof is required")
    if package["uv_lock_sha256"] != hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest():
        raise RuntimeError("prebuilt wheel uses a different dependency lock")
    package_roots = (
        list(runtime.glob("Lib/site-packages/pyocd_debug_mcp"))
        if os.name == "nt"
        else list(runtime.glob("lib/python*/site-packages/pyocd_debug_mcp"))
    )
    if len(package_roots) != 1:
        raise RuntimeError("prebuilt environment does not contain exactly one source package")
    installed = {
        "pyocd_debug_mcp/" + p.relative_to(package_roots[0]).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in package_roots[0].rglob("*.py")
    }
    candidate = {
        p.relative_to(source / "src").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (source / "src/pyocd_debug_mcp").rglob("*.py")
    }
    if installed != candidate or installed != package["python_payload"]:
        raise RuntimeError("installed source wheel differs from the frozen exact candidate")
    if selected.get("env") != expected_cadences:
        raise RuntimeError("preserve exact cadences and the qualified exclusive monitor root")
    if codex_harness._store_root() != monitor_root:
        raise RuntimeError("test assertions and actual child must use the same scoped store")
    original_pin = codex_harness.CodexAgentTestCase._assert_model_was_honoured
    calls = []

    def observed_pin(self, result):
        index = len(calls)
        (args.evidence / f"model-{index}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (args.evidence / f"model-{index}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        calls.append(
            {
                "exit": result.returncode,
                "tool_calls": result.tool_calls,
                "origin": "synthetic-pin-oracle"
                if ".ModelPinningIsEnforced." in args.method
                else "actual-codex-result",
            }
        )
        return original_pin(self, result)

    codex_harness.CodexAgentTestCase._assert_model_was_honoured = observed_pin
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(args.method)
        with (args.evidence / "unittest.stderr.txt").open("w", encoding="utf-8") as stream:
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        record = {
            "method": args.method,
            "testsRun": result.testsRun,
            "failures": [(t.id(), reason) for t, reason in result.failures],
            "errors": [(t.id(), reason) for t, reason in result.errors],
            "skipped": [(t.id(), reason) for t, reason in result.skipped],
            "model_result_boundaries": calls,
            "execution": "local",
            "codex_sign_in": "existing-supported",
            "codex_home": str(codex_home),
            "codex_overrides": overrides,
            "native_profile": str(profile),
            "native_app_data": str(app_data),
            "monitor_root": str(monitor_root),
            "shared_store_content_access": False,
            "outcome": "ERROR"
            if result.errors
            else "FAIL"
            if result.failures
            else "SKIP"
            if result.skipped
            else "PASS"
            if result.wasSuccessful() and result.testsRun == 1
            else "UNRUN",
        }
        dump(args.evidence / "result.json", record)
        return int(record["outcome"] != "PASS")
    except BaseException:
        dump(
            args.evidence / "exception.json",
            {"method": args.method, "outcome": "ERROR", "traceback": traceback.format_exc()},
        )
        raise
    finally:
        codex_harness.CodexAgentTestCase._assert_model_was_honoured = original_pin


if __name__ == "__main__":
    raise SystemExit(main())
