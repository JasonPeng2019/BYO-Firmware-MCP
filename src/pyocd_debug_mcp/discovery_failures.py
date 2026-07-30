"""The typed discovery/open failure code family and the remedies each one must carry.

One module so `preflight.py`, `server.py`, and `tools/discovery.py` cannot drift into
three different vocabularies for the same condition.

Two rules here are structural, not documentary:

* A backend-open failure (`probe/open-failed`, `uart/open-failed`) must never carry a
  `hook_contract_call`. Discovery already succeeded -- the device was found and named.
  Looping the agent back to discovery would send it to rewrite a hook that is working.
  `open_failure_payload` cannot emit that field, and a test asserts it.
* Nothing in this module stamps a gate or grants authority. These are diagnostics plus
  a next call, and the next call is only ever a contract or refresh tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

DISCOVERY_NO_NATIVE_PROBE = "discovery/no-native-probe"
DISCOVERY_NO_NATIVE_UART = "discovery/no-native-uart"
DISCOVERY_HOOK_FAILED = "discovery/hook-failed"
DISCOVERY_HOOK_TIMEOUT = "discovery/hook-timeout"
DISCOVERY_HOOK_OUTPUT_INVALID = "discovery/hook-output-invalid"
DISCOVERY_HOOK_SOURCE_CHANGED = "discovery/hook-source-changed"
DISCOVERY_UNSUPPORTED_PROVIDER = "discovery/unsupported-provider"
DISCOVERY_SELECTION_DISAPPEARED = "discovery/selection-disappeared"
PROBE_OPEN_FAILED = "probe/open-failed"
UART_OPEN_FAILED = "uart/open-failed"

DISCOVERY_CODES = frozenset(
    {
        DISCOVERY_NO_NATIVE_PROBE,
        DISCOVERY_NO_NATIVE_UART,
        DISCOVERY_HOOK_FAILED,
        DISCOVERY_HOOK_TIMEOUT,
        DISCOVERY_HOOK_OUTPUT_INVALID,
        DISCOVERY_HOOK_SOURCE_CHANGED,
        DISCOVERY_UNSUPPORTED_PROVIDER,
        DISCOVERY_SELECTION_DISAPPEARED,
    }
)
OPEN_FAILURE_CODES = frozenset({PROBE_OPEN_FAILED, UART_OPEN_FAILED})
ALL_FAILURE_CODES = DISCOVERY_CODES | OPEN_FAILURE_CODES

CONTRACT_TOOL = "get_discovery_hook_contract"
REFRESH_TOOL = "refresh_discovery_hooks"

# The locked-environment check comes first, because the overwhelmingly likelier cause of
# an empty inventory is that the server's own environment cannot see the USB device --
# not that a hook is needed. Writing a hook to work around a driver problem is wasted
# effort, so the diagnostic is offered before the contract.
LOCKED_ENVIRONMENT_PROBE_DIAGNOSTIC = (
    "From the BYO Server checkout run `uv run --locked python -m pyocd list --probes`; "
    "this checks whether the server's locked Python environment can enumerate the USB "
    "debugger at all."
)
LOCKED_ENVIRONMENT_UART_DIAGNOSTIC = (
    "From the BYO Server checkout run "
    "`uv run --locked python -m serial.tools.list_ports -v`; this checks whether the "
    "server's locked Python environment can enumerate serial ports at all."
)

PROBE_OPEN_FAILED_CHECKS = (
    "the debugger's driver is installed and the device is not in a fault state",
    "no other process holds the probe open (a vendor IDE, a GDB server, another session)",
    "the probe's firmware is current enough for the installed pyOCD",
    "the target board has power and the debug connector is fully seated",
)
UART_OPEN_FAILED_CHECKS = (
    "no other process holds the serial port open",
    "the port path still exists and the user has permission to open it",
    "the configured baud rate matches the firmware",
)

FailureKind = Literal["probe", "uart"]


def contract_call(kind: FailureKind, *, retry_id: str | None = None) -> dict[str, object]:
    """The exact call an agent should make to get the hook contract for one kind."""

    arguments: dict[str, object] = {"kind": kind}
    if retry_id is not None:
        arguments["retry_id"] = retry_id
    return {"tool": CONTRACT_TOOL, "arguments": arguments}


def refresh_call(retry_id: str | None = None) -> dict[str, object]:
    """The exact call that reloads the manifest and re-runs eligible hooks."""

    return {"tool": REFRESH_TOOL, "arguments": {"retry_id": retry_id}}


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    """A typed discovery failure and the remedy its payload must carry."""

    code: str
    message: str
    kind: FailureKind
    hook_contract_call: Mapping[str, object] | None = None
    refresh_call: Mapping[str, object] | None = None
    hook_diagnostics: tuple[Mapping[str, object], ...] = ()
    native_diagnostics: Mapping[str, object] | None = None
    remedies: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "agent_prompt": self.message,
            "kind": self.kind,
        }
        if self.hook_contract_call is not None:
            payload["hook_contract_call"] = dict(self.hook_contract_call)
        if self.refresh_call is not None:
            payload["refresh_call"] = dict(self.refresh_call)
        if self.hook_diagnostics:
            payload["hook_diagnostics"] = [dict(row) for row in self.hook_diagnostics]
        if self.native_diagnostics is not None:
            payload["native_diagnostics"] = dict(self.native_diagnostics)
        if self.remedies:
            payload["remedies"] = list(self.remedies)
        return payload


def no_native_probe_failure(
    *,
    hooks_available: bool,
    native_diagnostics: Mapping[str, object] | None = None,
    hook_diagnostics: Sequence[Mapping[str, object]] = (),
    retry_id: str | None = None,
) -> DiscoveryFailure:
    """Native probe discovery came back empty."""

    message = (
        "No debug probe is visible to the server. Tell the user to attach the intended "
        "board's debugger and retry; this is not a board-naming ambiguity. "
        + LOCKED_ENVIRONMENT_PROBE_DIAGNOSTIC
        + " If that command also reports nothing and the user can still see the debugger "
        "in a vendor tool, call "
        f"{CONTRACT_TOOL}(kind=\"probe\") to get the contract for a discovery hook that "
        "names it. Do not expose this payload or internal IDs."
    )
    return DiscoveryFailure(
        code=DISCOVERY_NO_NATIVE_PROBE,
        message=message,
        kind="probe",
        hook_contract_call=contract_call("probe", retry_id=retry_id) if hooks_available else None,
        hook_diagnostics=tuple(hook_diagnostics),
        native_diagnostics=native_diagnostics,
        remedies=(LOCKED_ENVIRONMENT_PROBE_DIAGNOSTIC,),
    )


def no_native_uart_failure(
    *,
    hooks_available: bool,
    hook_diagnostics: Sequence[Mapping[str, object]] = (),
    retry_id: str | None = None,
) -> DiscoveryFailure:
    """Native UART discovery came back empty *and* this board's workflow needs UART."""

    message = (
        "This board's workflow uses UART, but no serial port is visible to the server. "
        "Tell the user to attach and identify the board's UART connection. "
        + LOCKED_ENVIRONMENT_UART_DIAGNOSTIC
        + " If that command also reports nothing, call "
        f"{CONTRACT_TOOL}(kind=\"uart\") to get the contract for a discovery hook that "
        "names the port. Do not expose this payload or internal IDs."
    )
    return DiscoveryFailure(
        code=DISCOVERY_NO_NATIVE_UART,
        message=message,
        kind="uart",
        hook_contract_call=contract_call("uart", retry_id=retry_id) if hooks_available else None,
        hook_diagnostics=tuple(hook_diagnostics),
        remedies=(LOCKED_ENVIRONMENT_UART_DIAGNOSTIC,),
    )


