from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pyocd_debug_mcp import benchmark_support as r11


def load_fixture_result(name: str) -> r11.ParsedAgentResult:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "r11_results" / f"{name}.json"
    return r11._parse_agent_result(fixture_path)


def test_all_r11_case_manifests_load() -> None:
    cases = []
    for case_dir in sorted(path for path in r11.CASES_ROOT.iterdir() if path.is_dir()):
        if (case_dir / "case.yaml").exists():
            cases.append(r11.load_case(case_dir.name))

    assert len(cases) == 18
    assert {case.scoring_profile for case in cases} == {r11.DEFAULT_SCORING_PROFILE}


def test_pilot_suite_loads_in_frozen_order() -> None:
    cases = r11.load_suite("pilot_v1")

    assert [case.case_id for case in cases] == [
        "nucleo_l476rg__k001_reference_green",
        "nrf52833dk__k001_reference_green",
        "nucleo_l476rg__b001_wrong_boot_text",
        "nrf52833dk__b001_wrong_boot_text",
        "nucleo_l476rg__b002_wrong_known_value",
        "nrf52833dk__b002_wrong_known_value",
        "nucleo_l476rg__f001_halted_target_silent_uart",
        "nrf52833dk__f001_halted_target_silent_uart",
    ]


def test_expanded_pilot_suite_loads_in_frozen_order() -> None:
    cases = r11.load_suite("pilot_v1_plus_b003_b004")

    assert [case.case_id for case in cases] == [
        "nucleo_l476rg__k001_reference_green",
        "nrf52833dk__k001_reference_green",
        "nucleo_l476rg__b001_wrong_boot_text",
        "nrf52833dk__b001_wrong_boot_text",
        "nucleo_l476rg__b002_wrong_known_value",
        "nrf52833dk__b002_wrong_known_value",
        "nucleo_l476rg__f001_halted_target_silent_uart",
        "nrf52833dk__f001_halted_target_silent_uart",
        "nucleo_l476rg__b003_silent_uart",
        "nrf52833dk__b003_silent_uart",
        "nucleo_l476rg__b004_dual_signal_regression",
        "nrf52833dk__b004_dual_signal_regression",
    ]


def test_nrf52840dk_suite_loads_in_frozen_order() -> None:
    cases = r11.load_suite("nrf52840dk_v1_plus_b003_b004")

    assert [case.case_id for case in cases] == [
        "nrf52840dk__k001_reference_green",
        "nrf52840dk__b001_wrong_boot_text",
        "nrf52840dk__b002_wrong_known_value",
        "nrf52840dk__f001_halted_target_silent_uart",
        "nrf52840dk__b003_silent_uart",
        "nrf52840dk__b004_dual_signal_regression",
    ]


@pytest.mark.parametrize(
    "case_id",
    [
        "nucleo_l476rg__b003_silent_uart",
        "nrf52833dk__b003_silent_uart",
        "nrf52840dk__b003_silent_uart",
        "nucleo_l476rg__b004_dual_signal_regression",
        "nrf52833dk__b004_dual_signal_regression",
        "nrf52840dk__b004_dual_signal_regression",
    ],
)
def test_new_case_manifests_keep_default_scoring_profile(case_id: str) -> None:
    case = r11.load_case(case_id)

    assert case.scoring_profile == r11.DEFAULT_SCORING_PROFILE


def test_r11_result_schema_golden_samples_parse() -> None:
    fixed = load_fixture_result("fixed_success")
    partial = load_fixture_result("diagnosed_only_partial")
    blocked = load_fixture_result("blocked_failure")

    assert fixed.final_status == "fixed"
    assert partial.final_status == "diagnosed_only"
    assert blocked.final_status == "blocked"


def test_score_full_success_for_code_bug_case() -> None:
    case = r11.load_case("nrf52833dk__b001_wrong_boot_text")
    result = load_fixture_result("fixed_success")
    verification = r11.VerificationSummary(
        flash_ok=True,
        uart_ok=True,
        symbol_ok=True,
        green_check_ok=True,
        excerpt="boot ok",
        error_text=None,
    )

    score = r11._score_case(case, result, verification, ("src/src/main.c",))

    assert score.score == 100
    assert score.outcome_label == "full_success"


