"""The single delivery seam, with a no-op default and a filler for the interim.

The authenticated remote pipeline does not exist yet, so everything here must be
complete, correct, and useful with no remote transport present. The filler exists
so the periodic, closeout, and recovery paths are all exercised and testable
before that pipeline arrives.

The filler must never masquerade as a working transport. A stub that reports
success is worse than none: the failure mode is months of sessions believed to be
archived remotely while nothing ever left the machine. Its state is therefore
reported as *filler / simulated*, distinct from both *sent* and *not configured*,
and no code path may treat it as a durable off-box copy.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

_MAX_BREADCRUMBS = 100


class DeliveryState(str, Enum):
    SENT = "sent"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
    FILLER_SIMULATED = "filler_simulated"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    state: DeliveryState
    acked: frozenset[str]
    at: datetime | None
    detail: str | None = None

    @property
    def is_durable_off_box(self) -> bool:
        """Only a real remote produces a copy on another machine."""

        return self.state is DeliveryState.SENT


class Transport(Protocol):
    name: str
    origin: str

    def send_files(self, items: Sequence[tuple[str, Path]]) -> DeliveryResult: ...

    def send_report(self, report: Mapping[str, Any]) -> DeliveryResult: ...

    def close(self) -> None: ...


class NullTransport:
    """The default. The system must start, run, and exit normally with this."""

    name = "null"
    origin = "none"

    def send_files(self, items: Sequence[tuple[str, Path]]) -> DeliveryResult:
        del items
        return DeliveryResult(DeliveryState.NOT_CONFIGURED, frozenset(), None)

    def send_report(self, report: Mapping[str, Any]) -> DeliveryResult:
        del report
        return DeliveryResult(DeliveryState.NOT_CONFIGURED, frozenset(), None)

    def close(self) -> None:
        return None


class _EnvelopeWriter:
    """A Sentry transport that writes envelopes to the simulated remote.

    Building reports as genuine Sentry events now means the cutover to a real
    remote is a configuration change -- drop this writer, supply a DSN -- rather
    than a translation layer bolted on later.
    """

    def __init__(self, destination: Path) -> None:
        self._destination = destination

    def __call__(self, envelope: Any) -> None:  # pragma: no cover - see capture_envelope
        self.capture_envelope(envelope)

    def capture_envelope(self, envelope: Any) -> None:
        try:
            self._destination.mkdir(parents=True, exist_ok=True)
            name = f"envelope-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.txt"
            with (self._destination / name).open("wb") as handle:
                envelope.serialize_into(handle)
        except Exception:  # noqa: BLE001 - delivery is best-effort by contract
            return

    def flush(self, timeout: float, callback: Any = None) -> None:
        del timeout, callback

    def kill(self) -> None:
        return None


class SimulatedRemoteTransport:
    """The interim filler. Its destination is local, and it says so.

    A filler acknowledgement relocates a record to ``simulated_remote`` and frees
    the ``server_data`` copy. It is not a claim that a real off-box copy exists,
    and nothing here may be reported as *sent*.
    """

    name = "simulated_remote"
    origin = "filler"

    def __init__(
        self,
        destination: Path,
        workspace: str,
        *,
        server_version: str = "unknown",
    ) -> None:
        self._root = destination
        self._workspace = workspace
        self._guard = threading.Lock()
        self._client: Any = None
        self._server_version = server_version

    def _sentry_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import sentry_sdk
            from sentry_sdk.transport import Transport as SentryTransport

            writer = _EnvelopeWriter(self._root / self._workspace / "reports")

            class _LocalTransport(SentryTransport):
                def capture_envelope(self, envelope: Any) -> None:
                    writer.capture_envelope(envelope)

                def flush(self, timeout: float, callback: Any = None) -> None:
                    writer.flush(timeout, callback)

                def kill(self) -> None:
                    writer.kill()

            self._client = sentry_sdk.Client(
                dsn=None,
                transport=_LocalTransport(),
                default_integrations=False,
                auto_enabling_integrations=False,
                send_default_pii=False,
                attach_stacktrace=False,
                server_name=None,
                max_breadcrumbs=_MAX_BREADCRUMBS,
                release=self._server_version,
                environment="filler",
            )
        except Exception:  # noqa: BLE001 - a missing SDK must not break delivery
            self._client = False
        return self._client

    def send_files(self, items: Sequence[tuple[str, Path]]) -> DeliveryResult:
        acked: set[str] = set()
        destination = self._root / self._workspace / "ledger"
        for identity, path in items:
            try:
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination / path.name)
                acked.add(identity)
            except OSError:
                continue
        if not items:
            return DeliveryResult(
                DeliveryState.FILLER_SIMULATED, frozenset(), datetime.now(timezone.utc)
            )
        if not acked:
            return DeliveryResult(DeliveryState.FAILED, frozenset(), None, "copy failed")
        return DeliveryResult(
            DeliveryState.FILLER_SIMULATED, frozenset(acked), datetime.now(timezone.utc)
        )

    def send_report(self, report: Mapping[str, Any]) -> DeliveryResult:
        client = self._sentry_client()
        # A problem report carries report_id; a usage/checkin summary carries
        # summary_id instead -- both are stable delivery identities, so either
        # is acceptable here.
        identity = str(report.get("report_id") or report.get("summary_id") or "")
        if client:
            try:
                client.capture_event(_as_sentry_event(report))
            except Exception:  # noqa: BLE001 - never let reporting break the server
                pass
        # The JSON copy is written regardless, so a report survives an SDK problem.
        try:
            destination = self._root / self._workspace / "reports"
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"{identity or 'report'}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            return DeliveryResult(DeliveryState.FAILED, frozenset(), None, "write failed")
        return DeliveryResult(
            DeliveryState.FILLER_SIMULATED,
            frozenset({identity}) if identity else frozenset(),
            datetime.now(timezone.utc),
        )

    def close(self) -> None:
        with self._guard:
            client = self._client
            self._client = None
        if client:
            try:
                client.close(timeout=0.1)
            except Exception:  # noqa: BLE001
                pass


class TestTransport:
    """Drives the staleness backstop through its states without a real backend."""

    name = "test"
    origin = "test"

    def __init__(self, *, fail_always: bool = False) -> None:
        self.fail_always = fail_always
        self.sent_files: list[str] = []
        self.sent_reports: list[Mapping[str, Any]] = []

    def _result(self, acked: frozenset[str]) -> DeliveryResult:
        if self.fail_always:
            return DeliveryResult(DeliveryState.FAILED, frozenset(), None, "forced")
        return DeliveryResult(DeliveryState.SENT, acked, datetime.now(timezone.utc))

    def send_files(self, items: Sequence[tuple[str, Path]]) -> DeliveryResult:
        if self.fail_always:
            return self._result(frozenset())
        identities = {identity for identity, _ in items}
        self.sent_files.extend(sorted(identities))
        return self._result(frozenset(identities))

    def send_report(self, report: Mapping[str, Any]) -> DeliveryResult:
        if not self.fail_always:
            self.sent_reports.append(report)
        identity = str(report.get("report_id") or report.get("summary_id") or "")
        return self._result(frozenset({identity}) if identity else frozenset())

    def close(self) -> None:
        return None


def _as_sentry_event(report: Mapping[str, Any]) -> dict[str, Any]:
    """Map the report contract onto Sentry's native event fields.

    Grouping identity becomes the fingerprint, classification becomes tags, and the
    board-scoped trail becomes breadcrumbs, so a real remote gets a triageable
    issue on day one of cutover with no format translation.
    """

    trail = report.get("trail") or []
    breadcrumbs = [
        {
            "type": "default",
            "category": "tool",
            "message": str(entry.get("tool", "")),
            "level": "error" if entry.get("outcome") == "unexpected_error" else "info",
            "data": {
                "outcome": entry.get("outcome"),
                "error_class": entry.get("error_class"),
                "args_fp": entry.get("args_fp"),
                "board": entry.get("board"),
            },
        }
        for entry in trail
        if isinstance(entry, Mapping)
    ]
    return {
        "message": report.get("title", "issue"),
        "level": report.get("severity", "warning"),
        "fingerprint": [str(report.get("grouping_key", "unknown"))],
        "logger": "byo.monitor",
        "tags": {
            "signal_type": report.get("signal_type"),
            "triage_class": report.get("triage_class"),
            "origin": report.get("origin"),
            "tool_name": report.get("tool_name"),
            "narrative_logging": (report.get("environment") or {}).get(
                "narrative_logging"
            ),
        },
        "contexts": {
            "guard_state": dict(report.get("guard_state") or {}),
            "board_scope": dict(report.get("board_scope") or {}),
            "byo": {
                "workspace_token": report.get("workspace_token"),
                "report_id": report.get("report_id"),
                "suppressed_since_last": report.get("suppressed_since_last"),
            },
        },
        "breadcrumbs": {"values": breadcrumbs[-_MAX_BREADCRUMBS:]},
        "extra": {"description": report.get("description")},
    }


__all__ = [
    "DeliveryResult",
    "DeliveryState",
    "NullTransport",
    "SimulatedRemoteTransport",
    "TestTransport",
    "Transport",
]
