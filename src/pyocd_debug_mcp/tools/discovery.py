"""The two discovery-hook MCP handlers: get the contract, refresh the hooks.

Neither tool is a hardware action. Neither opens a probe or a port, consumes a plan or
a permission, or stamps a gate. They are registered visible and unlocked -- an agent
that has just been told native discovery found nothing must be able to reach them
without first unlocking anything, or the fallback is unreachable rather than merely
inconvenient.

`get_discovery_hook_contract` executes nothing at all. `refresh_discovery_hooks` takes
no path, no argv, and no code: it loads the manifest the *server* designates, hashes it
and every hook file, and runs each eligible hook once.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from pyocd_debug_mcp import discovery_hooks
from pyocd_debug_mcp.discovery_failures import (
    CONTRACT_TOOL,
    REFRESH_TOOL,
    contract_call,
    refresh_call,
)
from pyocd_debug_mcp.discovery_hooks import (
    DISCOVERY_HOOK_REGISTRY_ENV,
    DiscoveryHookError,
    DiscoveryHookSnapshot,
    HookExecution,
)

MAX_RETRY_CONTEXTS = 32
RETRY_TTL_SECONDS = 900.0
RETRY_ID_BYTES = 16


def _json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------------------
# Retry contexts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryContext:
    """The exact call to replay once a hook has been written and refreshed.

    Run-scoped and memory-only. Deliberately *not* a field on `ServerRun`: a retry
    ticket is not authority, `clear_authority()` would wipe it, and storing it beside
    real gate/plan/permission state would signal that it is something it is not.
    """

    retry_id: str
    run_id: str
    kind: str
    created_at: float
    retry_tool: str | None = None
    retry_arguments: Mapping[str, Any] = ()  # type: ignore[assignment]
    board_id: str | None = None

    def retry_call(self) -> dict[str, object] | None:
        if self.retry_tool is None:
            return None
        return {"tool": self.retry_tool, "arguments": dict(self.retry_arguments or {})}


class WrongKindRetry(RuntimeError):
    """A retry ticket issued for one kind was presented for another."""


class ExpiredRetry(RuntimeError):
    """A retry ticket is unknown or older than its TTL."""


class DiscoveryRetryStore:
    """Bounded, run-scoped store of retry tickets.

    Eviction is oldest-first on insert, and the TTL is checked on read, so a long-lived
    server cannot accumulate tickets and a stale ticket cannot be replayed.
    """

    __slots__ = ("_guard", "_contexts", "_clock", "_run_id", "_token_factory")

    def __init__(
        self,
        run_id: str,
        *,
        clock: Callable[[], float] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._guard = threading.RLock()
        self._contexts: "OrderedDict[str, RetryContext]" = OrderedDict()
        self._clock = clock or time.monotonic
        self._run_id = run_id
        if token_factory is not None:
            self._token_factory = token_factory
        else:
            import secrets

            self._token_factory = lambda: secrets.token_urlsafe(RETRY_ID_BYTES)

    def issue(
        self,
        kind: str,
        *,
        retry_tool: str | None = None,
        retry_arguments: Mapping[str, Any] | None = None,
        board_id: str | None = None,
    ) -> RetryContext:
        context = RetryContext(
            retry_id=self._token_factory(),
            run_id=self._run_id,
            kind=kind,
            created_at=self._clock(),
            retry_tool=retry_tool,
            retry_arguments=dict(retry_arguments or {}),
            board_id=board_id,
        )
        with self._guard:
            self._contexts[context.retry_id] = context
            while len(self._contexts) > MAX_RETRY_CONTEXTS:
                self._contexts.popitem(last=False)
        return context

    def claim(self, retry_id: str, *, kind: str | None = None) -> RetryContext:
        """Return a live ticket, or refuse. Refusal happens before anything runs."""

        with self._guard:
            context = self._contexts.get(retry_id)
            if context is None:
                raise ExpiredRetry(
                    "that retry ticket is unknown or has expired; call "
                    f"{CONTRACT_TOOL} again to get a fresh one"
                )
            if self._clock() - context.created_at > RETRY_TTL_SECONDS:
                self._contexts.pop(retry_id, None)
                raise ExpiredRetry(
                    "that retry ticket has expired; call "
                    f"{CONTRACT_TOOL} again to get a fresh one"
                )
            if kind is not None and context.kind != kind:
                raise WrongKindRetry(
                    f"that retry ticket was issued for a '{context.kind}' hook contract, "
                    f"not '{kind}'; call {CONTRACT_TOOL} for the kind you mean"
                )
            return context

    def consume(self, retry_id: str) -> None:
        """Clear a ticket after a successful replay so it cannot be reused."""

        with self._guard:
            self._contexts.pop(retry_id, None)

    def known(self, retry_id: str) -> bool:
        with self._guard:
            return retry_id in self._contexts

    def count(self) -> int:
        with self._guard:
            return len(self._contexts)

    def retry_ids(self) -> tuple[str, ...]:
        with self._guard:
            return tuple(self._contexts)


# --------------------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------------------


@dataclass
class DiscoveryToolServices:
    """Everything the two handlers need, injected so they stay testable."""

    hook_root: Callable[[], Any]
    load_snapshot: Callable[[], DiscoveryHookSnapshot]
    current_snapshot: Callable[[], DiscoveryHookSnapshot]
    replace_snapshot: Callable[[DiscoveryHookSnapshot], DiscoveryHookSnapshot]
    retry_store: DiscoveryRetryStore
    registered_providers: Callable[[], Sequence[str]]
    run_hooks: Callable[[DiscoveryHookSnapshot, str], Sequence[HookExecution]]
    on_refresh: Callable[[DiscoveryHookSnapshot], None] | None = None


def build_discovery_handlers(
    services: DiscoveryToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the always-visible, non-authorizing discovery-hook tool surface."""

    def get_discovery_hook_contract(kind: str, retry_id: str | None = None) -> str:
        """Return everything needed to write a local hardware-discovery hook, and nothing else.

        Call this only after native discovery reported no debug probe or no serial port and
        the locked-environment check also came back empty, while the user can still see the
        device in a vendor tool. It executes nothing: it returns the server's own hook
        directory, the manifest and output schemas the server will validate against, the
        runners and platform this server supports, and the exact refresh call to make after
        writing the hook. Write the hook and manifest yourself, then call
        refresh_discovery_hooks. Hook output is configuration only; it can never grant
        hardware authority, restore a gate, plan, or permission, or select a target.
        Relay only agent_prompt guidance, never this JSON or internal paths.
        """

        normalized = str(kind or "").strip().casefold()
        if normalized not in discovery_hooks.SUPPORTED_KINDS:
            return _json(
                {
                    "status": "discovery_contract_rejected",
                    "code": "discovery/unsupported-kind",
                    "agent_prompt": (
                        "kind must be exactly 'probe' (a debug probe) or 'uart' (a serial "
                        f"port). Received: {kind!r}."
                    ),
                    "supported_kinds": sorted(discovery_hooks.SUPPORTED_KINDS),
                }
            )

        ticket: RetryContext | None = None
        if retry_id is not None:
            # A wrong-kind or expired ticket is refused here, before anything runs.
            try:
                ticket = services.retry_store.claim(retry_id, kind=normalized)
            except (WrongKindRetry, ExpiredRetry) as exc:
                return _json(
                    {
                        "status": "discovery_contract_rejected",
                        "code": "discovery/retry-ticket-invalid",
                        "agent_prompt": str(exc),
                        "executable": False,
                    }
                )

        platform = discovery_hooks.current_platform()
        document: dict[str, Any] = {
            "status": "discovery_hook_contract",
            "kind": normalized,
            "agent_prompt": _contract_prompt(normalized, platform),
            "hook_root": str(services.hook_root()),
            "manifest_filename": discovery_hooks.MANIFEST_FILENAME,
            "operating_system": platform,
            "supported_runners": sorted(discovery_hooks.SUPPORTED_RUNNERS),
            "supported_platforms": sorted(discovery_hooks.SUPPORTED_PLATFORMS),
            "operator_registry_environment_variable": DISCOVERY_HOOK_REGISTRY_ENV,
            "manifest_schema": discovery_hooks.MANIFEST_SCHEMA_EXAMPLE,
            "output_schema": (
                discovery_hooks.PROBE_OUTPUT_SCHEMA_EXAMPLE
                if normalized == "probe"
                else discovery_hooks.UART_OUTPUT_SCHEMA_EXAMPLE
            ),
            "example": _example_for(normalized),
            "limits": {
                "max_timeout_seconds": discovery_hooks.MAX_HOOK_TIMEOUT_SECONDS,
                "default_timeout_seconds": discovery_hooks.DEFAULT_HOOK_TIMEOUT_SECONDS,
                "max_stdout_bytes": discovery_hooks.MAX_HOOK_STDOUT_BYTES,
                "max_rows": discovery_hooks.MAX_HOOK_ROWS,
                "max_field_characters": discovery_hooks.MAX_FIELD_CHARS,
                "max_hooks": discovery_hooks.MAX_HOOKS_PER_MANIFEST,
            },
            "platform_guidance": discovery_hooks.PLATFORM_GUIDANCE[platform],
            "constraints": list(_CONTRACT_CONSTRAINTS),
        }
        if normalized == "probe":
            # PROBE_CLASSES is the registered-provider source of truth; probe_families.json
            # is friendly labels plus legacy CLI text matching and must not gate support.
            document["pyocd_providers"] = sorted(services.registered_providers())

        if ticket is not None:
            document["executable"] = True
            document["retry_id"] = ticket.retry_id
            document["refresh_call"] = refresh_call(ticket.retry_id)
            original = ticket.retry_call()
            if original is not None:
                document["original_call"] = original
            if ticket.board_id is not None:
                document["board_id"] = ticket.board_id
        else:
            # Inspection only: there is no failure to retry, so offer no refresh call.
            document["executable"] = False

        return _json(document)

    def refresh_discovery_hooks(retry_id: str | None = None) -> str:
        """Reload the server-designated hook manifest and run each eligible hook once.

        Call this after writing or repairing a hook and its manifest under the hook_root
        that get_discovery_hook_contract returned. It takes no path, no arguments, and no
        code: the server chooses what to load. It opens no probe and no serial port,
        consumes no plan or permission, and grants nothing. It returns per-hook status plus
        friendly rows, then the original call to retry. If a hook fails, repair it and call
        this again. Relay only agent_prompt guidance and friendly names, never this JSON.
        """

        ticket: RetryContext | None = None
        if retry_id is not None:
            try:
                ticket = services.retry_store.claim(retry_id)
            except (WrongKindRetry, ExpiredRetry) as exc:
                return _json(
                    {
                        "status": "discovery_refresh_rejected",
                        "code": "discovery/retry-ticket-invalid",
                        "agent_prompt": str(exc),
                    }
                )

        try:
            snapshot = services.load_snapshot()
        except DiscoveryHookError as exc:
            # The manifest is the agent's own file, so this is repairable in place; the
            # previously admitted snapshot is deliberately left untouched.
            return _json(
                {
                    "status": "discovery_refresh_rejected",
                    "code": "discovery/manifest-invalid",
                    "agent_prompt": (
                        f"The hook manifest could not be loaded: {exc} Fix the manifest at "
                        "the hook_root that get_discovery_hook_contract returned, then call "
                        f"{REFRESH_TOOL} again. The previously loaded hooks, if any, are "
                        "unchanged."
                    ),
                    "hook_root": str(services.hook_root()),
                    "manifest_schema": discovery_hooks.MANIFEST_SCHEMA_EXAMPLE,
                }
            )

        services.replace_snapshot(snapshot)
        if services.on_refresh is not None:
            services.on_refresh(snapshot)

        if not snapshot.hooks:
            return _json(
                {
                    "status": "discovery_hooks_absent",
                    "agent_prompt": (
                        "No hooks are declared at the server's hook directory, so nothing "
                        "was run. Write the manifest and hook file the contract described, "
                        f"then call {REFRESH_TOOL} again."
                    ),
                    "hook_root": str(services.hook_root()),
                    "manifest_filename": discovery_hooks.MANIFEST_FILENAME,
                    "hooks": [],
                }
            )

        executions: list[HookExecution] = []
        for hook_kind in sorted(discovery_hooks.SUPPORTED_KINDS):
            if snapshot.has_hooks_for(hook_kind):
                executions.extend(services.run_hooks(snapshot, hook_kind))

        probe_rows, uart_rows = _friendly_rows(executions)
        succeeded = [execution for execution in executions if execution.ok]
        failed = [execution for execution in executions if not execution.ok]

        document: dict[str, Any] = {
            "status": (
                "discovery_hooks_refreshed" if not failed else "discovery_hooks_partial"
            ),
            "hook_root": str(services.hook_root()),
            "manifest_sha256": snapshot.manifest_sha256,
            "loaded_at": snapshot.loaded_at,
            "operating_system": discovery_hooks.current_platform(),
            "eligible_counts": snapshot.eligible_counts(),
            "hooks": [execution.diagnostic_row() for execution in executions],
            "discovered_probes": probe_rows,
            "discovered_serial_ports": uart_rows,
            "agent_prompt": _refresh_prompt(succeeded, failed, probe_rows, uart_rows),
        }

        if ticket is not None:
            original = ticket.retry_call()
            if original is not None:
                document["retry_call"] = original
            if ticket.board_id is not None:
                document["board_id"] = ticket.board_id
            if not failed:
                # Clear on successful replay so the same ticket cannot be reused.
                services.retry_store.consume(ticket.retry_id)
            else:
                document["retry_id"] = ticket.retry_id
                document["refresh_call"] = refresh_call(ticket.retry_id)
        elif failed:
            document["contract_call"] = contract_call(failed[0].kind)

        return _json(document)

    return {
        CONTRACT_TOOL: get_discovery_hook_contract,
        REFRESH_TOOL: refresh_discovery_hooks,
    }


