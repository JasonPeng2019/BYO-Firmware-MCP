from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import threading
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import mcp.types as types
import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from pyocd_debug_mcp.turnkey.contracts import (
    MiddlemanDecision,
    TurnkeyContext,
    TurnkeyContractError,
)
from pyocd_debug_mcp.turnkey.controller import CallArtifactCleanupError, TurnkeyController
from pyocd_debug_mcp.turnkey.green_check import GreenCheckError, GreenCheckResult, GreenCheckRunner
from pyocd_debug_mcp.turnkey.provider import ProviderError, ProviderTerminationError
from pyocd_debug_mcp.turnkey.server import create_turnkey_server


def decision(action: str, params: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "action": action,
        "action_params": params or {},
        "observation_summary": "Observed one concrete bounded test action.",
        "problem_hypotheses": [],
        "current_strategy": "Continue only with the current workflow step.",
        "failed_strategies": [],
        "carry_forward_warnings": ["Keep hardware access inside guarded Server B."],
    }


class FakeSession:
    def __init__(self, replies: Iterable[object]) -> None:
        self.replies = iter(replies)
        self.prompts: list[str] = []
        self.closed = False

    def exchange(self, prompt: str, *, timeout_seconds: float) -> object:
        assert timeout_seconds > 0
        self.prompts.append(prompt)
        return next(self.replies)

    def close(self) -> None:
        self.closed = True


class CloseFailingSession(FakeSession):
    def close(self) -> None:
        self.closed = True
        raise OSError("provider close failed")


class TerminationFailingSession(FakeSession):
    def close(self) -> None:
        self.closed = True
        raise ProviderTerminationError("middleman is still alive")


class FakeFactory:
    def __init__(self, *sessions: FakeSession) -> None:
        self.sessions = list(sessions)
        self.opened: list[tuple[Path, str]] = []

    def open(
        self, *, workspace: Path, server_b_url: str, artifact_root: Path
    ) -> FakeSession:
        assert artifact_root.is_dir()
        self.opened.append((workspace, server_b_url))
        return self.sessions.pop(0)


class FailingFactory:
    def open(self, **_kwargs):
        raise OSError("provider workspace is unavailable")


class PassingGreenCheck:
    def run(self, **_kwargs) -> GreenCheckResult:
        return GreenCheckResult(True, ("fake-green",), 0, "GREEN OK", ())


class BlockingSession:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()
        self.closed = threading.Event()

    def exchange(self, prompt: str, *, timeout_seconds: float) -> object:
        del prompt, timeout_seconds
        self.entered.set()
        self.released.wait(10.0)
        raise ProviderError("middleman was cancelled")

    def close(self) -> None:
        self.closed.set()
        self.released.set()


class BlockingFactory:
    def __init__(self, session: BlockingSession) -> None:
        self.session = session

    def open(self, **_kwargs) -> BlockingSession:
        return self.session


def context(tmp_path: Path) -> TurnkeyContext:
    turn = {
        "action": "Inspected one file.",
        "reasoning": "It was the direct implementation owner.",
        "codebase_changes": "none",
        "result": "The expected symbol was present.",
    }
    return TurnkeyContext.parse(
        {
            "tool_summary": "Complete a bounded firmware task and prove it.",
            "task": "Exercise the turnkey controller.",
            "memory_tier1_turn1": turn,
            "memory_tier1_turn2": turn,
            "memory_tier1_turn3": turn,
            "memory_tier1_turn4": turn,
            "memory_tier2": "Twelve earlier turns established the test baseline.",
            "memory_tier3": "The session is ready for one delegated task.",
            "relevant_files": "src/main.c — task owner",
            "board_facts": "board=test mcu=test probe=test",
            "reference_artifacts": "reference.elf",
            "build_context": f"workspace={tmp_path}",
            "iteration_max": 8,
            "green_check_guide": "Run the deterministic fake check.",
            "green_check_script": {
                "filename": "green.py",
                "content": "print('GREEN OK')\n",
                "command": ["{python}", "{script}"],
            },
            "green_check_expected_outputs": ["GREEN OK"],
        }
    )


