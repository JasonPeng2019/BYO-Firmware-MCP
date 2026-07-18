from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent

from pyocd_debug_mcp import server
from pyocd_debug_mcp.kernel.operations import SAFE_EXIT_REMINDER
from pyocd_debug_mcp.kernel.registry import RegistryFastMCP
from pyocd_debug_mcp.tools.batch import MAX_BATCH_CHILDREN, build_batch_handlers


BOARD_ID = "board_a"


def _add_tool(
    mcp: RegistryFastMCP,
    name: str,
    handler: Callable[..., Any],
) -> None:
    mcp.add_tool(handler, name=name, description=handler.__doc__, structured_output=False)


def _install_batch(mcp: RegistryFastMCP) -> None:
    handler = build_batch_handlers(
        mcp.call_tool,
        tool_exists=mcp.registry.is_registered,
    )["action_batch"]
    _add_tool(mcp, "action_batch", handler)
    mcp.configure_layer2("action_batch")


def _child(tool_name: str, **arguments: Any) -> dict[str, object]:
    return {
        "tool_name": tool_name,
        "arguments": {"board_id": BOARD_ID, **arguments},
    }


def _text(result: Any) -> str:
    assert isinstance(result, list) and len(result) == 1
    content = result[0]
    assert isinstance(content, TextContent)
    return content.text


def _payload(result: Any) -> dict[str, Any]:
    text = _text(result)
    body, separator, reminder = text.rpartition("\n")
    assert separator and reminder == SAFE_EXIT_REMINDER
    return cast(dict[str, Any], json.loads(body))


@pytest.mark.asyncio
async def test_ac_16_5_children_execute_in_order_and_match_direct_results() -> None:
    mcp = RegistryFastMCP("batch-order")
    calls: list[tuple[str, int]] = []

    def echo(board_id: str, value: int) -> str:
        """Record and echo one ordered fake hardware call."""

        calls.append((board_id, value))
        return f"echo:{board_id}:{value}"

    _add_tool(mcp, "echo", echo)
    _install_batch(mcp)

    direct = await mcp.call_tool("echo", {"board_id": BOARD_ID, "value": 7})
    result = await mcp.call_tool(
        "action_batch",
        {
            "board_id": BOARD_ID,
            "actions": [_child("echo", value=7), _child("echo", value=8)],
        },
    )
    payload = _payload(result)

    assert payload["status"] == "batch_completed"
    assert [item["tool_name"] for item in payload["completed"]] == ["echo", "echo"]
    direct_document = [item.model_dump(mode="json") for item in direct]
    assert payload["completed"][0]["result"] == direct_document
    assert calls == [(BOARD_ID, 7), (BOARD_ID, 7), (BOARD_ID, 8)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actions, expected",
    [
        (
            [
                _child("record", value=1),
                {
                    "tool_name": "record",
                    "arguments": {"board_id": "board_b", "value": 2},
                },
            ],
            "not shared board",
        ),
        (
            [_child("record", value=1), _child(" Action_Batch ")],
            "nested action_batch",
        ),
    ],
)
async def test_ac_16_1_16_2_complete_precheck_rejects_before_any_child(
    actions: list[dict[str, object]],
    expected: str,
) -> None:
    mcp = RegistryFastMCP("batch-precheck")
    calls: list[int] = []

    def record(board_id: str, value: int) -> str:
        """Record a fake child only if batch preflight completed."""

        del board_id
        calls.append(value)
        return str(value)

    _add_tool(mcp, "record", record)
    _install_batch(mcp)

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool(
            "action_batch",
            {"board_id": BOARD_ID, "actions": actions},
        )

    assert expected in str(caught.value)
    assert calls == []
    assert SAFE_EXIT_REMINDER in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actions, expected",
    [
        ([], "at least one"),
        (
            [_child("record", value=index) for index in range(MAX_BATCH_CHILDREN + 1)],
            f"no more than {MAX_BATCH_CHILDREN}",
        ),
    ],
)
async def test_empty_and_oversized_batches_fail_before_any_child(
    actions: list[dict[str, object]],
    expected: str,
) -> None:
    mcp = RegistryFastMCP("batch-bounds")
    calls: list[int] = []

    def record(board_id: str, value: int) -> str:
        """Remain untouched when the bounded-list precheck fails."""

        del board_id
        calls.append(value)
        return str(value)

    _add_tool(mcp, "record", record)
    _install_batch(mcp)

    with pytest.raises(ToolError, match=expected):
        await mcp.call_tool(
            "action_batch",
            {"board_id": BOARD_ID, "actions": actions},
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "batch_request",
    [
        {"board_id": BOARD_ID, "actions": "not-a-list"},
        {"board_id": BOARD_ID, "actions": [{"tool_name": "record"}]},
        {
            "board_id": BOARD_ID,
            "actions": [
                {
                    "tool_name": "record",
                    "arguments": {"board_id": BOARD_ID, "value": 1},
                    "unexpected": True,
                }
            ],
        },
        {
            "board_id": BOARD_ID,
            "actions": [{"tool_name": 7, "arguments": {"board_id": BOARD_ID}}],
        },
        {
            "board_id": BOARD_ID,
            "actions": [{"tool_name": "missing_tool", "arguments": {"board_id": BOARD_ID}}],
        },
        {
            "board_id": BOARD_ID,
            "actions": [{"tool_name": "record", "arguments": {"value": 1}}],
        },
        {
            "board_id": f" {BOARD_ID} ",
            "actions": [_child("record", value=1)],
        },
    ],
)
async def test_malformed_batches_fail_before_dispatch(
    batch_request: dict[str, object],
) -> None:
    mcp = RegistryFastMCP("batch-malformed")
    calls: list[int] = []

    def record(board_id: str, value: int) -> str:
        """Remain untouched when schema or structural validation fails."""

        del board_id
        calls.append(value)
        return str(value)

    _add_tool(mcp, "record", record)
    _install_batch(mcp)

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("action_batch", batch_request)

    assert calls == []
    assert SAFE_EXIT_REMINDER in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("disguised", ["ACTION_BATCH", " Action_Batch ", "\taction_batch\n"])