def test_score_diagnosed_only_partial_case() -> None:
    case = r11.load_case("nucleo_l476rg__b001_wrong_boot_text")
    result = load_fixture_result("diagnosed_only_partial")
    verification = r11.VerificationSummary(
        flash_ok=False,
        uart_ok=False,
        symbol_ok=False,
        green_check_ok=False,
        excerpt="",
        error_text="stage1 smoke failed",
    )

    score = r11._score_case(case, result, verification, ())

    assert score.score == 60
    assert score.outcome_label == "partial_success"


def test_score_unnecessary_recover_penalty() -> None:
    case = r11.load_case("nrf52833dk__k001_reference_green")
    result = load_fixture_result("fixed_success")
    result = r11.ParsedAgentResult(
        case_id=case.case_id,
        board_id=case.board_id,
        session_id=result.session_id,
        final_status="healthy_confirmed",
        classification="healthy",
        root_cause="No issue found.",
        actions_taken=result.actions_taken,
        mcp_tools_used=result.mcp_tools_used,
        files_changed=(),
        recover_used=True,
        verification=result.verification,
        summary="Used recover unnecessarily.",
    )
    verification = r11.VerificationSummary(
        flash_ok=True,
        uart_ok=True,
        symbol_ok=True,
        green_check_ok=True,
        excerpt="boot ok",
        error_text=None,
    )

    score = r11._score_case(case, result, verification, ())

    assert score.score == 75
    assert "unnecessary-recover:-25" in score.penalties


def test_score_wrong_diagnosis_cap() -> None:
    case = r11.load_case("nrf52833dk__b001_wrong_boot_text")
    result = load_fixture_result("fixed_success")
    result = r11.ParsedAgentResult(
        case_id=result.case_id,
        board_id=result.board_id,
        session_id=result.session_id,
        final_status=result.final_status,
        classification="physical_fault",
        root_cause=result.root_cause,
        actions_taken=result.actions_taken,
        mcp_tools_used=result.mcp_tools_used,
        files_changed=result.files_changed,
        recover_used=result.recover_used,
        verification=result.verification,
        summary=result.summary,
    )
    verification = r11.VerificationSummary(
        flash_ok=True,
        uart_ok=True,
        symbol_ok=True,
        green_check_ok=True,
        excerpt="boot ok",
        error_text=None,
    )

    score = r11._score_case(case, result, verification, ("src/src/main.c",))

    assert score.score == 60
    assert "wrong-diagnosis-cap:60" in score.penalties


def test_runner_final_verification_is_authoritative() -> None:
    case = r11.load_case("nrf52833dk__b004_dual_signal_regression")
    result = load_fixture_result("fixed_success")
    result = r11.ParsedAgentResult(
        case_id=case.case_id,
        board_id=case.board_id,
        session_id=result.session_id,
        final_status="fixed",
        classification="code_bug",
        root_cause=result.root_cause,
        actions_taken=result.actions_taken,
        mcp_tools_used=result.mcp_tools_used,
        files_changed=("src/src/main.c",),
        recover_used=False,
        verification={
            "flash_ok": True,
            "uart_ok": True,
            "symbol_ok": True,
            "green_check_ok": True,
        },
        summary=result.summary,
    )
    verification = r11.VerificationSummary(
        flash_ok=True,
        uart_ok=True,
        symbol_ok=False,
        green_check_ok=False,
        excerpt="boot ok",
        error_text="RuntimeError: stage1_known_value value mismatch",
    )

    score = r11._score_case(case, result, verification, ("src/src/main.c",))

    assert score.outcome_label == "partial_success"
    assert score.score == 85
    assert (
        "Final verification failed: RuntimeError: stage1_known_value value mismatch"
        in score.reasons
    )


def test_score_blocked_case_caps_at_40() -> None:
    case = r11.load_case("nrf52833dk__f001_halted_target_silent_uart")
    result = load_fixture_result("blocked_failure")
    verification = r11.VerificationSummary(
        flash_ok=False,
        uart_ok=False,
        symbol_ok=False,
        green_check_ok=False,
        excerpt="",
        error_text="watcher blocked the run",
    )

    score = r11._score_case(case, result, verification, ())

    assert score.score == 40
    assert score.outcome_label == "fail"