def test_decision_contract_rejects_extra_or_wrong_action_parameters() -> None:
    with pytest.raises(ValueError, match="unknown"):
        MiddlemanDecision.parse(decision("next_step") | {"extra": True})
    with pytest.raises(ValueError, match="exactly"):
        MiddlemanDecision.parse(decision("finish_task", {}))


def test_controller_reprompts_invalid_json_and_requires_server_green_check(tmp_path: Path) -> None:
    session = FakeSession(
        (
            {"not": "a decision"},
            decision("finish_task", {"task_result": "too early"}),
            decision("next_step"),
            decision("request_green_check"),
            decision(
                "validate_green_check",
                {"script_args": [], "preparation_summary": "Prepared the fake check."},
            ),
            decision("finish_task", {"task_result": "fixed and proven"}),
        )
    )
    controller = TurnkeyController(
        FakeFactory(session),
        green_checks=PassingGreenCheck(),  # type: ignore[arg-type]
    )

    result = controller.run(
        tool_name="complex_task",
        context=context(tmp_path),
        workspace=tmp_path,
        server_b_url="http://127.0.0.1:8765/mcp",
        steps=("Do the bounded task.",),
    )

    assert result.status == "completed"
    assert result.message == "fixed and proven"
    assert result.green_check is not None and result.green_check.passed
    assert session.closed is True
    assert not tuple(tmp_path.glob(".turnkey-call-*"))
    assert "SCHEMA CORRECTION" in session.prompts[1]
    assert "finish_task rejected" in session.prompts[2]


def _full_call(tmp_path: Path) -> dict[str, object]:
    selected = context(tmp_path)
    turns = [
        {
            "action": item.action,
            "reasoning": item.reasoning,
            "codebase_changes": item.codebase_changes,
            "result": item.result,
        }
        for item in selected.tier1
    ]
    return {
        "tool_summary": selected.tool_summary,
        "task": selected.task,
        "bug": "Observed bad output; expected good output; reproduce with the green check.",
        "memory_tier1_turn1": turns[0],
        "memory_tier1_turn2": turns[1],
        "memory_tier1_turn3": turns[2],
        "memory_tier1_turn4": turns[3],
        "memory_tier2": selected.memory_tier2,
        "memory_tier3": selected.memory_tier3,
        "relevant_files": selected.relevant_files,
        "board_facts": selected.board_facts,
        "reference_artifacts": selected.reference_artifacts,
        "build_context": selected.build_context,
        "iteration_max": selected.iteration_max,
        "green_check_guide": selected.green_check_guide,
        "green_check_script": {
            "filename": selected.green_check_script.filename,
            "content": selected.green_check_script.content,
            "command": list(selected.green_check_script.command),
        },
        "green_check_expected_outputs": list(selected.green_check_expected_outputs),
    }