async def test_disguised_recursion_is_rejected_before_dispatch(disguised: str) -> None:
    mcp = RegistryFastMCP("batch-disguised-recursion")
    calls: list[str] = []

    def record(board_id: str) -> str:
        """Must not run before disguised recursion is rejected."""

        del board_id
        calls.append("record")
        return "record"

    _add_tool(mcp, "record", record)
    _install_batch(mcp)

    with pytest.raises(ToolError, match="nested action_batch"):
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [_child("record"), _child(disguised)],
            },
        )

    assert calls == []


@pytest.mark.asyncio
async def test_ac_16_3_each_child_consumes_budget_only_at_its_execution_start() -> None:
    mcp = RegistryFastMCP("batch-budget")
    calls: list[int] = []
    state = {"remaining": 2, "guard_calls": 0}
    execution_lock = Lock()

    def guarded(board_id: str, value: int) -> str:
        """Execute one fake plan-guarded operation."""

        del board_id
        calls.append(value)
        return f"ran:{value}"

    def enforce(_name: str, _board: str, _arguments: Mapping[str, object]) -> None:
        state["guard_calls"] += 1
        if state["remaining"] == 0:
            raise ToolError("plan budget exhausted")
        state["remaining"] -= 1

    _add_tool(mcp, "guarded", guarded)
    mcp.registry.configure(
        "guarded", hidden=True, locked=True, prerequisite="guarded-plan"
    )
    mcp.registry.unlock("guarded", BOARD_ID)
    mcp.configure_guarded_dispatch(
        "guarded",
        guard=enforce,
        lock_for_board=lambda _board: execution_lock,
    )
    mcp.configure_layer2("guarded")
    _install_batch(mcp)

    payload = _payload(
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [
                    _child("guarded", value=1),
                    _child("guarded", value=2),
                    _child("guarded", value=3),
                ],
            },
        )
    )

    assert payload["status"] == "batch_failed"
    assert [item["index"] for item in payload["completed"]] == [0, 1]
    assert payload["failure"]["index"] == 2
    assert "plan budget exhausted" in payload["failure"]["message"]
    assert calls == [1, 2]
    assert state == {"remaining": 0, "guard_calls": 3}


