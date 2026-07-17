"""Dynamic MCP discovery plus handler-path tool authorization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from threading import RLock
from typing import Any, ContextManager

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Icon, ToolAnnotations

from pyocd_debug_mcp.kernel.operations import (
    BoardBusyError,
    ManagedOperation,
    OperationTimeoutError,
    dispatch,
    operation_manager,
    operation_timeout_seconds,
    wrap_layer2_response,
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Discovery and authorization metadata for one MCP tool."""

    name: str
    hidden_by_default: bool = False
    locked_by_default: bool = False
    prerequisite: str | None = None


class ToolRegistry:
    """Own tool visibility and per-board physical lock state.

    Discovery is deliberately derived from, but weaker than, authorization. A
    hidden tool is still registered with FastMCP so a stale client can call its
    name and receive the same explicit locked-tool refusal.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._unlocked_boards: dict[str, set[str]] = {}
        self._guard = RLock()
        self._list_revision = 0

    @property
    def list_revision(self) -> int:
        with self._guard:
            return self._list_revision

    def register(
        self,
        name: str,
        *,
        hidden: bool = False,
        locked: bool = False,
        prerequisite: str | None = None,
    ) -> ToolDefinition:
        if not name:
            raise ValueError("tool name must not be empty")
        if locked and not prerequisite:
            prerequisite = f"{name}-plan"

        definition = ToolDefinition(
            name=name,
            hidden_by_default=hidden,
            locked_by_default=locked,
            prerequisite=prerequisite,
        )
        with self._guard:
            existing = self._definitions.get(name)
            if existing is not None:
                if existing != definition:
                    raise ValueError(f"tool '{name}' is already registered with different policy")
                return existing
            self._definitions[name] = definition
            self._unlocked_boards[name] = set()
            if not hidden:
                self._list_revision += 1
        return definition

    def configure(
        self,
        name: str,
        *,
        hidden: bool,
        locked: bool,
        prerequisite: str | None = None,
    ) -> ToolDefinition:
        """Set initial policy for an already registered FastMCP tool."""

        if locked and not prerequisite:
            prerequisite = f"{name}-plan"
        with self._guard:
            current = self._require_definition(name)
            was_advertised = self._is_advertised(current)
            updated = replace(
                current,
                hidden_by_default=hidden,
                locked_by_default=locked,
                prerequisite=prerequisite,
            )
            self._definitions[name] = updated
            if not locked:
                self._unlocked_boards[name].clear()
            is_advertised = self._is_advertised(updated)
            if was_advertised != is_advertised:
                self._list_revision += 1
            return updated

    def unregister(self, name: str) -> None:
        with self._guard:
            definition = self._require_definition(name)
            was_advertised = self._is_advertised(definition)
            del self._definitions[name]
            del self._unlocked_boards[name]
            if was_advertised:
                self._list_revision += 1

    def advertised(self) -> tuple[str, ...]:
        with self._guard:
            return tuple(
                name
                for name, definition in self._definitions.items()
                if self._is_advertised(definition)
            )

    def definition(self, name: str) -> ToolDefinition:
        with self._guard:
            return self._require_definition(name)

    def is_registered(self, name: str) -> bool:
        """Return whether policy metadata exists without raising for an optional alias."""

        with self._guard:
            return name in self._definitions

    def unlock(self, name: str, board_id: str) -> None:
        if not board_id:
            raise ValueError("board_id must not be empty")
        with self._guard:
            definition = self._require_definition(name)
            if not definition.locked_by_default:
                return
            was_advertised = self._is_advertised(definition)
            self._unlocked_boards[name].add(board_id)
            if was_advertised != self._is_advertised(definition):
                self._list_revision += 1

    def relock(self, name: str, board_id: str) -> None:
        with self._guard:
            definition = self._require_definition(name)
            was_advertised = self._is_advertised(definition)
            self._unlocked_boards[name].discard(board_id)
            if was_advertised != self._is_advertised(definition):
                self._list_revision += 1

    def reset(self) -> None:
        """Restore every guarded tool to its default run-start lock state."""

        with self._guard:
            before = self.advertised()
            for boards in self._unlocked_boards.values():
                boards.clear()
            if before != self.advertised():
                self._list_revision += 1

    def is_unlocked(self, name: str, board_id: str | None) -> bool:
        with self._guard:
            definition = self._require_definition(name)
            if not definition.locked_by_default:
                return True
            return board_id is not None and board_id in self._unlocked_boards[name]

    def require_unlocked(self, name: str, board_id: str | None) -> None:
        if self.is_unlocked(name, board_id):
            return
        definition = self.definition(name)
        prerequisite = definition.prerequisite or f"{name}-plan"
        board_text = f" for board '{board_id}'" if board_id else ""
        raise ToolError(
            f"Tool '{name}' is locked{board_text}. Call '{prerequisite}' first."
        )

    def _require_definition(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool: {name}") from exc

    def _is_advertised(self, definition: ToolDefinition) -> bool:
        return not definition.hidden_by_default or bool(self._unlocked_boards[definition.name])


TimeoutResolver = Callable[[str, Mapping[str, object] | None], float]
InvocationGuard = Callable[[str, str, Mapping[str, object]], None]
ExecutionLockResolver = Callable[[str], ContextManager[object]]
OperationResourceBinder = Callable[[ManagedOperation], None]
OperationFinalizerResolver = Callable[[str, str, Mapping[str, object]], Callable[[], None] | None]


@dataclass(frozen=True, slots=True)
class GuardedDispatchPolicy:
    guard: InvocationGuard
    lock_for_board: ExecutionLockResolver


class RegistryFastMCP(FastMCP):
    """FastMCP adapter with dynamic discovery, locks, and bounded dispatch."""

    def __init__(
        self,
        name: str | None = None,
        *,
        registry: ToolRegistry | None = None,
        timeout_resolver: TimeoutResolver = operation_timeout_seconds,
        **settings: Any,
    ) -> None:
        self.registry = registry or ToolRegistry()
        self._timeout_resolver = timeout_resolver
        self._guarded_dispatch: dict[str, GuardedDispatchPolicy] = {}
        self._layer2_tools: set[str] = set()
        self._operation_resource_binder: OperationResourceBinder | None = None
        self._finalizer_resolver: OperationFinalizerResolver | None = None
        super().__init__(name=name, **settings)

    def configure_operation_resources(self, binder: OperationResourceBinder) -> None:
        """Bind persistent board resources into each managed hardware invocation."""

        self._operation_resource_binder = binder

    def configure_finalizers(self, resolver: OperationFinalizerResolver) -> None:
        """Configure strict resolution of structured on-exit actions."""

        self._finalizer_resolver = resolver

    def configure_layer2(self, name: str) -> None:
        """Mark a registered hardware action for common failure wrapping."""

        self.registry.definition(name)
        self._layer2_tools.add(name)

    def configure_guarded_dispatch(
        self,
        name: str,
        *,
        guard: InvocationGuard,
        lock_for_board: ExecutionLockResolver,
    ) -> None:
        """Bind one registered synchronous tool to plan enforcement and its board lock."""

        self.registry.definition(name)
        self._guarded_dispatch[name] = GuardedDispatchPolicy(guard, lock_for_board)

    def add_tool(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        super().add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )
        self.registry.register(name or fn.__name__)

    def remove_tool(self, name: str) -> None:
        super().remove_tool(name)
        self.registry.unregister(name)

    async def list_tools(self):  # type: ignore[no-untyped-def]
        tools = await super().list_tools()
        advertised = frozenset(self.registry.advertised())
        return [tool for tool in tools if tool.name in advertised]

    async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[no-untyped-def]
        board_value = arguments.get("board_id")
        board_id = board_value if isinstance(board_value, str) and board_value else None
        try:
            self.registry.require_unlocked(name, board_id)
        except ToolError as exc:
            if name in self._layer2_tools:
                raise ToolError(wrap_layer2_response(str(exc))) from exc
            raise
        revision_before = self.registry.list_revision
        dispatch_policy = self._guarded_dispatch.get(name)
        before_execution = None
        execution_lock = None
        if dispatch_policy is not None:
            if board_id is None:
                raise ToolError(f"Guarded tool '{name}' requires a non-empty board_id.")
            before_execution = partial(
                dispatch_policy.guard,
                name,
                board_id,
                dict(arguments),
            )
            execution_lock = dispatch_policy.lock_for_board(board_id)
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        context = self.get_context()
        timeout = self._timeout_resolver(name, arguments)
        finalizer = None
        if "on_exit" in arguments:
            if board_id is None or self._finalizer_resolver is None:
                raise ToolError(f"Tool '{name}' cannot accept an on_exit finalizer.")
            try:
                finalizer = self._finalizer_resolver(name, board_id, arguments)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
        try:
            request_id = context.request_id
        except ValueError:
            request_id = None

        try:
            if tool.is_async:

                async def invoke_async():  # type: ignore[no-untyped-def]
                    return await tool.run(arguments, context=context, convert_result=True)

                return await dispatch(
                    name,
                    board_id,
                    invoke_async,
                    timeout,
                    before_execution=before_execution,
                    execution_lock=execution_lock,
                    request_id=request_id,
                    serialize_board=name != "action_batch",
                    resource_binder=self._operation_resource_binder
                    if name in self._layer2_tools
                    else None,
                    finalizer=finalizer,
                )

            def invoke_sync():  # type: ignore[no-untyped-def]
                return anyio.run(
                    partial(tool.run, arguments, context=context, convert_result=True)
                )

            return await dispatch(
                name,
                board_id,
                invoke_sync,
                timeout,
                before_execution=before_execution,
                execution_lock=execution_lock,
                request_id=request_id,
                resource_binder=self._operation_resource_binder
                if name in self._layer2_tools
                else None,
                finalizer=finalizer,
            )
        except (OperationTimeoutError, BoardBusyError) as exc:
            message = str(exc)
            if name in self._layer2_tools:
                message = wrap_layer2_response(message)
            raise ToolError(message) from exc
        except ToolError as exc:
            if name in self._layer2_tools:
                raise ToolError(wrap_layer2_response(str(exc))) from exc
            raise
        except Exception as exc:
            if name in self._layer2_tools:
                raise ToolError(wrap_layer2_response(str(exc))) from exc
            raise
        finally:
            if self.registry.list_revision != revision_before:
                await self._send_tool_list_changed()

    def create_initialization_options(self):  # type: ignore[no-untyped-def]
        """Advertise the tool-list notification capability to MCP clients."""

        return self._mcp_server.create_initialization_options(
            NotificationOptions(tools_changed=True)
        )

    async def _send_tool_list_changed(self) -> None:
        context = self.get_context()
        try:
            session = context.session
        except ValueError:
            return
        await session.send_tool_list_changed()

    async def run_stdio_async(self) -> None:
        """Run stdio while advertising dynamic tool-list notifications."""

        try:
            async with stdio_server() as (read_stream, write_stream):
                await self._mcp_server.run(
                    read_stream,
                    write_stream,
                    self.create_initialization_options(),
                )
        finally:
            operation_manager.cancel_all("stdio client EOF or server shutdown")