def _payload(result: types.CallToolResult) -> dict[str, object]:
    if result.structuredContent is not None:
        return result.structuredContent
    assert len(result.content) == 1 and isinstance(result.content[0], types.TextContent)
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_mcp_load_tool_unlocks_one_agentic_tool_and_delta_restores_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = FakeSession(
        (
            decision("next_step"),
            decision(
                "finalize_needs_user_permission",
                {"permission_request": "Allow one guarded action."},
            ),
        )
    )
    second = FakeSession((decision("fail_task", {"failure_reason": "continued fake stop"}),))
    monkeypatch.setenv("BYO_WORKSPACE_ROOT", str(tmp_path))
    mcp = create_turnkey_server(FakeFactory(first, second))
    call = _full_call(tmp_path)

    async with create_connected_server_and_client_session(mcp) as client:
        handshake = _payload(await client.call_tool("initialization_handshake", {}))
        assert handshake["server_b_client_endpoint"] == "http://127.0.0.1:8765/mcp"
        listed_tools = (await client.list_tools()).tools
        tools = {item.name for item in listed_tools}
        assert {
            "initialization_handshake",
            "load_bug_fix",
            "load_complex_implementation",
            "load_complex_task",
            "bug_fix",
            "complex_implementation",
            "complex_task",
        } <= tools
        complex_schema = next(item.inputSchema for item in listed_tools if item.name == "complex_task")
        properties = set(complex_schema.get("properties", {}))
        assert "plan" not in properties and "steps" not in properties
        assert complex_schema["patternProperties"] == {
            "^step_[1-9][0-9]*$": {"minLength": 1, "type": "string"}
        }
        assert complex_schema["additionalProperties"] is False
        assert complex_schema["oneOf"][0]["required"][-1] == "step_1"
        assert complex_schema["oneOf"][1]["required"] == [
            "tool_summary",
            "task",
            "continue_instruction",
        ]
        bug_schema = next(item.inputSchema for item in listed_tools if item.name == "bug_fix")
        assert len(bug_schema["oneOf"]) == 2
        assert "bug" in bug_schema["oneOf"][0]["required"]
        assert bug_schema["oneOf"][1]["required"] == [
            "tool_summary",
            "task",
            "continue_instruction",
        ]

        locked = _payload(await client.call_tool("bug_fix", call))
        assert locked["status"] == "agentic_tool_refused"
        assert "load_bug_fix" in str(locked["message"])

        loaded = _payload(await client.call_tool("load_bug_fix", {}))
        assert loaded["status"] == "agentic_tool_unlocked"

        paused = _payload(await client.call_tool("bug_fix", call))
        assert paused["status"] == "needs_user_permission"
        assert paused["step_index"] == 1

        delta = _payload(
            await client.call_tool(
                "bug_fix",
                {
                    "tool_summary": "Continue after permission handling.",
                    "task": call["task"],
                    "continue_instruction": "please continue",
                },
            )
        )
        assert delta["status"] == "failed"
        assert "SUMMARY: Continue after permission handling." in second.prompts[0]
        assert "CURRENT STEP (2/5)" in second.prompts[0]
        assert "please continue" in second.prompts[0]
        assert f"BUG: {call['bug']}" in second.prompts[0]


@pytest.mark.asyncio
async def test_delta_retains_structured_bug_without_parsing_markers_from_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = FakeSession((decision("finalize_needs_user_permission", {"permission_request": "ok"}),))
    second = FakeSession((decision("fail_task", {"failure_reason": "done"}),))
    monkeypatch.setenv("BYO_WORKSPACE_ROOT", str(tmp_path))
    mcp = create_turnkey_server(FakeFactory(first, second))
    call = _full_call(tmp_path)
    call["task"] = "Fix parser\nBUG: preserve this user-authored task text"
    call["bug"] = "actual defect"
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_bug_fix", {})
        await client.call_tool("bug_fix", call)
        await client.call_tool(
            "bug_fix",
            {
                "tool_summary": "Continue.",
                "task": call["task"],
                "continue_instruction": "continue",
            },
        )

    prompt = second.prompts[0]
    assert prompt.count("BUG: preserve this user-authored task text") == 1
    assert prompt.count("BUG: actual defect") == 1


@pytest.mark.asyncio
async def test_complex_task_returns_exact_user_text_and_resumes_from_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay_session = FakeSession(
        (decision("return_text_to_user", {"text": "Live bounded update."}),)
    )
    completion_session = FakeSession(
        (
            decision("next_step"),
            decision("request_green_check"),
            decision(
                "validate_green_check",
                {"script_args": [], "preparation_summary": "prepared"},
            ),
            decision("finish_task", {"task_result": "done"}),
        )
    )
    monkeypatch.setenv("BYO_WORKSPACE_ROOT", str(tmp_path))
    mcp = create_turnkey_server(FakeFactory(relay_session, completion_session))
    call = _full_call(tmp_path)
    call.pop("bug")
    call["step_1"] = "Implement and prove the bounded task."
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_complex_task", {})
        raw_result = await client.call_tool("complex_task", call)
        assert not raw_result.isError, raw_result
        result = _payload(raw_result)
        assert result["status"] == "user_text_required", result
        assert result["user_text"] == "Live bounded update."
        resumed = _payload(
            await client.call_tool(
                "complex_task",
                {
                    "tool_summary": call["tool_summary"],
                    "task": call["task"],
                    "continue_instruction": "The exact text was relayed; continue.",
                },
            )
        )

    assert resumed["status"] == "completed", resumed
    assert "RESUMED STATE" in completion_session.prompts[0]
    assert "Keep hardware access inside guarded Server B." in completion_session.prompts[0]
    assert "return_text_to_user requested this exact relay" in completion_session.prompts[0]


