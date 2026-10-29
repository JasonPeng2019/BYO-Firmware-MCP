"""Run one unchanged real-model method with ROOT-provisioned disposable auth.

UNEXECUTED preparation fixture. ROOT must freeze this file and qualify a real
native account before use. It never reads/copies authentication and replaces
only the test fixture's auth-copy method, not any product decision or assertion.
Run all eighteen selectors in CASE-MATRIX.json exactly once in the fixed pass.
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
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
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

    oracle = type("DeterministicOriginalPinBody", (unittest.TestCase,), {
        "__module__": __name__, method: original,
        "_assert_model_was_honoured": observed,
    })
    with (evidence / "unittest.stderr.txt").open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(oracle(method))
    record = {
        "schema": "original-model-pin-oracle/v1",
        "method": "tests.test_monitor_codex_e2e.ModelPinningIsEnforced." + method,
        "loaded": 1, "testsRun": result.testsRun, "real_model_requests": 0,
        "original_body": original.__qualname__, "original_assertion": deciding.__qualname__,
        "source_hashes": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                          for name in ("tests/test_monitor_codex_e2e.py", "tests/codex_harness.py")},
        "fixture_scope": "Original deterministic body/assertion; real-agent class/module setup unused",
        "synthetic_header_checks": calls,
        "failures": [(test.id(), reason) for test, reason in result.failures],
        "errors": [(test.id(), reason) for test, reason in result.errors],
        "skipped": [(test.id(), reason) for test, reason in result.skipped],
        "outcome": "PASS" if result.wasSuccessful() and result.testsRun == 1
                   and not result.skipped and len(calls) == 2 else "FAIL",
    }
    dump(evidence / "result.json", record)
    print(json.dumps(record), flush=True)
    return int(record["outcome"] != "PASS")


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Model methods and pin oracle execute only in GitHub Actions")
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
    # Metadata-only checks precede every dependency/product/test import.
    qualification = json.loads(args.qualification.read_text(encoding="utf-8"))
    if qualification.get("schema") != "v-final-model-boundary/v1":
        raise RuntimeError("ROOT model-boundary qualification is missing")
    if qualification.get("acceptance_execution_authorized") is not True:
        raise RuntimeError("ROOT exact freeze/execution authority is required")
    for key in ("freeze_commit", "execution_card_sha256", "os_account_proof_sha256", "native_knownfolder_proof_sha256"):
        if not qualification.get(key):
            raise RuntimeError("missing ROOT boundary evidence: " + key)
    if qualification.get("model") != "gpt-5.6-luna":
        raise RuntimeError("preserve the test model pin; no silent migration")
    if qualification.get("all_homes_disposable_and_exclusive") is not True:
        raise RuntimeError("per-method native credential homes must be disposable and exclusive")
    if qualification.get("config_is_nonsecret_mcp_fixture") is not True:
        raise RuntimeError("do not open a config that may contain authentication material")
    private = Path(qualification["private_root"]).resolve(strict=True)
    profile = native_profile()
    if profile != Path(qualification["native_profile"]).resolve(strict=True):
        raise RuntimeError("actual OS token/passwd profile does not match ROOT qualification")
    if not profile.is_relative_to(private):
        raise RuntimeError("native profile is outside the qualified private boundary")
    if not Path(tempfile.gettempdir()).resolve().is_relative_to(private):
        raise RuntimeError("temporary projects would escape the private boundary")
    if os.environ.get("BYO_MCP_MONITOR_ROOT"):
        raise RuntimeError("existing model assertions use actual app-data storage; no projected monitor override")
    source = args.firmware_source.resolve(strict=True)
    target = source / "tests/test_monitor_codex_e2e.py"
    for relative, expected in qualification["source_hashes"].items():
        if hashlib.sha256((source / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError("model fixture source changed: " + relative)
    required = {"tests/test_monitor_codex_e2e.py", "tests/codex_harness.py", "tests/store_cleanup.py"}
    required.update(p.relative_to(source).as_posix() for p in (source / "src").rglob("*.py"))
    if not required <= set(qualification["source_hashes"]):
        raise RuntimeError("qualification omits deciding model fixtures")
    tree = ast.parse(target.read_text(encoding="utf-8"))
    selectors = {
        "tests.test_monitor_codex_e2e." + cls.name + "." + method.name
        for cls in tree.body if isinstance(cls, ast.ClassDef)
        for method in cls.body if isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
    }
    if len(selectors) != 18 or args.method not in selectors:
        raise RuntimeError("reconcile current model discovery before execution")
    home = Path(qualification["method_homes"][args.method]).resolve(strict=True)
    if not home.is_relative_to(private) or home in (private, profile):
        raise RuntimeError("model credential home is not an exact disposable child")
    config = home / "config.toml"
    if hashlib.sha256(config.read_bytes()).hexdigest() != qualification["config_sha256"][args.method]:
        raise RuntimeError("nonsecret preprovisioned MCP config differs from frozen input")
    # This file is explicitly a nonsecret fixture; authentication lives elsewhere
    # and is never opened. Pin the same single source MCP surface as the test.
    settings = tomllib.loads(config.read_text(encoding="utf-8"))
    if settings.get("model") != "gpt-5.6-luna":
        raise RuntimeError("preprovisioned config changed the required model")
    if settings.get("model_reasoning_effort") != "medium":
        raise RuntimeError("preserve the existing model fixture's effort setting")
    servers = settings.get("mcp_servers", {})
    expected_args = ["run", "--project", str(source).replace("\\", "/"), "pyocd-debug-mcp"]
    if set(servers) != {"byo_monitor_itest"}:
        raise RuntimeError("model config must expose only the frozen test server")
    selected = servers["byo_monitor_itest"]
    if selected.get("command") != "uv" or selected.get("args") != expected_args:
        raise RuntimeError("model fixture does not launch the exact source interface")
    if Path(selected.get("cwd", "")).resolve() != source:
        raise RuntimeError("preserve the source fixture's MCP cwd")
    if args.evidence.exists():
        raise RuntimeError("do not overwrite/rerun a reserved method result")
    args.evidence.mkdir(parents=True)
    os.environ["CODEX_HOME"] = str(home)
    sys.path[:0] = [str(source), str(source / "src"), str(source / "tests")]
    # Dependencies are real; no shim or product monkeypatch is installed.
    from platformdirs import user_data_dir

    app_data = Path(user_data_dir("BYO", appauthor=False, roaming=False)).resolve()
    if app_data != Path(qualification["native_app_data"]).resolve():
        raise RuntimeError("actual native app-data selector differs from KnownFolder qualification")
    if not app_data.is_relative_to(private):
        raise RuntimeError("model module snapshot/restore would touch unowned app data")
    if home.is_relative_to(app_data) or app_data.is_relative_to(home):
        raise RuntimeError("credential home and monitoring store must be disjoint")
    from tests import codex_harness

    expected_cadences = {
        codex_harness.SNAPSHOT_CADENCE_ENV: str(codex_harness.E2E_SNAPSHOT_CADENCE),
        codex_harness.CHECKIN_CADENCE_ENV: str(codex_harness.E2E_CHECKIN_CADENCE),
    }
    if selected.get("env") != expected_cadences:
        raise RuntimeError("preserve the original model fixture's exact cadence environment")
    original_home = codex_harness.CodexAgentTestCase._isolated_codex_home
    original_pin = codex_harness.CodexAgentTestCase._assert_model_was_honoured
    calls = []

    def prepared_home(self):
        return home  # Already authenticated by ROOT; no auth.json inspection/copy.

    def observed_pin(self, result):
        index = len(calls)
        (args.evidence / f"model-{index}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (args.evidence / f"model-{index}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        calls.append({"exit": result.returncode, "tool_calls": result.tool_calls,
                      "origin": "synthetic-pin-oracle" if ".ModelPinningIsEnforced." in args.method else "actual-codex-result"})
        return original_pin(self, result)

    codex_harness.CodexAgentTestCase._isolated_codex_home = prepared_home
    codex_harness.CodexAgentTestCase._assert_model_was_honoured = observed_pin
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(args.method)
        with (args.evidence / "unittest.stderr.txt").open("w", encoding="utf-8") as stream:
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        record = {"method": args.method, "testsRun": result.testsRun,
                  "failures": [(t.id(), reason) for t, reason in result.failures],
                  "errors": [(t.id(), reason) for t, reason in result.errors],
                  "skipped": [(t.id(), reason) for t, reason in result.skipped],
                  "model_result_boundaries": calls, "native_profile": str(profile),
                  "native_app_data": str(app_data),
                  "outcome": "ERROR" if result.errors else "FAIL" if result.failures else "SKIP" if result.skipped else "PASS" if result.wasSuccessful() and result.testsRun == 1 else "UNRUN"}
        dump(args.evidence / "result.json", record)
        return int(record["outcome"] != "PASS")
    except BaseException:
        dump(args.evidence / "exception.json", {"method": args.method, "outcome": "ERROR", "traceback": traceback.format_exc()})
        raise
    finally:
        codex_harness.CodexAgentTestCase._isolated_codex_home = original_home
        codex_harness.CodexAgentTestCase._assert_model_was_honoured = original_pin


if __name__ == "__main__":
    raise SystemExit(main())