_HOOK_FAILURE_MESSAGES = {
    DISCOVERY_HOOK_FAILED: (
        "A discovery hook did not complete successfully. Read its failure class and "
        "exit code below, repair the hook file, then call {refresh} again."
    ),
    DISCOVERY_HOOK_TIMEOUT: (
        "A discovery hook exceeded its configured deadline and was terminated with its "
        "whole process group. Make it return faster or raise its timeout_seconds within "
        "the allowed maximum, then call {refresh} again."
    ),
    DISCOVERY_HOOK_OUTPUT_INVALID: (
        "A discovery hook ran but its output did not match the published schema. Compare "
        "the excerpt below against output_schema from {contract}, fix the hook so it "
        "prints exactly one JSON document to stdout and nothing else, then call "
        "{refresh} again."
    ),
    DISCOVERY_HOOK_SOURCE_CHANGED: (
        "A hook file changed after the refresh that admitted it, so it was refused "
        "without being executed. Call {refresh} again to re-admit the current bytes."
    ),
}


def hook_failure(
    code: str,
    kind: FailureKind,
    *,
    hook_diagnostics: Sequence[Mapping[str, object]],
    retry_id: str | None = None,
) -> DiscoveryFailure:
    """A hook ran (or was refused) and did not yield usable rows."""

    if code not in _HOOK_FAILURE_MESSAGES:
        raise ValueError(f"{code} is not a hook failure code")
    message = _HOOK_FAILURE_MESSAGES[code].format(
        refresh=f"{REFRESH_TOOL}(retry_id=...)",
        contract=f'{CONTRACT_TOOL}(kind="{kind}")',
    )
    return DiscoveryFailure(
        code=code,
        message=message,
        kind=kind,
        hook_contract_call=contract_call(kind, retry_id=retry_id),
        refresh_call=refresh_call(retry_id),
        hook_diagnostics=tuple(hook_diagnostics),
    )