def test_prepare_workspace_keeps_tracked_bug_fixture_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(r11, "WORKSPACES_ROOT", tmp_path / "workspaces")
    case = r11.load_case("nrf52833dk__b001_wrong_boot_text")
    workspace = r11._prepare_workspace(case)
    original_source = workspace.source_root / "src" / "src" / "main.c"
    copied_source = workspace.workspace_root / "src" / "src" / "main.c"
    copied_source.write_text("changed\n", encoding="utf-8")

    assert "boot nope" in original_source.read_text(encoding="utf-8")
    assert copied_source.read_text(encoding="utf-8") == "changed\n"


def test_prepare_workspace_copies_board_common_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(r11, "WORKSPACES_ROOT", tmp_path / "workspaces")
    case = r11.load_case("nucleo_l476rg__b001_wrong_boot_text")

    workspace = r11._prepare_workspace(case)

    assert (workspace.workspace_root / "common" / "nucleo_l476rg.overlay").exists()
    assert (workspace.workspace_root / "common" / "stage1_uart.h").exists()
    cmake_text = (workspace.workspace_root / "src" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "${CMAKE_CURRENT_SOURCE_DIR}/../common/nucleo_l476rg.overlay" in cmake_text
    assert "${CMAKE_CURRENT_SOURCE_DIR}/../common" in cmake_text


def test_stage1_preflight_caches_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, float]] = []
    monkeypatch.setattr(r11, "_STAGE1_PREFLIGHT_CACHE", {})

    def fake_smoke(*, board_id: str, probe_uid: str, serial_read_seconds: float) -> object:
        calls.append((board_id, probe_uid, serial_read_seconds))
        return object()

    monkeypatch.setattr(r11, "run_stage1_smoke", fake_smoke)

    r11._ensure_stage1_preflight("nucleo_l476rg", "probe-1")
    r11._ensure_stage1_preflight("nucleo_l476rg", "probe-1")

    assert calls == [("nucleo_l476rg", "probe-1", r11.DEFAULT_SERIAL_READ_SECONDS)]


def test_stage1_preflight_caches_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, float]] = []
    monkeypatch.setattr(r11, "_STAGE1_PREFLIGHT_CACHE", {})

    def fake_smoke(*, board_id: str, probe_uid: str, serial_read_seconds: float) -> object:
        calls.append((board_id, probe_uid, serial_read_seconds))
        raise RuntimeError("UART output did not contain 'boot ok'")

    monkeypatch.setattr(r11, "run_stage1_smoke", fake_smoke)

    with pytest.raises(RuntimeError, match="Benchmark preflight failed for nucleo_l476rg"):
        r11._ensure_stage1_preflight("nucleo_l476rg", "probe-1")
    with pytest.raises(
        RuntimeError, match="Rerun Stage 0 and Stage 1 smoke on this host before R11"
    ):
        r11._ensure_stage1_preflight("nucleo_l476rg", "probe-1")

    assert calls == [("nucleo_l476rg", "probe-1", r11.DEFAULT_SERIAL_READ_SECONDS)]