@pytest.mark.asyncio
async def test_whole_list_precheck_does_not_burn_first_child_budget() -> None:
    mcp = RegistryFastMCP("batch-no-preburn")
    state = {"remaining": 1, "guard_calls": 0}

    def guarded(board_id: str) -> str:
        """Represent a child whose budget must survive structural preflight."""

        del board_id
        return "ran"

    def consume(_name: str, _board: str, _arguments: Mapping[str, object]) -> None:
        state["guard_calls"] += 1
        state["remaining"] -= 1

    _add_tool(mcp, "guarded", guarded)
    mcp.registry.configure(
        "guarded", hidden=True, locked=True, prerequisite="guarded-plan"
    )
    mcp.registry.unlock("guarded", BOARD_ID)
    mcp.configure_guarded_dispatch(
        "guarded", guard=consume, lock_for_board=lambda _board: Lock()
    )
    _install_batch(mcp)

    with pytest.raises(ToolError, match="not shared board"):
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [
                    _child("guarded"),
                    {
                        "tool_name": "guarded",
                        "arguments": {"board_id": "board_b"},
                    },
                ],
            },
        )

    assert state == {"remaining": 1, "guard_calls": 0}


@pytest.mark.asyncio
async def test_parameter_drift_refuses_at_child_time_without_burning_remaining_budget() -> None:
    mcp = RegistryFastMCP("batch-parameter-drift")
    calls: list[int] = []
    state = {"remaining": 2, "guard_calls": 0}

    def guarded(board_id: str, value: int) -> str:
        """Execute only the exact parameter bound by the fake plan."""

        del board_id
        calls.append(value)
        return str(value)

    def enforce(_name: str, _board: str, arguments: Mapping[str, object]) -> None:
        state["guard_calls"] += 1
        if arguments.get("value") != 1:
            raise ToolError("parameters differ from the immutable plan")
        state["remaining"] -= 1

    _add_tool(mcp, "guarded", guarded)
    mcp.registry.configure(
        "guarded", hidden=True, locked=True, prerequisite="guarded-plan"
    )
    mcp.registry.unlock("guarded", BOARD_ID)
    mcp.configure_guarded_dispatch(
        "guarded", guard=enforce, lock_for_board=lambda _board: Lock()
    )
    _install_batch(mcp)

    payload = _payload(
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [
                    _child("guarded", value=1),
                    _child("guarded", value=2),
                    _child("guarded", value=1),
                ],
            },
        )
    )

    assert [item["index"] for item in payload["completed"]] == [0]
    assert payload["failure"]["index"] == 1
    assert "parameters differ" in payload["failure"]["message"]
    assert calls == [1]
    assert state == {"remaining": 1, "guard_calls": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("state_key", ["gate_open", "fresh"])
async def test_ac_16_4_policy_change_between_children_stops_before_mutation(
    state_key: str,
) -> None:
    mcp = RegistryFastMCP(f"batch-{state_key}")
    calls: list[str] = []
    state = {state_key: True}

    def invalidate(board_id: str) -> str:
        """Close one fake gate or freshness condition between children."""

        del board_id
        calls.append("invalidate")
        state[state_key] = False
        return "invalidated"

    def mutate(board_id: str) -> str:
        """Represent a backend mutation that policy must protect."""

        del board_id
        calls.append("mutate")
        return "mutated"

    def after(board_id: str) -> str:
        """Must never execute after the first failed child."""

        del board_id
        calls.append("after")
        return "after"

    def enforce(_name: str, _board: str, _arguments: Mapping[str, object]) -> None:
        if not state[state_key]:
            raise ToolError(f"{state_key} policy closed")

    _add_tool(mcp, "invalidate", invalidate)
    _add_tool(mcp, "mutate", mutate)
    _add_tool(mcp, "after", after)
    mcp.registry.configure(
        "mutate", hidden=True, locked=True, prerequisite="mutate-plan"
    )
    mcp.registry.unlock("mutate", BOARD_ID)
    mcp.configure_guarded_dispatch(
        "mutate", guard=enforce, lock_for_board=lambda _board: Lock()
    )
    _install_batch(mcp)

    payload = _payload(
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [
                    _child("invalidate"),
                    _child("mutate"),
                    _child("after"),
                ],
            },
        )
    )

    assert payload["status"] == "batch_failed"
    assert [item["tool_name"] for item in payload["completed"]] == ["invalidate"]
    assert payload["failure"]["tool_name"] == "mutate"
    assert f"{state_key} policy closed" in payload["failure"]["message"]
    assert calls == ["invalidate"]