# --------------------------------------------------------------------------------------
# Prompt and payload helpers
# --------------------------------------------------------------------------------------

_CONTRACT_CONSTRAINTS = (
    "The hook is your code, not the server's. The server never writes, edits, or "
    "generates hook files.",
    "Print exactly one JSON document matching output_schema to stdout, and nothing else.",
    "stdin is closed. Anything that waits for input receives immediate EOF.",
    "The hook runs under a hard per-hook deadline with its whole process group owned by "
    "the server; exceeding it terminates the group.",
    "stdout is capped. Output beyond the cap is discarded and the result is rejected as "
    "invalid rather than truncated silently.",
    "Hook output is configuration only. It cannot select a target, set a connection "
    "policy, restore a gate, plan, or permission, or authorize a hardware action.",
    "A hook cannot make an unsupported pyOCD provider openable. If the provider is not "
    "registered, no hook will help.",
    "The hook directory is inside the project's gitignored `.firm/`, so hooks are "
    "untracked by default. Tell the user if they want it committed.",
)


def _contract_prompt(kind: str, platform: str) -> str:
    subject = "debug probe" if kind == "probe" else "serial port"
    return (
        f"Write a local discovery hook that names the {subject} this machine cannot "
        "enumerate natively. Create the hook file and the manifest yourself under "
        "hook_root, following manifest_schema and output_schema exactly, then call "
        f"{REFRESH_TOOL} with the retry_id from this response. Ask the user only for "
        "facts you cannot determine yourself: which vendor tool can see the device, and "
        "where it is installed. Tell the user in ordinary language that you are adding a "
        "local helper so the server can see their hardware; do not show them this JSON, "
        f"the schemas, or internal paths. Platform guidance for {platform} is included."
    )