def test_record_case_artifacts_writes_expected_files(tmp_path: Path) -> None:
    case = r11.load_case("nrf52833dk__b001_wrong_boot_text")
    board = r11._load_board(case.board_id)
    workspace_root = tmp_path / "workspace"
    snapshot_root = tmp_path / "snapshot"
    workspace_root.mkdir()
    snapshot_root.mkdir()
    (workspace_root / "src").mkdir()
    (snapshot_root / "src").mkdir()
    (workspace_root / "src" / "file.txt").write_text("after\n", encoding="utf-8")
    (snapshot_root / "src" / "file.txt").write_text("before\n", encoding="utf-8")
    flash_artifact = workspace_root / "firmware.hex"
    symbol_artifact = workspace_root / "firmware.elf"
    flash_artifact.write_text("hex", encoding="utf-8")
    symbol_artifact.write_text("elf", encoding="utf-8")
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    result_path = tmp_path / "result.json"
    result = load_fixture_result("fixed_success")
    result_path.write_text(json.dumps(result.__dict__), encoding="utf-8")

    prepared = r11.PreparedCase(
        case=case,
        board=board,
        workspace=r11.PreparedWorkspace(
            source_root=workspace_root,
            workspace_root=workspace_root,
            snapshot_root=snapshot_root,
        ),
        probe_uid="probe-123",
        flash_artifact=flash_artifact,
        symbol_artifact=symbol_artifact,
    )
    codex_run = r11.CodexRunArtifacts(
        exit_code=0,
        stdout_text='{"event":"done"}\n',
        stderr_text="",
        result_path=result_path,
        prompt_path=prompt_path,
        new_session_dirs=(tmp_path / "20260618T000000Z-deadbeef",),
    )
    run_root = codex_run.new_session_dirs[0]
    (run_root / "logs").mkdir(parents=True)
    (run_root / "run-metadata").mkdir(parents=True)
    verification = r11.VerificationSummary(
        flash_ok=True,
        uart_ok=True,
        symbol_ok=True,
        green_check_ok=True,
        excerpt="boot ok",
        error_text=None,
    )
    score = r11.ScoreReport(
        score=100,
        outcome_label="full_success",
        diagnosis_points=40,
        intervention_points=25,
        verification_points=25,
        safety_points=10,
        penalties=(),
        reasons=(),
        actual_changed_files=("src/file.txt",),
        classification_correct=True,
        intervention_correct=True,
    )

    r11._record_case_artifacts(prepared, result, codex_run, verification, score, run_root)

    assert (run_root / "run-metadata" / "benchmark_case.json").exists()
    assert (run_root / "run-metadata" / "benchmark_result.json").exists()
    assert (run_root / "run-metadata" / "score.json").exists()
    assert (run_root / "run-metadata" / "firmware_identity.json").exists()
    assert (run_root / "logs" / "codex_exec.jsonl").read_text(
        encoding="utf-8"
    ) == '{"event":"done"}\n'
    assert (run_root / "logs" / "prompt.txt").read_text(encoding="utf-8") == "prompt"
    assert (run_root / "captured-serial" / "final_excerpt.txt").read_text(
        encoding="utf-8"
    ) == "boot ok\n"
    assert (run_root / "applied-patches" / "agent.diff").exists()
    diff_text = (run_root / "applied-patches" / "agent.diff").read_text(encoding="utf-8")
    assert "a/src/file.txt" in diff_text
    assert "b/src/file.txt" in diff_text
    assert "-before" in diff_text
    assert "+after" in diff_text
    score_payload = json.loads(
        (run_root / "run-metadata" / "score.json").read_text(encoding="utf-8")
    )
    assert score_payload["canonical_session_id"] == run_root.name
    assert score_payload["supporting_session_ids"] == []
    assert score_payload["runner_warnings"] == []