@pytest.mark.asyncio
async def test_delta_retains_explicit_final_step_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused_session = FakeSession(
        (
            decision("next_step"),
            decision("return_text_to_user", {"text": "One final confirmation."}),
        )
    )
    completion_session = FakeSession(
        (
            decision("request_green_check"),
            decision(
                "validate_green_check",
                {"script_args": [], "preparation_summary": "prepared"},
            ),
            decision("finish_task", {"task_result": "done"}),
        )
    )
    monkeypatch.setenv("BYO_WORKSPACE_ROOT", str(tmp_path))
    mcp = create_turnkey_server(FakeFactory(paused_session, completion_session))
    call = _full_call(tmp_path)
    call.pop("bug")
    call["step_1"] = "Complete the only caller-authored step."

    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_complex_task", {})
        paused = _payload(await client.call_tool("complex_task", call))
        assert paused["workflow_complete"] is True
        resumed = _payload(
            await client.call_tool(
                "complex_task",
                {
                    "tool_summary": call["tool_summary"],
                    "task": call["task"],
                    "continue_instruction": "The confirmation was relayed; continue.",
                },
            )
        )

    assert resumed["status"] == "completed"


@pytest.mark.asyncio
async def test_return_text_uses_mcp_elicitation_and_keeps_the_same_provider_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession(
        (
            decision("return_text_to_user", {"text": "Please confirm the attached board."}),
            decision("next_step"),
            decision("request_green_check"),
            decision(
                "validate_green_check",
                {"script_args": [], "preparation_summary": "prepared"},
            ),
            decision("finish_task", {"task_result": "confirmed and complete"}),
        )
    )
    relayed: list[str] = []

    async def elicit(context, params):
        del context
        relayed.append(params.message)
        return types.ElicitResult(action="accept", content={"continue_task": True})

    monkeypatch.setenv("BYO_WORKSPACE_ROOT", str(tmp_path))
    mcp = create_turnkey_server(FakeFactory(session))
    call = _full_call(tmp_path)
    call.pop("bug")
    call["step_1"] = "Complete the bounded task after the user-facing relay."

    async with create_connected_server_and_client_session(
        mcp, elicitation_callback=elicit
    ) as client:
        await client.call_tool("load_complex_task", {})
        result = _payload(await client.call_tool("complex_task", call))

    assert result["status"] == "completed"
    assert relayed == ["Please confirm the attached board."]
    assert len(session.prompts) == 5
    assert "surfaced to Client A" in session.prompts[1]
    assert session.closed is True


def test_controller_rejects_iteration_cap_that_cannot_finish_workflow(tmp_path: Path) -> None:
    selected = context(tmp_path)
    too_short = replace(selected, iteration_max=7)
    factory = FakeFactory(FakeSession(()))
    controller = TurnkeyController(factory)

    with pytest.raises(TurnkeyContractError, match="at least 8 decisions"):
        controller.run(
            tool_name="bug_fix",
            context=too_short,
            workspace=tmp_path,
            server_b_url="http://127.0.0.1:8765/mcp",
        )
    assert factory.opened == []


def test_iteration_limit_returns_the_last_agent_diagnostics(tmp_path: Path) -> None:
    replies = tuple(
        decision("continue_step")
        | {
            "observation_summary": f"bounded observation {index}",
            "failed_strategies": ["discarded approach"],
            "carry_forward_warnings": ["Keep hardware access inside guarded Server B."],
        }
        for index in range(4)
    )
    selected = replace(context(tmp_path), iteration_max=4)

    result = TurnkeyController(FakeFactory(FakeSession(replies))).run(
        tool_name="complex_task",
        context=selected,
        workspace=tmp_path,
        server_b_url="http://127.0.0.1:8765/mcp",
        steps=("Attempt the bounded task.",),
    )
    document = result.to_document()

    assert result.status == "iteration_limit"
    assert "bounded observation 3" in result.message
    assert document["last_result"] == "continue_step accepted: bounded observation 3"
    assert document["failed_strategies"] == ["discarded approach"]
    assert document["carry_forward_warnings"] == [
        "Keep hardware access inside guarded Server B."
    ]


