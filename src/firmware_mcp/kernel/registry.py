"""Dynamic MCP discovery plus per-board serialized dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import RLock
from types import TracebackType
from typing import Any, Generic, TypeVar, cast

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import Icon, ToolAnnotations

from firmware_mcp.kernel.operations import (
    ManagedOperation,
    OperationTimeoutError,
    dispatch,
    operation_manager,
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Discovery metadata for one normally visible MCP tool."""

    name: str


class ToolRegistry:
    """Own the registered, normally visible MCP surface."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._guard = RLock()
        self._list_revision = 0

    @property
    def list_revision(self) -> int:
        with self._guard:
            return self._list_revision

    def register(
        self,
        name: str,
    ) -> ToolDefinition:
        if not name:
            raise ValueError("tool name must not be empty")
        definition = ToolDefinition(name=name)
        with self._guard:
            existing = self._definitions.get(name)
            if existing is not None:
                if existing != definition:
                    raise ValueError(f"tool '{name}' is already registered with different metadata")
                return existing
            self._definitions[name] = definition
            self._list_revision += 1
        return definition

    def unregister(self, name: str) -> None:
        with self._guard:
            self._require_definition(name)
            del self._definitions[name]
            self._list_revision += 1

    def advertised(self) -> tuple[str, ...]:
        with self._guard:
            return tuple(name for name, definition in self._definitions.items() if definition.name)

    def definition(self, name: str) -> ToolDefinition:
        with self._guard:
            return self._require_definition(name)

    def is_registered(self, name: str) -> bool:
        """Return whether policy metadata exists without raising for an optional alias."""

        with self._guard:
            return name in self._definitions

    def _require_definition(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool: {name}") from exc


OperationResourceBinder = Callable[[ManagedOperation], None]
StreamItem = TypeVar("StreamItem")


class _EOFObservingReceiveStream(Generic[StreamItem]):
    """Forward one receive stream while making genuine client EOF observable.

    The MCP server is the only consumer: this adapter neither buffers nor
    reads ahead.  It records the first actual closure before passing it on, so
    active synchronous operations can terminate their owned resources before
    the MCP server waits for their handlers to finish.
    """

    def __init__(
        self,
        stream: MemoryObjectReceiveStream[StreamItem],
        *,
        cancel_all: Callable[[str], object] | None = None,
    ) -> None:
        self._stream = stream
        # Resolve the process-local manager at stream construction.  This
        # keeps the production path direct while allowing a focused transport
        # test to observe the one synchronous EOF notification.
        self._cancel_all = operation_manager.cancel_all if cancel_all is None else cancel_all
        self._eof_observed = False

    def _observe_eof(self) -> None:
        if not self._eof_observed:
            self._eof_observed = True
            self._cancel_all("stdio client EOF")

    async def receive(self) -> StreamItem:
        try:
            return cast(StreamItem, await self._stream.receive())
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            self._observe_eof()
            raise

    def __aiter__(self) -> "_EOFObservingReceiveStream[StreamItem]":
        return self

    async def __anext__(self) -> StreamItem:
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> "_EOFObservingReceiveStream[StreamItem]":
        await self._stream.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._stream.__aexit__(exc_type, exc_value, traceback)


class RegistryFastMCP(FastMCP):
    """FastMCP adapter with dynamic discovery and cancellation-aware dispatch."""

    def __init__(
        self,
        name: str | None = None,
        *,
        registry: ToolRegistry | None = None,
        **settings: Any,
    ) -> None:
        self.registry = registry or ToolRegistry()
        self._layer2_tools: set[str] = set()
        self._operation_resource_binder: OperationResourceBinder | None = None
        super().__init__(name=name, **settings)

    def configure_operation_resources(self, binder: OperationResourceBinder) -> None:
        """Bind persistent board resources into each managed hardware invocation."""

        self._operation_resource_binder = binder

    def configure_layer2(self, name: str) -> None:
        """Mark a hardware action that owns persistent board resources."""

        self.registry.definition(name)
        self._layer2_tools.add(name)

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
        revision_before = self.registry.list_revision
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        context = self.get_context()
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
                    request_id=request_id,
                    resource_binder=self._operation_resource_binder
                    if name in self._layer2_tools
                    else None,
                )

            def invoke_sync():  # type: ignore[no-untyped-def]
                return anyio.run(partial(tool.run, arguments, context=context, convert_result=True))

            return await dispatch(
                name,
                board_id,
                invoke_sync,
                request_id=request_id,
                resource_binder=self._operation_resource_binder
                if name in self._layer2_tools
                else None,
            )
        except OperationTimeoutError as exc:
            raise ToolError(str(exc)) from exc
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
                    cast(
                        MemoryObjectReceiveStream[SessionMessage | Exception],
                        _EOFObservingReceiveStream(read_stream),
                    ),
                    write_stream,
                    self.create_initialization_options(),
                )
        finally:
            operation_manager.cancel_all("stdio client EOF or server shutdown")