@pytest.mark.asyncio
async def test_first_child_refusal_matches_direct_dispatch_and_stops_batch() -> None:
    mcp = RegistryFastMCP("batch-parity")
    calls: list[str] = []

    def guarded(board_id: str) -> str:
        """Must remain unreachable without the fake active plan."""

        del board_id
        calls.append("guarded")
        return "unexpected"

    def after(board_id: str) -> str:
        """Must not run after the guarded refusal."""

        del board_id
        calls.append("after")
        return "unexpected"

    def refuse(_name: str, _board: str, _arguments: Mapping[str, object]) -> None:
        raise ToolError("active exact plan is required")

    _add_tool(mcp, "guarded", guarded)
    _add_tool(mcp, "after", after)
    mcp.registry.configure(
        "guarded", hidden=True, locked=True, prerequisite="guarded-plan"
    )
    mcp.registry.unlock("guarded", BOARD_ID)
    mcp.configure_guarded_dispatch(
        "guarded", guard=refuse, lock_for_board=lambda _board: Lock()
    )
    mcp.configure_layer2("guarded")
    _install_batch(mcp)

    with pytest.raises(ToolError) as direct:
        await mcp.call_tool("guarded", {"board_id": BOARD_ID})
    payload = _payload(
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [_child("guarded"), _child("after")],
            },
        )
    )

    assert payload["completed"] == []
    assert payload["failure"]["index"] == 0
    assert payload["failure"]["message"] == str(direct.value)
    assert calls == []


@pytest.mark.asyncio
async def test_later_child_handler_failure_stops_all_subsequent_children() -> None:
    mcp = RegistryFastMCP("batch-later-failure")
    calls: list[str] = []

    def succeed(board_id: str) -> str:
        """Complete before the later backend failure."""

        del board_id
        calls.append("succeed")
        return "done"

    def fail(board_id: str) -> str:
        """Represent a backend failure after execution started."""

        del board_id
        calls.append("fail")
        raise RuntimeError("fake backend failed after start")

    def after(board_id: str) -> str:
        """Must never run after the backend failure."""

        del board_id
        calls.append("after")
        return "unexpected"

    _add_tool(mcp, "succeed", succeed)
    _add_tool(mcp, "fail", fail)
    _add_tool(mcp, "after", after)
    _install_batch(mcp)

    payload = _payload(
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [_child("succeed"), _child("fail"), _child("after")],
            },
        )
    )

    assert [item["tool_name"] for item in payload["completed"]] == ["succeed"]
    assert payload["failure"]["tool_name"] == "fail"
    assert "fake backend failed after start" in payload["failure"]["message"]
    assert calls == ["succeed", "fail"]