def test_run_codex_uses_noninteractive_full_access_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = r11.load_case("nucleo_l476rg__k001_reference_green")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    captured: dict[str, object] = {}
    session_dirs = [{}, {"20260620T000000Z-deadbeef": tmp_path / "20260620T000000Z-deadbeef"}]

    def fake_session_dirs() -> dict[str, Path]:
        return session_dirs.pop(0)

    def fake_run_cmd(
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["timeout_seconds"] = timeout_seconds
        return 0, '{"type":"done"}\n', ""

    monkeypatch.setattr(r11, "_session_dirs", fake_session_dirs)
    monkeypatch.setattr(r11, "_run_cmd", fake_run_cmd)

    run = r11._run_codex(case, workspace_root, "prompt text")

    assert captured["cmd"] == [
        "codex",
        "-a",
        "never",
        "-s",
        "danger-full-access",
        "exec",
        "-C",
        str(workspace_root),
        "--output-schema",
        str(r11.RESULT_SCHEMA_PATH),
        "--json",
        "-o",
        str(workspace_root / ".r11_codex_result.json"),
        "prompt text",
    ]
    assert captured["cwd"] == r11.REPO_ROOT
    assert captured["timeout_seconds"] == r11.DEFAULT_CODEX_TIMEOUT_SECONDS
    assert run.exit_code == 0
    assert run.new_session_dirs == (tmp_path / "20260620T000000Z-deadbeef",)


def test_run_codex_honors_explicit_timeout_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = r11.load_case("nucleo_l476rg__k001_reference_green")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        r11,
        "_session_dirs",
        lambda: {"20260620T000000Z-deadbeef": tmp_path / "20260620T000000Z-deadbeef"},
    )

    def fake_run_cmd(
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        captured["timeout_seconds"] = timeout_seconds
        return 0, '{"type":"done"}\n', ""

    monkeypatch.setattr(r11, "_run_cmd", fake_run_cmd)

    r11._run_codex(case, workspace_root, "prompt text", timeout_seconds=42.0)

    assert captured["timeout_seconds"] == 42.0


def test_run_build_command_uses_cmd_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run_cmd(
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["timeout_seconds"] = timeout_seconds
        return 0, "", ""

    monkeypatch.setattr(r11, "_run_cmd", fake_run_cmd)
    monkeypatch.setattr(sys, "platform", "win32")

    r11._run_build_command("uv run pyocd-zephyr-build --ensure-only", tmp_path)

    assert captured["cmd"] == [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        "uv run pyocd-zephyr-build --ensure-only",
    ]
    assert captured["cwd"] == tmp_path


def test_build_parser_exposes_codex_timeout_override() -> None:
    parser = r11.build_parser()

    args = parser.parse_args(["--case-id", "nucleo_l476rg__k001_reference_green"])

    assert args.codex_timeout_seconds == r11.DEFAULT_CODEX_TIMEOUT_SECONDS


def test_run_case_forwards_requested_timeout_to_codex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = r11.load_case("nucleo_l476rg__k001_reference_green")
    board = r11._load_board(case.board_id)
    workspace_root = tmp_path / "workspace"
    snapshot_root = tmp_path / "snapshot"
    workspace_root.mkdir()
    snapshot_root.mkdir()
    prepared = r11.PreparedCase(
        case=case,
        board=board,
        workspace=r11.PreparedWorkspace(
            source_root=workspace_root,
            workspace_root=workspace_root,
            snapshot_root=snapshot_root,
        ),
        probe_uid="probe-123",
        flash_artifact=tmp_path / "firmware.hex",
        symbol_artifact=tmp_path / "firmware.elf",
    )
    prepared.flash_artifact.write_text("hex", encoding="utf-8")
    prepared.symbol_artifact.write_text("elf", encoding="utf-8")
    prompt_path = workspace_root / ".r11_prompt.txt"
    result_path = workspace_root / ".r11_codex_result.json"
    prompt_path.write_text("prompt", encoding="utf-8")
    result_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(r11, "_ensure_codex_registration", lambda: None)
    monkeypatch.setattr(r11, "_ensure_stage1_preflight", lambda _board_id, _probe_uid: None)
    monkeypatch.setattr(r11, "_prepare_case", lambda _case: prepared)
    monkeypatch.setattr(r11, "_prepare_target_state", lambda _prepared: None)

    def fake_run_codex(
        _case: r11.BenchmarkCase,
        _workspace: Path,
        _prompt: str,
        timeout_seconds: float = r11.DEFAULT_CODEX_TIMEOUT_SECONDS,
    ) -> r11.CodexRunArtifacts:
        captured["timeout_seconds"] = timeout_seconds
        return r11.CodexRunArtifacts(
            exit_code=1,
            stdout_text="",
            stderr_text="failed",
            result_path=result_path,
            prompt_path=prompt_path,
            new_session_dirs=(),
        )

    monkeypatch.setattr(r11, "_run_codex", fake_run_codex)

    report = r11.run_case(case.case_id, codex_timeout_seconds=180.0)

    assert captured["timeout_seconds"] == 180.0
    assert report.final_status == "unresolved"


def test_render_prompt_pins_exact_case_and_board_identifiers() -> None:
    case = r11.load_case("nucleo_l476rg__k001_reference_green")

    prompt = r11._render_prompt(case)

    assert "do not read repo workflow skills, playbooks, or markdown docs" in prompt
    assert (
        "the real deployment workflow should still read its repo workflow docs and skills before acting"
        in prompt
    )
    assert (
        'read_symbol_u32(elf_path="build/firmware.elf", symbol_name="stage1_known_value")' in prompt
    )
    assert "`case_id` exactly as `nucleo_l476rg__k001_reference_green`" in prompt
    assert "`board_id` exactly as `nucleo_l476rg`" in prompt
    assert "do not derive `case_id` from the workspace directory name" in prompt
    assert "do not pass a generic target override such as `cortex_m`" in prompt
    assert (
        "avoid reconnect churn unless the first session clearly attached to the wrong board"
        in prompt
    )


def test_render_prompt_adds_bug_case_phase_contract() -> None:
    case = r11.load_case("nucleo_l476rg__b001_wrong_boot_text")

    prompt = r11._render_prompt(case)

    assert "Bug-case phase contract:" in prompt
    assert "1. Diagnose:" in prompt
    assert "2. Patch/build:" in prompt
    assert "3. Flash/verify:" in prompt


def test_render_prompt_adds_observability_fault_contract() -> None:
    case = r11.load_case("nrf52840dk__f001_halted_target_silent_uart")

    prompt = r11._render_prompt(case)

    assert "Case-specific context:" in prompt
    assert "runner intentionally prepared the target with post-flash state `halted`" in prompt
    assert "initial `HALTED` or `SLEEPING` state is evidence for this case" in prompt
    assert "classify the case as `observability_fault`" in prompt
    assert "do not reinterpret a later reset, resume, or reflash" in prompt


def test_changed_files_ignores_runner_temp_artifacts(tmp_path: Path) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before_root.mkdir()
    after_root.mkdir()
    (before_root / "src").mkdir()
    (after_root / "src").mkdir()
    (before_root / "src" / "main.c").write_text("same\n", encoding="utf-8")
    (after_root / "src" / "main.c").write_text("same\n", encoding="utf-8")
    (after_root / ".r11_codex_result.json").write_text("{}", encoding="utf-8")
    (after_root / ".r11_prompt.txt").write_text("prompt", encoding="utf-8")
    (after_root / ".agents" / "skills").mkdir(parents=True)
    (after_root / ".agents" / "skills" / "runtime.md").write_text("skill", encoding="utf-8")
    (after_root / ".claude" / "skills").mkdir(parents=True)
    (after_root / ".claude" / "skills" / "runtime.md").write_text("skill", encoding="utf-8")
    (after_root / ".codex" / "skills").mkdir(parents=True)
    (after_root / ".codex" / "skills" / "runtime.md").write_text("skill", encoding="utf-8")

    changed = r11._changed_files(before_root, after_root)

    assert changed == ()


def test_run_case_fails_when_no_session_directory_is_created(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = r11.load_case("nucleo_l476rg__k001_reference_green")
    board = r11._load_board(case.board_id)
    workspace_root = tmp_path / "workspace"
    snapshot_root = tmp_path / "snapshot"
    workspace_root.mkdir()
    snapshot_root.mkdir()
    prepared = r11.PreparedCase(
        case=case,
        board=board,
        workspace=r11.PreparedWorkspace(
            source_root=workspace_root,
            workspace_root=workspace_root,
            snapshot_root=snapshot_root,
        ),
        probe_uid="probe-123",
        flash_artifact=tmp_path / "firmware.hex",
        symbol_artifact=tmp_path / "firmware.elf",
    )
    prepared.flash_artifact.write_text("hex", encoding="utf-8")
    prepared.symbol_artifact.write_text("elf", encoding="utf-8")
    prompt_path = workspace_root / ".r11_prompt.txt"
    result_path = workspace_root / ".r11_codex_result.json"
    prompt_path.write_text("prompt", encoding="utf-8")
    result_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(r11, "_ensure_codex_registration", lambda: None)
    monkeypatch.setattr(r11, "_ensure_stage1_preflight", lambda _board_id, _probe_uid: None)
    monkeypatch.setattr(r11, "_prepare_case", lambda _case: prepared)
    monkeypatch.setattr(r11, "_prepare_target_state", lambda _prepared: None)
    monkeypatch.setattr(
        r11,
        "_run_codex",
        lambda _case, _workspace, _prompt, timeout_seconds=r11.DEFAULT_CODEX_TIMEOUT_SECONDS: (
            r11.CodexRunArtifacts(
                exit_code=1,
                stdout_text="",
                stderr_text="failed",
                result_path=result_path,
                prompt_path=prompt_path,
                new_session_dirs=(),
            )
        ),
    )

    report = r11.run_case(case.case_id)

    assert report.final_status == "unresolved"
    assert report.score_report.score == 0
    assert report.run_root is None


def test_run_case_uses_structured_final_session_as_canonical_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = r11.load_case("nrf52833dk__k001_reference_green")
    board = r11._load_board(case.board_id)
    workspace_root = tmp_path / "workspace"
    snapshot_root = tmp_path / "snapshot"
    workspace_root.mkdir()
    snapshot_root.mkdir()
    prepared = r11.PreparedCase(
        case=case,
        board=board,
        workspace=r11.PreparedWorkspace(
            source_root=workspace_root,
            workspace_root=workspace_root,
            snapshot_root=snapshot_root,
        ),
        probe_uid="probe-123",
        flash_artifact=tmp_path / "firmware.hex",
        symbol_artifact=tmp_path / "firmware.elf",
    )
    prepared.flash_artifact.write_text("hex", encoding="utf-8")
    prepared.symbol_artifact.write_text("elf", encoding="utf-8")
    prompt_path = workspace_root / ".r11_prompt.txt"
    result_path = workspace_root / ".r11_codex_result.json"
    prompt_path.write_text("prompt", encoding="utf-8")
    result_payload = {
        "case_id": case.case_id,
        "board_id": case.board_id,
        "session_id": "20260620T000001Z-canonical",
        "final_status": "healthy_confirmed",
        "classification": "healthy",
        "root_cause": "No issue found.",
        "actions_taken": ["connect", "flash_firmware", "read_serial"],
        "mcp_tools_used": ["connect", "flash_firmware", "read_serial"],
        "files_changed": [],
        "recover_used": False,
        "verification": {
            "flash_ok": True,
            "uart_ok": True,
            "symbol_ok": True,
            "green_check_ok": True,
        },
        "summary": "Board remained healthy.",
    }
    result_path.write_text(json.dumps(result_payload), encoding="utf-8")

    supporting_root = tmp_path / "20260620T000000Z-supporting"
    canonical_root = tmp_path / "20260620T000001Z-canonical"
    for run_root, board_id in (
        (supporting_root, "nucleo_l476rg"),
        (canonical_root, case.board_id),
    ):
        (run_root / "run-metadata").mkdir(parents=True)
        (run_root / "run-metadata" / "session.json").write_text(
            json.dumps({"board_id": board_id, "probe_uid": "probe-123"}),
            encoding="utf-8",
        )

    captured: dict[str, object] = {}

    monkeypatch.setattr(r11, "_ensure_codex_registration", lambda: None)
    monkeypatch.setattr(r11, "_ensure_stage1_preflight", lambda _board_id, _probe_uid: None)
    monkeypatch.setattr(r11, "_prepare_case", lambda _case: prepared)
    monkeypatch.setattr(r11, "_prepare_target_state", lambda _prepared: None)
    monkeypatch.setattr(
        r11,
        "_run_codex",
        lambda _case, _workspace, _prompt, timeout_seconds=r11.DEFAULT_CODEX_TIMEOUT_SECONDS: (
            r11.CodexRunArtifacts(
                exit_code=0,
                stdout_text='{"type":"done"}\n',
                stderr_text="",
                result_path=result_path,
                prompt_path=prompt_path,
                new_session_dirs=(supporting_root, canonical_root),
            )
        ),
    )
    monkeypatch.setattr(
        r11,
        "_run_final_verification",
        lambda _prepared: r11.VerificationSummary(
            flash_ok=True,
            uart_ok=True,
            symbol_ok=True,
            green_check_ok=True,
            excerpt="boot ok",
            error_text=None,
        ),
    )
    monkeypatch.setattr(
        r11,
        "_score_case",
        lambda _case, _agent_result, _verification, _changed_files: r11.ScoreReport(
            score=100,
            outcome_label="full_success",
            diagnosis_points=40,
            intervention_points=25,
            verification_points=25,
            safety_points=10,
            penalties=(),
            reasons=(),
            actual_changed_files=(),
            classification_correct=True,
            intervention_correct=True,
        ),
    )
    monkeypatch.setattr(
        r11,
        "_record_case_artifacts",
        lambda _prepared, _result, _codex_run, _verification, _score, run_root, session_selection=None: (
            captured.update(
                {
                    "run_root": run_root,
                    "session_selection": session_selection,
                }
            )
        ),
    )

    report = r11.run_case(case.case_id)

    assert report.final_status == "healthy_confirmed"
    assert report.session_id == canonical_root.name
    assert report.run_root == canonical_root
    selection = captured["session_selection"]
    assert isinstance(selection, r11.SessionSelection)
    assert selection.canonical_run_root == canonical_root
    assert selection.supporting_run_roots == (supporting_root,)
    assert "supporting-session-count:1" in selection.runner_warnings
    assert (
        f"supporting-session-board-mismatch:{supporting_root.name}:nucleo_l476rg"
        in selection.runner_warnings
    )


def test_run_case_fails_when_structured_session_id_is_missing_from_new_session_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = r11.load_case("nrf52833dk__k001_reference_green")
    board = r11._load_board(case.board_id)
    workspace_root = tmp_path / "workspace"
    snapshot_root = tmp_path / "snapshot"
    workspace_root.mkdir()
    snapshot_root.mkdir()
    prepared = r11.PreparedCase(
        case=case,
        board=board,
        workspace=r11.PreparedWorkspace(
            source_root=workspace_root,
            workspace_root=workspace_root,
            snapshot_root=snapshot_root,
        ),
        probe_uid="probe-123",
        flash_artifact=tmp_path / "firmware.hex",
        symbol_artifact=tmp_path / "firmware.elf",
    )
    prepared.flash_artifact.write_text("hex", encoding="utf-8")
    prepared.symbol_artifact.write_text("elf", encoding="utf-8")
    prompt_path = workspace_root / ".r11_prompt.txt"
    result_path = workspace_root / ".r11_codex_result.json"
    prompt_path.write_text("prompt", encoding="utf-8")
    result_payload = {
        "case_id": case.case_id,
        "board_id": case.board_id,
        "session_id": "20260620T000001Z-canonical",
        "final_status": "healthy_confirmed",
        "classification": "healthy",
        "root_cause": "No issue found.",
        "actions_taken": ["connect"],
        "mcp_tools_used": ["connect"],
        "files_changed": [],
        "recover_used": False,
        "verification": {
            "flash_ok": True,
            "uart_ok": True,
            "symbol_ok": True,
            "green_check_ok": True,
        },
        "summary": "Board remained healthy.",
    }
    result_path.write_text(json.dumps(result_payload), encoding="utf-8")
    unrelated_root = tmp_path / "20260620T000000Z-other"
    unrelated_root.mkdir()

    monkeypatch.setattr(r11, "_ensure_codex_registration", lambda: None)
    monkeypatch.setattr(r11, "_ensure_stage1_preflight", lambda _board_id, _probe_uid: None)
    monkeypatch.setattr(r11, "_prepare_case", lambda _case: prepared)
    monkeypatch.setattr(r11, "_prepare_target_state", lambda _prepared: None)
    monkeypatch.setattr(
        r11,
        "_run_codex",
        lambda _case, _workspace, _prompt, timeout_seconds=r11.DEFAULT_CODEX_TIMEOUT_SECONDS: (
            r11.CodexRunArtifacts(
                exit_code=0,
                stdout_text='{"type":"done"}\n',
                stderr_text="",
                result_path=result_path,
                prompt_path=prompt_path,
                new_session_dirs=(unrelated_root,),
            )
        ),
    )

    report = r11.run_case(case.case_id)

    assert report.final_status == "unresolved"
    assert report.score_report.score == 0
    assert report.run_root is None
    assert (
        report.verification.error_text
        == "Structured benchmark result reported session_id=20260620T000001Z-canonical, but that session was not created under runs/ during this case."
    )