def _example_for(kind: str) -> dict[str, Any]:
    entry = next(
        item
        for item in discovery_hooks.MANIFEST_SCHEMA_EXAMPLE["hooks"]  # type: ignore[index]
        if item["kind"] == kind  # type: ignore[index]
    )
    source = (
        discovery_hooks.EXAMPLE_PROBE_HOOK_SOURCE
        if kind == "probe"
        else discovery_hooks.EXAMPLE_UART_HOOK_SOURCE
    )
    return {
        "manifest": {
            "schema_version": discovery_hooks.HOOK_SCHEMA_VERSION,
            "hooks": [entry],
        },
        "manifest_filename": discovery_hooks.MANIFEST_FILENAME,
        "hook_filename": entry["entrypoint"],  # type: ignore[index]
        "hook_source": source,
    }


def _friendly_rows(
    executions: Sequence[HookExecution],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Friendly, relayable rows. No internal identifiers beyond what the user must pick."""

    probe_rows: list[dict[str, object]] = []
    uart_rows: list[dict[str, object]] = []
    for execution in executions:
        if execution.output is None:
            continue
        for probe in execution.output.probes:
            suffix = probe.unique_id[-6:] if probe.unique_id else "unknown"
            probe_rows.append(
                {
                    "friendly_name": f"{probe.description} (identifier ending {suffix})",
                    "provider": probe.provider,
                    "found_by": execution.hook_id,
                }
            )
        for uart in execution.output.uarts:
            uart_rows.append(
                {
                    "friendly_name": f"{uart.description} ({uart.port_path})",
                    "found_by": execution.hook_id,
                    "stable_identity": uart.has_stable_identity,
                }
            )
    return probe_rows, uart_rows


def _refresh_prompt(
    succeeded: Sequence[HookExecution],
    failed: Sequence[HookExecution],
    probe_rows: Sequence[Mapping[str, object]],
    uart_rows: Sequence[Mapping[str, object]],
) -> str:
    if failed:
        first = failed[0]
        return (
            f"{len(failed)} of {len(failed) + len(succeeded)} hooks did not produce usable "
            f"output. The first failure was '{first.hook_id}' ({first.outcome}): "
            f"{first.failure_detail} Repair that hook and call {REFRESH_TOOL} again with "
            "the same retry_id. Tell the user plainly that the local helper is not working "
            "yet; do not show them this JSON."
        )
    if not probe_rows and not uart_rows:
        return (
            "Every hook ran successfully but none reported any hardware. Ask the user to "
            "confirm the device is attached and visible in their vendor tool, then either "
            f"correct the hook and call {REFRESH_TOOL} again, or tell them the device "
            "cannot be found."
        )
    found = []
    if probe_rows:
        found.append(f"{len(probe_rows)} debug probe(s)")
    if uart_rows:
        found.append(f"{len(uart_rows)} serial port(s)")
    return (
        f"Hook discovery succeeded and found {' and '.join(found)}. Retry the original "
        "call in retry_call. If the user must choose between several, present only the "
        "friendly_name values and never the identifiers."
    )