@pytest.mark.asyncio
async def test_one_time_permission_is_checked_and_consumed_per_child() -> None:
    mcp = RegistryFastMCP("batch-permission")
    calls: list[int] = []
    permission = {"available": True}

    def permitted(board_id: str, value: int) -> str:
        """Execute only while the fake one-time permission remains."""

        del board_id
        calls.append(value)
        return str(value)

    def consume(_name: str, _board: str, _arguments: Mapping[str, object]) -> None:
        if not permission["available"]:
            raise ToolError("fresh one-time permission required")
        permission["available"] = False

    _add_tool(mcp, "permitted", permitted)
    mcp.registry.configure(
        "permitted", hidden=True, locked=True, prerequisite="permitted-plan"
    )
    mcp.registry.unlock("permitted", BOARD_ID)
    mcp.configure_guarded_dispatch(
        "permitted", guard=consume, lock_for_board=lambda _board: Lock()
    )
    _install_batch(mcp)

    payload = _payload(
        await mcp.call_tool(
            "action_batch",
            {
                "board_id": BOARD_ID,
                "actions": [
                    _child("permitted", value=1),
                    _child("permitted", value=2),
                ],
            },
        )
    )

    assert [item["index"] for item in payload["completed"]] == [0]
    assert "fresh one-time permission required" in payload["failure"]["message"]
    assert calls == [1]


@pytest.mark.asyncio
async def test_server_composition_exposes_a_working_batch_without_an_outer_board_lock() -> None:
    result = await server.mcp.call_tool(
        "action_batch",
        {
            "board_id": "composition_board",
            "actions": [
                {
                    "tool_name": "wait",
                    "arguments": {"board_id": "composition_board", "ms": 1},
                },
                {
                    "tool_name": "wait",
                    "arguments": {"board_id": "composition_board", "ms": 1},
                },
            ],
        },
    )
    payload = _payload(result)

    assert payload["status"] == "batch_completed"
    assert [item["tool_name"] for item in payload["completed"]] == ["wait", "wait"]
    assert "action_batch" in server.tool_registry.advertised()
    assert "action_batch" not in server.mcp._guarded_dispatch


@pytest.mark.asyncio
async def test_server_batch_cannot_smuggle_manual_fields_through_visible_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_calls: list[object] = []
    monkeypatch.setattr(
        server,
        "_connect_impl",
        lambda *args, **kwargs: backend_calls.append((args, kwargs)) or "unexpected",
    )

    result = await server.mcp.call_tool(
        "action_batch",
        {
            "board_id": "composition_board",
            "actions": [
                {
                    "tool_name": "connect",
                    "arguments": {
                        "board_id": "composition_board",
                        "unique_id": "wrong-probe",
                        "target": "wrong-target",
                        "board_config": "wrong-config.yaml",
                    },
                }
            ],
        },
    )
    payload = _payload(result)

    assert payload["status"] == "batch_failed"
    assert payload["completed"] == []
    assert payload["failure"]["tool_name"] == "connect"
    assert "unique_id" in payload["failure"]["message"]
    assert backend_calls == []


@pytest.mark.asyncio
async def test_simultaneous_same_board_batches_serialize_each_child_dispatch() -> None:
    mcp = RegistryFastMCP("batch-same-board-concurrency")
    execution_lock = Lock()
    counter_lock = Lock()
    active = 0
    maximum = 0
    calls: list[int] = []

    def measured(board_id: str, value: int) -> str:
        """Measure overlap inside the shared same-board execution lock."""

        nonlocal active, maximum
        assert board_id == BOARD_ID
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.015)
        calls.append(value)
        with counter_lock:
            active -= 1
        return str(value)

    def allow(_name: str, _board: str, _arguments: Mapping[str, object]) -> None:
        return None

    _add_tool(mcp, "measured", measured)
    mcp.registry.configure(
        "measured", hidden=True, locked=True, prerequisite="measured-plan"
    )
    mcp.registry.unlock("measured", BOARD_ID)
    mcp.configure_guarded_dispatch(
        "measured", guard=allow, lock_for_board=lambda _board: execution_lock
    )
    _install_batch(mcp)

    async def run_batch(first: int) -> dict[str, Any]:
        return _payload(
            await mcp.call_tool(
                "action_batch",
                {
                    "board_id": BOARD_ID,
                    "actions": [
                        _child("measured", value=first),
                        _child("measured", value=first + 1),
                    ],
                },
            )
        )

    first, second = await asyncio.gather(run_batch(1), run_batch(3))

    assert first["status"] == second["status"] == "batch_completed"
    assert sorted(calls) == [1, 2, 3, 4]
    assert maximum == 1
