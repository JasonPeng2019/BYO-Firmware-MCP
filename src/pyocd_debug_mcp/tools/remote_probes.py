"""The two remote-probe MCP handlers: register a probe-server endpoint, unregister it.

Neither tool is a hardware action. `register_remote_probe` opens no probe and no
session -- it attempts one bounded TCP connect to report reachability honestly, then
writes the registry either way. `unregister_remote_probe` only edits the registry file.
Both are registered visible and unlocked, exactly like the discovery-hook tools next to
them in `server.py` and for the same reason: an agent that has just learned pyOCD cannot
see a probe over local USB must be able to reach the one route that survives that
(`pyocd server` plus this registration) without first unlocking anything.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from pyocd_debug_mcp import remote_probes

DEFAULT_REMOTE_PROBE_PORT = 5555


def _json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


@dataclass
class RemoteProbeToolServices:
    """Everything the two handlers need, injected so they stay testable."""

    registry_path: Callable[[], Path]
    check_endpoint: Callable[[str, int], bool] = functools.partial(
        remote_probes.check_endpoint, timeout_seconds=remote_probes.DEFAULT_CHECK_TIMEOUT_SECONDS
    )


def build_remote_probe_handlers(
    services: RemoteProbeToolServices,
) -> dict[str, Callable[..., str]]:
    """Build the always-visible, non-authorizing remote-probe registry tool surface."""

    def register_remote_probe(
        host: str,
        port: int = DEFAULT_REMOTE_PROBE_PORT,
        description: str | None = None,
    ) -> str:
        """Register a `pyocd server` endpoint so it appears as a normal probe row.

        Reach for this when pyOCD cannot see a debug probe over local USB at all --
        a broken libusb/driver stack on this machine, a WSL or container environment
        that needs to reach a probe owned by the host, or a probe physically attached
        to a different machine. It is the delivery mechanism for the one route that
        still works in that case: `remote:<host>:<port>` addresses a `pyocd server`
        process instead of local USB.

        The user (not this tool) must start that process on the machine that
        physically owns the probe: `pyocd server -p <port> -u <probe-uid>`. On
        Windows that process needs `PYTHONIOENCODING=utf-8` set in its environment,
        or it crashes while printing its own probe table -- tell the user this if
        they are on Windows.

        Parameters:
            host: hostname or IP address of the machine running `pyocd server`, e.g.
                "192.168.1.20" or "localhost".
            port: TCP port `pyocd server` is listening on, an integer 1-65535.
                Default 5555, pyOCD's own default port.
            description: optional free-text label for this endpoint, e.g.
                "bench ST-LINK".

        Example: register_remote_probe(host="192.168.1.20", port=5555,
        description="bench ST-LINK").

        Returns a JSON object with `selector` (the exact `remote:<host>:<port>`
        string that will appear as the row's `unique_id` in every future inventory
        snapshot), `reachable` (whether a bounded TCP connect attempt to the endpoint
        succeeded just now), and `agent_prompt` telling you what to do next.

        This tool probes the endpoint and reports honestly whether it answered, but
        **registers it either way** -- an endpoint that does not answer right now may
        simply not have its server started yet, and refusing to register it would be
        paternalism, not correctness. Re-registering an existing `host:port` updates
        its description instead of creating a duplicate row.

        Common failure: an empty host, or a port outside 1-65535, is rejected before
        anything is written -- supply a real host and an in-range port and call again.
        """

        try:
            normalized_host = remote_probes.normalize_host(host)
            normalized_port = remote_probes.normalize_port(port)
        except remote_probes.RemoteProbeError as exc:
            return _json(
                {
                    "status": "remote_probe_rejected",
                    "code": "remote_probe/invalid-endpoint",
                    "agent_prompt": (
                        f"{exc} Provide a non-empty host and a port between 1 and "
                        "65535, then call register_remote_probe again."
                    ),
                }
            )

        registry_path = services.registry_path()
        # Reachability is resolved entirely outside the registry's own critical
        # section: a TCP connect can take up to a few seconds, and holding the
        # write lock across it would serialize two unrelated registrations behind
        # each other's network timeout. `register_entry` does its own fresh load
        # under the lock, so this ordering cannot lose a concurrent write -- see
        # `remote_probes._registry_lock`.
        reachable = services.check_endpoint(normalized_host, normalized_port)
        remote_probes.register_entry(
            registry_path, normalized_host, normalized_port, description or ""
        )
        selector = f"remote:{normalized_host}:{normalized_port}"

        if reachable:
            agent_prompt = (
                f"{selector} answered a TCP connect attempt just now and is registered. "
                "It will appear as a 'remote' provider row in every future inventory "
                "snapshot; use its unique_id verbatim as the probe selector."
            )
        else:
            agent_prompt = (
                f"{selector} did NOT answer a TCP connect attempt just now, but it is "
                "registered anyway -- an unreachable endpoint right now is not proof of "
                "a mistake, the server may not be started yet. Tell the user to run "
                f"'pyocd server -p {normalized_port} -u <probe-uid>' on the machine that "
                "owns the probe (Windows needs PYTHONIOENCODING=utf-8 in that process's "
                "environment or it crashes printing its own probe table), then take a "
                "fresh inventory snapshot to confirm it now answers."
            )

        return _json(
            {
                "status": "remote_probe_registered",
                "selector": selector,
                "host": normalized_host,
                "port": normalized_port,
                "description": description or "",
                "reachable": reachable,
                "agent_prompt": agent_prompt,
            }
        )

    def unregister_remote_probe(host: str, port: int = DEFAULT_REMOTE_PROBE_PORT) -> str:
        """Remove a previously registered `pyocd server` endpoint from the registry.

        Reach for this when a registered remote probe endpoint was mistyped,
        decommissioned, or is otherwise no longer wanted -- without it a typo'd
        endpoint is permanent and pollutes every future inventory snapshot forever.

        Parameters:
            host: the exact host that was passed to register_remote_probe.
            port: the exact port that was passed to register_remote_probe, an
                integer 1-65535. Default 5555.

        Example: unregister_remote_probe(host="192.168.1.20", port=5555).

        Returns a JSON object with `removed` (true if an entry was actually deleted)
        and `agent_prompt`. Removing an endpoint that was never registered is not an
        error: the response says so plainly and nothing on disk changes.

        Common failure: an empty host, or a port outside 1-65535, is rejected before
        the registry is even read -- supply the exact host and port that were
        registered and call again.
        """

        try:
            normalized_host = remote_probes.normalize_host(host)
            normalized_port = remote_probes.normalize_port(port)
        except remote_probes.RemoteProbeError as exc:
            return _json(
                {
                    "status": "remote_probe_rejected",
                    "code": "remote_probe/invalid-endpoint",
                    "agent_prompt": (
                        f"{exc} Provide a non-empty host and a port between 1 and "
                        "65535, then call unregister_remote_probe again."
                    ),
                }
            )

        registry_path = services.registry_path()
        _remaining, removed = remote_probes.unregister_entry(
            registry_path, normalized_host, normalized_port
        )
        selector = f"remote:{normalized_host}:{normalized_port}"

        status = "remote_probe_unregistered" if removed else "remote_probe_not_registered"
        return _json(
            {
                "status": status,
                "selector": selector,
                "host": normalized_host,
                "port": normalized_port,
                "removed": removed,
                "agent_prompt": (
                    f"{selector} was removed; it will no longer appear in inventory "
                    "snapshots."
                    if removed
                    else f"{selector} was not in the registry; nothing changed."
                ),
            }
        )

    return {
        "register_remote_probe": register_remote_probe,
        "unregister_remote_probe": unregister_remote_probe,
    }