def unsupported_provider_failure(
    provider: str,
    *,
    registered_providers: Sequence[str],
) -> DiscoveryFailure:
    """Discovery worked; the installed pyOCD cannot drive the named provider.

    A hook cannot fix this, so no contract call is offered. Saying otherwise would send
    the agent into a loop rewriting a hook that already did its job.
    """

    message = (
        f"A discovery hook named the provider '{provider}', which the installed pyOCD "
        "does not register, so no amount of hook repair can make it openable. Tell the "
        "user which providers this installation supports and stop. "
        f"Registered providers: {', '.join(registered_providers)}."
    )
    return DiscoveryFailure(
        code=DISCOVERY_UNSUPPORTED_PROVIDER,
        message=message,
        kind="probe",
        remedies=("install or enable a pyOCD probe plug-in that registers this provider",),
    )


def selection_disappeared_failure(reason: str, kind: FailureKind = "probe") -> DiscoveryFailure:
    """A recorded selection is gone. Reroute through setup; never substitute."""

    message = (
        f"{reason} Route back through setup_overview so the user can identify the "
        "current physical connection. Do not substitute a similarly described device."
    )
    return DiscoveryFailure(
        code=DISCOVERY_SELECTION_DISAPPEARED,
        message=message,
        kind=kind,
        remedies=("rerun setup_overview and reselect the connection",),
    )


def open_failure_payload(
    code: str,
    *,
    detail: str,
    identity: str | None = None,
) -> dict[str, object]:
    """A backend-open failure. Structurally incapable of carrying a hook contract call.

    Discovery already succeeded: the device was found and named. This is an action
    failure, and the remedies are driver, contention, firmware, and physical checks --
    never "go write a hook".
    """

    if code not in OPEN_FAILURE_CODES:
        raise ValueError(f"{code} is not a backend-open failure code")
    checks = PROBE_OPEN_FAILED_CHECKS if code == PROBE_OPEN_FAILED else UART_OPEN_FAILED_CHECKS
    subject = "debug probe" if code == PROBE_OPEN_FAILED else "serial port"
    message = (
        f"The {subject} was found, but opening it failed: {detail} This is not a "
        "discovery problem, so do not call the discovery hook tools. Check the items "
        "listed under remedies with the user, then retry the same call."
    )
    payload: dict[str, object] = {
        "code": code,
        "agent_prompt": message,
        "remedies": list(checks),
        "detail": detail,
    }
    if identity is not None:
        payload["identity"] = identity
    assert "hook_contract_call" not in payload
    return payload


def carries_hook_contract(payload: Mapping[str, Any]) -> bool:
    """True when a payload offers a hook contract anywhere an agent would find it."""

    if "hook_contract_call" in payload or "refresh_call" in payload:
        return True
    return any(
        isinstance(value, Mapping) and carries_hook_contract(value) for value in payload.values()
    )