@pytest.mark.asyncio
async def test_load_unlock_is_isolated_between_mcp_clients() -> None:
    mcp = create_turnkey_server(FakeFactory())
    async with create_connected_server_and_client_session(mcp) as first:
        async with create_connected_server_and_client_session(mcp) as second:
            assert _payload(await first.call_tool("load_bug_fix", {}))["status"] == (
                "agentic_tool_unlocked"
            )
            refused = _payload(
                await second.call_tool(
                    "bug_fix",
                    {"tool_summary": "bounded", "task": "same task"},
                )
            )
            assert refused["status"] == "agentic_tool_refused"
            assert "load_bug_fix" in str(refused["message"])


@pytest.mark.asyncio
async def test_operational_errors_return_the_documented_failure_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BYO_WORKSPACE_ROOT", str(tmp_path))
    mcp = create_turnkey_server(FailingFactory())  # type: ignore[arg-type]
    call = _full_call(tmp_path)

    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_bug_fix", {})
        result = _payload(await client.call_tool("bug_fix", call))

    assert result["status"] == "agentic_tool_error"
    assert str(result["message"]).startswith("agentic tool did not finish:")
    assert str(result["message"]).endswith("diagnose the issue and try again.")


@pytest.mark.asyncio
async def test_cancelled_mcp_call_closes_the_live_middleman(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = BlockingSession()
    monkeypatch.setenv("BYO_WORKSPACE_ROOT", str(tmp_path))
    mcp = create_turnkey_server(BlockingFactory(session))  # type: ignore[arg-type]
    call = _full_call(tmp_path)

    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_bug_fix", {})
        request_id = client._request_id  # type: ignore[reportPrivateUsage]
        pending = asyncio.create_task(client.call_tool("bug_fix", call))
        assert await asyncio.to_thread(session.entered.wait, 2.0)
        await client.send_notification(
            types.ClientNotification(
                types.CancelledNotification(
                    params=types.CancelledNotificationParams(
                        requestId=request_id,
                        reason="turnkey cancellation bridge test",
                    )
                )
            )
        )
        with pytest.raises(McpError, match="Request cancelled"):
            await pending
        assert await asyncio.to_thread(session.closed.wait, 2.0)

    for _ in range(200):
        if not tuple(tmp_path.glob(".turnkey-call-*")):
            break
        await asyncio.sleep(0.01)
    assert not tuple(tmp_path.glob(".turnkey-call-*"))


def test_controller_requires_last_step_and_requested_green_check(tmp_path: Path) -> None:
    session = FakeSession(
        (
            decision(
                "validate_green_check",
                {"script_args": [], "preparation_summary": "not requested"},
            ),
            decision("next_step"),
            decision("request_green_check"),
            decision("next_step"),
            decision("request_green_check"),
            decision(
                "validate_green_check",
                {"script_args": [], "preparation_summary": "prepared"},
            ),
            decision("finish_task", {"task_result": "proven"}),
        )
    )
    controller = TurnkeyController(
        FakeFactory(session),
        green_checks=PassingGreenCheck(),  # type: ignore[arg-type]
    )

    result = controller.run(
        tool_name="complex_task",
        context=context(tmp_path),
        workspace=tmp_path,
        server_b_url="http://127.0.0.1:8765/mcp",
        steps=("Implement.", "Prove."),
    )

    assert result.status == "completed"
    assert result.step_index == 1
    assert "validate_green_check rejected" in session.prompts[1]


def test_return_text_pauses_with_exact_user_visible_result(tmp_path: Path) -> None:
    session = FakeSession((decision("return_text_to_user", {"text": "One bounded update."}),))
    result = TurnkeyController(
        FakeFactory(session), green_checks=PassingGreenCheck()  # type: ignore[arg-type]
    ).run(
        tool_name="complex_task",
        context=context(tmp_path),
        workspace=tmp_path,
        server_b_url="http://127.0.0.1:8765/mcp",
        steps=("Complete and prove.",),
    )
    assert result.status == "user_text_required"
    assert result.user_text == "One bounded update."
    assert len(session.prompts) == 1


def test_provider_close_failure_preserves_result_and_removes_call_artifacts(
    tmp_path: Path,
) -> None:
    session = CloseFailingSession(
        (decision("fail_task", {"failure_reason": "bounded provider stop"}),)
    )

    result = TurnkeyController(FakeFactory(session)).run(
        tool_name="complex_task",
        context=context(tmp_path),
        workspace=tmp_path,
        server_b_url="http://127.0.0.1:8765/mcp",
        steps=("Attempt the bounded task.",),
    )

    assert result.status == "failed"
    assert "bounded provider stop" in result.message
    assert session.closed is True
    assert not tuple(tmp_path.glob(".turnkey-call-*"))


def test_live_middleman_after_forced_termination_becomes_a_lifecycle_failure(
    tmp_path: Path,
) -> None:
    session = TerminationFailingSession(
        (decision("fail_task", {"failure_reason": "primary result"}),)
    )

    with pytest.raises(ProviderTerminationError, match="still alive"):
        TurnkeyController(FakeFactory(session)).run(
            tool_name="complex_task",
            context=context(tmp_path),
            workspace=tmp_path,
            server_b_url="http://127.0.0.1:8765/mcp",
            steps=("Attempt the bounded task.",),
        )

    assert session.closed is True
    assert not tuple(tmp_path.glob(".turnkey-call-*"))


def test_call_artifact_cleanup_failure_is_not_silently_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession((decision("fail_task", {"failure_reason": "primary result"}),))

    def fail_cleanup(_path: Path) -> None:
        raise OSError("directory is still in use")

    monkeypatch.setattr("pyocd_debug_mcp.turnkey.controller.shutil.rmtree", fail_cleanup)
    with pytest.raises(CallArtifactCleanupError, match="could not delete call-owned artifacts"):
        TurnkeyController(FakeFactory(session)).run(
            tool_name="complex_task",
            context=context(tmp_path),
            workspace=tmp_path,
            server_b_url="http://127.0.0.1:8765/mcp",
            steps=("Attempt the bounded task.",),
        )


def test_green_check_requires_expected_literal_in_process_output(tmp_path: Path) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "green.py"
    script.write_text("print('NOT GOOD')\n", encoding="utf-8")
    result = GreenCheckRunner().run(
        script_path=script,
        script_args=(),
        expected_outputs=("OK",),
        command_template=("{python}", "{script}"),
        workspace=tmp_path,
        artifact_root=artifact_root,
        timeout_seconds=2.0,
    )
    assert result.passed is False
    assert result.missing_outputs == ("OK",)


def test_green_check_accepts_expected_literal_from_stderr(tmp_path: Path) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "green.py"
    script.write_text("import sys\nprint('PASS ON STDERR', file=sys.stderr)\n", encoding="utf-8")

    result = GreenCheckRunner().run(
        script_path=script,
        script_args=(),
        expected_outputs=("PASS ON STDERR",),
        command_template=("{python}", "{script}"),
        workspace=tmp_path,
        artifact_root=artifact_root,
        timeout_seconds=2.0,
    )

    assert result.passed is True


def test_green_check_rejects_changed_client_a_script(tmp_path: Path) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "green.py"
    original = b"print('FAIL')\n"
    script.write_bytes(original)
    expected_digest = hashlib.sha256(original).hexdigest()
    script.write_text("print('PASS')\n", encoding="utf-8")

    with pytest.raises(GreenCheckError, match="changed after Client A supplied"):
        GreenCheckRunner().run(
            script_path=script,
            script_args=(),
            expected_outputs=("PASS",),
            command_template=("{python}", "{script}"),
            workspace=tmp_path,
            artifact_root=artifact_root,
            timeout_seconds=2.0,
            trusted_script_root=artifact_root,
            expected_script_sha256=expected_digest,
        )


def test_green_check_matches_literal_inside_capture_and_preserves_whitespace(tmp_path: Path) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "green.py"
    script.write_text("print('UART capture: boot ok after reset')\n", encoding="utf-8")
    runner = GreenCheckRunner()
    contained = runner.run(
        script_path=script,
        script_args=(),
        expected_outputs=("boot ok",),
        command_template=("{python}", "{script}"),
        workspace=tmp_path,
        artifact_root=artifact_root,
        timeout_seconds=2.0,
    )
    script.write_text("print('boot ok')\n", encoding="utf-8")
    whitespace_sensitive = runner.run(
        script_path=script,
        script_args=(),
        expected_outputs=(" boot ok ",),
        command_template=("{python}", "{script}"),
        workspace=tmp_path,
        artifact_root=artifact_root,
        timeout_seconds=2.0,
    )
    assert contained.passed is True
    assert whitespace_sensitive.passed is False


def test_controller_materializes_script_only_in_deleted_call_root(tmp_path: Path) -> None:
    session = FakeSession(
        (
            decision("next_step"),
            decision("request_green_check"),
            decision(
                "validate_green_check",
                {"script_args": [], "preparation_summary": "prepared"},
            ),
            decision("finish_task", {"task_result": "done"}),
        )
    )
    result = TurnkeyController(FakeFactory(session)).run(
        tool_name="complex_task",
        context=context(tmp_path),
        workspace=tmp_path,
        server_b_url="http://127.0.0.1:8765/mcp",
        steps=("Prove.",),
    )
    assert result.status == "completed"
    assert not (tmp_path / "green.py").exists()
    assert not tuple(tmp_path.glob(".turnkey-call-*"))


def test_green_check_accepts_exact_value_after_capture_label(tmp_path: Path) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "green.py"
    script.write_text("print('UART capture: boot ok')\n", encoding="utf-8")
    result = GreenCheckRunner().run(
        script_path=script,
        script_args=(),
        expected_outputs=("boot ok",),
        command_template=("{python}", "{script}"),
        workspace=tmp_path,
        artifact_root=artifact_root,
        timeout_seconds=2.0,
    )
    assert result.passed is True


def test_green_check_uses_explicit_interpreter_not_host_suffix_rules(tmp_path: Path) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "green.vendor-script"
    script.write_text("print('PORTABLE OK')\n", encoding="utf-8")
    result = GreenCheckRunner().run(
        script_path=script,
        script_args=(),
        expected_outputs=("PORTABLE OK",),
        command_template=("{python}", "{script}"),
        workspace=tmp_path,
        artifact_root=artifact_root,
        timeout_seconds=2.0,
    )
    assert result.passed is True


def test_green_check_streams_verbose_output_and_returns_only_a_bounded_summary(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "verbose.py"
    script.write_text("print('x' * 100000)\nprint('GREEN OK')\n", encoding="utf-8")

    result = GreenCheckRunner().run(
        script_path=script,
        script_args=(),
        expected_outputs=("GREEN OK",),
        command_template=("{python}", "{script}"),
        workspace=tmp_path,
        artifact_root=artifact_root,
        timeout_seconds=2.0,
    )

    assert result.passed is True
    assert len(result.output.encode("utf-8")) < 40 * 1024
    assert "[bounded output omitted]" in result.output
    assert "bytes=" in result.output and "sha256=" in result.output


def test_green_check_timeout_is_a_contract_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "owned"
    artifact_root.mkdir()
    script = artifact_root / "green.py"
    script.write_text("print('OK')\n", encoding="utf-8")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["green"], 1.0)

    monkeypatch.setattr("pyocd_debug_mcp.turnkey.green_check.run_owned", timeout)
    with pytest.raises(GreenCheckError, match="execution failed"):
        GreenCheckRunner().run(
            script_path=script,
            script_args=(),
            expected_outputs=("OK",),
            command_template=("{python}", "{script}"),
            workspace=tmp_path,
            artifact_root=artifact_root,
            timeout_seconds=1.0,
        )
