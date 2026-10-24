"""Test-only, non-reaping cleanup receipts. Observations never grant authority.

No reads of protocol streams, extra poll/wait calls, signals or helper launches
occur here. OS identity queries are snapshots, not exhaustive containment proof.
All byte/event/receipt reads are capped with explicit gaps. Instrumentation adds
overhead, so an observed duration alone is not a causal or cleanup-success claim.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import traceback
import uuid


def exception_facts(exc):
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "errno": getattr(exc, "errno", None),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def value_facts(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
            "prefix_hex": value[:4096].hex(),
            "truncated": len(value) > 4096,
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [value_facts(item) for item in value[:32]] + (
            [{"gap": "items beyond 32"}] if len(value) > 32 else []
        )
    if isinstance(value, dict):
        return {str(k): value_facts(v) for k, v in value.items()}
    return {"type": type(value).__name__, "python_object_id": id(value)}


def process_facts(pid, expected_birth=None):
    """Query identity without Popen.poll/wait or psutil.wait (which can reap)."""
    result = {
        "pid": pid,
        "expected_creation_time": expected_birth,
        "at_monotonic": time.monotonic(),
    }
    try:
        import psutil

        process = psutil.Process(pid)
        birth = process.create_time()
        result["creation_time"] = birth
        if expected_birth is not None and birth != expected_birth:
            return {
                **result,
                "identity": "reused",
                "gap": "PID no longer identifies the observed process",
            }
        result["identity"] = "observed" if expected_birth is None else "same_birth"
        for name in ("ppid", "cmdline", "exe", "status", "username"):
            try:
                result[name] = getattr(process, name)()
            except (OSError, psutil.Error) as exc:
                result[name] = {"unavailable": exception_facts(exc)}
        for name in ("getpgid", "getsid"):
            try:
                result[name] = getattr(os, name)(pid)
            except (AttributeError, OSError) as exc:
                result[name] = {"unavailable": exception_facts(exc)}
        # The facts above are not atomic. Never attach them to a replacement PID.
        try:
            result["birth_after_queries"] = psutil.Process(pid).create_time()
            if result["birth_after_queries"] != birth:
                result["identity"] = "changed_during_observation"
        except psutil.NoSuchProcess:
            result["identity"] = "gone_during_observation"
    except ImportError as exc:
        result.update(identity="unavailable", error=exception_facts(exc))
    except psutil.NoSuchProcess as exc:
        result.update(
            identity="missing",
            error=exception_facts(exc),
            gap="No current process identity; absence alone does not prove cleanup authority",
        )
    except Exception as exc:
        # Missing/denied identity is evidence of a gap, never of successful cleanup.
        result.update(identity="unavailable", error=exception_facts(exc))
    return result


def descendant_facts(pid, birth, boundary, directory=None):
    """Snapshot an observed tree without polling, waiting, reaping or signaling.

    Retain the child's actual argv/image and its contemporaneous parent identity.
    A tree observation is ownership evidence, not a guessed functional role.
    Short-lived processes between snapshots are outside this observation's scope.
    """
    rows, gaps = [], []
    result = {
        "descendants": rows,
        "evidence_gaps": gaps,
        "boundary": boundary,
        "at_monotonic": time.monotonic(),
        "observer_pid": os.getpid(),
    }
    leader = process_facts(pid, birth)
    result["root_identity"] = leader
    if birth is None or leader.get("identity") != "same_birth":
        gaps.append({"gap": "Observed root identity unavailable or changed", "identity": leader})
        return result
    try:
        import psutil
    except ImportError as exc:
        gaps.append({"error": exception_facts(exc)})
        return result
    try:
        processes = psutil.Process(pid).children(recursive=True)
        for process in processes:
            try:
                child_birth = process.create_time()
                identity = process_facts(process.pid, child_birth)
                parent_pid = identity.get("ppid")
                parent = (
                    process_facts(parent_pid)
                    if type(parent_pid) is int
                    else {"gap": "No contemporaneous parent PID"}
                )
                role = (
                    role_evidence(process.pid, child_birth, directory)
                    if directory is not None and (Path(directory) / "roles.jsonl").is_file()
                    else {"role": "unknown; no exact role receipt", "matches": []}
                )
                row = {
                    "pid": process.pid,
                    "creation_time": child_birth,
                    "identity": identity,
                    "parent_identity": parent,
                    "role_evidence": role,
                    "boundary": boundary,
                    "at_monotonic": time.monotonic(),
                    "observer_pid": os.getpid(),
                    "ownership": "captured recursive descendant of exact observed root",
                    "root_pid": pid,
                    "root_creation_time": birth,
                }
                rows.append(row)
                if identity.get("identity") != "same_birth":
                    gaps.append({"gap": "Child changed or unavailable during capture", "identity": identity})
                if parent.get("identity") != "observed":
                    gaps.append({"gap": "Parent identity unavailable during capture", "identity": parent})
                for field in ("ppid", "cmdline", "exe", "status"):
                    if not identity.get(field) or isinstance(identity[field], dict):
                        gaps.append({"gap": "Child " + field + " unavailable during capture",
                                     "identity": identity})
            except (OSError, psutil.Error) as exc:
                gaps.append({"pid": process.pid, "error": exception_facts(exc)})
        after = process_facts(pid, birth)
        result["root_after_capture"] = after
        if after.get("identity") not in {"same_birth", "missing"}:
            gaps.append({"gap": "Root changed during tree capture", "identity": after})
    except (OSError, psutil.Error) as exc:
        gaps.append({"error": exception_facts(exc)})
    return result


def terminal_identity(facts):
    """Classify recorded facts only; unknown/reused identities are never gone."""
    identity = facts.get("identity")
    birth = facts.get("expected_creation_time")
    if type(birth) not in (int, float) or not math.isfinite(birth) or birth <= 0:
        return "unknown"
    if identity == "missing":
        return "gone"
    if (
        identity == "same_birth"
        and facts.get("creation_time") == birth
        and facts.get("birth_after_queries") == birth
    ):
        status = facts.get("status")
        if status in ("zombie", "dead"):
            return "terminal"
        if isinstance(status, str) and status:
            return "live"
    return "unknown"


def execution_relationship(launcher, interpreter, command, argv, expected_argv, parent=None):
    """Join recorded execution facts, never a PID-only or descendant ownership claim.

    A Windows venv launcher can delegate the unchanged command to base Python.
    Require the specific interpreter's self-report and stable OS identities; a
    child also needs a contemporaneous observation of that exact parent birth.
    This pure join neither queries processes nor grants any cleanup authority.
    """
    gaps = []
    result = {
        "relationship": "unknown",
        "evidence_gaps": gaps,
        "launcher": launcher,
        "interpreter": interpreter,
        "parent": parent,
        "command": command,
        "argv": argv,
        "expected_argv": expected_argv,
    }

    def stable(label, facts):
        if not isinstance(facts, dict):
            gaps.append(label + ": no identity observation")
            return
        pid, birth = facts.get("pid"), facts.get("creation_time")
        if (
            type(pid) is not int
            or pid <= 0
            or type(birth) not in (int, float)
            or not math.isfinite(birth)
            or birth <= 0
            or facts.get("birth_after_queries") != birth
            or facts.get("identity") not in {"observed", "same_birth"}
            or (
                facts.get("identity") == "same_birth"
                and facts.get("expected_creation_time") != birth
            )
        ):
            gaps.append(label + ": missing, reused or unstable birth identity")
        if (
            type(facts.get("ppid")) is not int
            or facts["ppid"] <= 0
            or not isinstance(facts.get("exe"), str)
            or not facts["exe"]
            or not isinstance(facts.get("status"), str)
            or not facts["status"]
            or facts["status"] in {"zombie", "dead"}
        ):
            gaps.append(label + ": unavailable parent, executable or live status")

    def arguments(value):
        return isinstance(value, (list, tuple)) and bool(value) and all(
            isinstance(item, str) for item in value
        )

    stable("launcher", launcher)
    stable("interpreter", interpreter)
    if not arguments(command) or len(command) < 2:
        gaps.append("No exact dispatched Python command")
    else:
        for label, facts in (("launcher", launcher), ("interpreter", interpreter)):
            cmdline = facts.get("cmdline") if isinstance(facts, dict) else None
            if not arguments(cmdline) or list(cmdline[1:]) != list(command[1:]):
                gaps.append(label + ": command does not identify the dispatched module")
    if (
        not arguments(expected_argv)
        or not arguments(argv)
        or list(argv) != list(expected_argv)
    ):
        gaps.append("Missing or mismatched executing module argv self-report")
    if gaps:
        return result
    if launcher["pid"] == interpreter["pid"]:
        if any(
            launcher[key] != interpreter[key]
            for key in ("creation_time", "ppid", "exe", "cmdline")
        ):
            gaps.append("Direct execution does not have the same birth and command identity")
        else:
            result["relationship"] = "direct"
    else:
        stable("parent at execution", parent)
        if not gaps and (
            interpreter["ppid"] != launcher["pid"]
            or interpreter["creation_time"] < launcher["creation_time"]
            or any(
                parent.get(key) != launcher[key]
                for key in ("pid", "creation_time", "ppid", "exe", "cmdline")
            )
        ):
            gaps.append("Interpreter is not the child of the recorded same-birth launcher")
        if not gaps:
            result["relationship"] = "launcher-child"
    return result


def stream_facts(stream):
    if stream is None:
        return {"stream": None}
    result = {"type": type(stream).__name__, "closed": stream.closed}
    try:
        result["fileno"] = stream.fileno()
    except (OSError, ValueError) as exc:
        result["fileno"] = {"unavailable": exception_facts(exc)}
    return result


def popen_facts(process):
    """Read cached Popen state only; returncode=None does not prove liveness."""
    return {
        "pid": process.pid,
        "argv": value_facts(process.args),
        "cached_returncode": process.returncode,
        "stdin": stream_facts(process.stdin),
        "stdout": stream_facts(process.stdout),
        "stderr": stream_facts(process.stderr),
    }


def marker_facts(path):
    if path is None:
        return {"path": None, "state": "no marker reference"}
    result = {"path": str(path)}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(16385)
        result.update(
            state="read",
            bytes_observed=len(raw),
            prefix_hex=raw[:16384].hex(),
            truncated=len(raw) > 16384,
        )
        if len(raw) <= 16384:
            result["sha256"] = hashlib.sha256(raw).hexdigest()
    except OSError as exc:
        result.update(state="unavailable", error=exception_facts(exc))
    return result


def read_receipt(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return {"path": str(path), "gap": "receipt exceeds 1 MiB observation cap"}
        return {
            "path": str(path),
            "records": [json.loads(line) for line in raw.splitlines() if line.strip()],
        }
    except (OSError, ValueError) as exc:
        return {"path": str(path), "gap": exception_facts(exc)}


class CleanupTrace:
    def __init__(self, label, directory=None):
        self.label = label
        selected = directory or os.environ.get("BYO_FINAL_CLEANUP_EVIDENCE")
        self.directory = None if selected is None else Path(selected)
        self.path = (
            None
            if self.directory is None
            else self.directory
            / ("cleanup-" + str(os.getpid()) + "-" + uuid.uuid4().hex + ".jsonl")
        )
        self.events = []
        self.gaps = [] if self.path else ["No durable observation directory was supplied"]
        self._guard = threading.Lock()

    def record(self, kind, **facts):
        with self._guard:
            if len(self.events) >= 1024:
                if "event cap reached" not in self.gaps:
                    self.gaps.append("event cap reached")
                return
            row = {
                "kind": kind,
                "label": self.label,
                "sequence": len(self.events),
                "pid": os.getpid(),
                "observer_pid": os.getpid(),
                "thread": threading.get_ident(),
                "at_monotonic": time.monotonic(),
                **facts,
            }
            self.events.append(row)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, default=value_facts) + "\n")
                except (OSError, ValueError) as exc:
                    self.gaps.append(exception_facts(exc))

    def invoke(self, name, original, *args, **kwargs):
        """Call exactly once, with untouched arguments, result and raised object."""
        started = time.monotonic()
        self.record(
            "operation-start", operation=name, args=value_facts(args), kwargs=value_facts(kwargs)
        )
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            self.record(
                "operation-error",
                operation=name,
                elapsed=time.monotonic() - started,
                error=exception_facts(exc),
            )
            raise
        self.record(
            "operation-return",
            operation=name,
            elapsed=time.monotonic() - started,
            result=value_facts(result),
        )
        return result

    def delegate(self, name, original):
        @functools.wraps(original)
        def observed(*args, **kwargs):
            return self.invoke(name, original, *args, **kwargs)

        return observed

    def summary(self):
        return {
            "path": None if self.path is None else str(self.path),
            "events": self.events,
            "evidence_gaps": self.gaps,
            "scope": "bounded observations; no cleanup authority",
        }

    def role(self, pid, role, source, expected_birth=None):
        facts = process_facts(pid, expected_birth)
        row = {
            "pid": pid,
            "creation_time": facts.get("creation_time"),
            "role": role,
            "source": source,
            "identity": facts,
        }
        self.record("registered-role", **row)
        if self.directory is not None:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with (self.directory / "roles.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\n")
            except OSError as exc:
                self.gaps.append(exception_facts(exc))
        return facts


def role_evidence(pid, birth, directory):
    receipt = read_receipt(Path(directory) / "roles.jsonl")
    records = receipt.get("records", [])
    matches = [
        r
        for r in records
        if r.get("pid") == pid
        and r.get("creation_time") == birth
        and birth is not None
        and r.get("identity", {}).get("identity") in {"observed", "same_birth"}
        and r["identity"].get("birth_after_queries") == birth
    ]
    return {
        "matches": matches,
        "receipt_gap": receipt.get("gap"),
        "role": "registered exact identity" if matches else "unknown; no exact role receipt",
    }
