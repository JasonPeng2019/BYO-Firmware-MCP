"""Two content bars, because the two logging layers have different audiences.

**Mechanical bar** -- trail, ledger, counters, and every server-detected report.
Universal in every build and the only thing a professional build emits. No
payloads, no full host paths, no raw hardware identifiers; arguments survive only
as salted fingerprints.

**Narrative bar** -- personal builds only. This is the opt-in codebase-describing
layer, so it *allows* real symbol names, file names, and description of the code:
that is the point of it, not a leak. It rejects only verbatim payloads, which are
payloads rather than summary and add nothing as prose.

The fingerprint salt is a privacy secret, not an integrity one. Like a local
chain key it runs as the user and does not defend against the machine's own
owner; it defends against readers of the delivered reports, which is the correct
threat model because reports leave for the team's remote.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from hashlib import blake2b
from pathlib import Path
from typing import Any

from pyocd_debug_mcp.monitor.paths import deployment_salt

_MAX_SCALAR_CHARS = 512
_FINGERPRINT_BYTES = 8

# Keys whose values are hardware or host identifiers: recorded as truncated
# digests so they group correctly without being readable.
_IDENTIFIER_KEYS = frozenset(
    {
        "probe_uid",
        "probe_serial",
        "serial",
        "serial_number",
        "unique_id",
        "device_uid",
        "mcu_part_number",
        "target",
        "connection_id",
    }
)

# Keys that carry payload bytes or file bodies outright. Never recorded at all.
_PAYLOAD_KEYS = frozenset(
    {
        "data",
        "payload",
        "contents",
        "content",
        "bytes",
        "text",
        "value",
        "values",
        "buffer",
        "capture",
        "argv",
        "env",
        "environment",
    }
)

_PATH_KEYS = frozenset(
    {
        "path",
        "elf_path",
        "hex_path",
        "bin_path",
        "map_path",
        "output_dir",
        "datasheet_path",
        "board_config",
        "artifact",
        "artifact_path",
    }
)

_HEX_RUN = re.compile(r"(?:[0-9A-Fa-f]{2}[\s:,\-]?){32,}")
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{256,}={0,2}")
_ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|usr|opt|var)/)\S+")
_FLAGGED = re.compile(r"\s-{1,2}\w")
_DIGITS = re.compile(r"\d+")
_HEXNUM = re.compile(r"0[xX][0-9A-Fa-f]+")
_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

_SELF_RATING = re.compile(
    r"\b(?:i (?:did|performed|handled)\s+(?:well|poorly|badly|great)"
    r"|my performance|self-assess|i(?:'m| am) (?:confident|proud|ashamed)"
    r"|(?:rate|grade|score) (?:myself|my own))\b",
    re.IGNORECASE,
)


class NarrativeContentError(ValueError):
    """Model-authored prose carried a verbatim payload instead of a description."""


def _salted(text: str, size: int = _FINGERPRINT_BYTES) -> str:
    return blake2b(
        deployment_salt() + text.encode("utf-8", "replace"), digest_size=size
    ).hexdigest()


def fingerprint(value: object) -> str:
    """Return a salted, non-invertible fingerprint of a canonical argument set.

    Salting is what closes the reconstruction channel: an unsalted hash of a small
    or guessable input -- a register address, a short enum, a filename from a known
    set -- can be brute-forced back to its real value, and that recovered value is
    codebase content. Salting also stops the same value producing a matching
    fingerprint across reports, closing cross-report correlation.
    """

    from pyocd_debug_mcp.guardrails.plan_engine import canonical_json

    try:
        text = canonical_json(_jsonable(value))
    except Exception:  # noqa: BLE001 - a fingerprint must never break a tool call
        text = repr(value)
    return _salted(text)


def digest_id(value: str, keep: int = 4) -> str:
    """Return a truncated salted digest for a hardware or host identifier."""

    return _salted(value, size=max(2, keep))


def safe_path(value: str) -> str:
    """Return a path reduced to its basename plus a salted digest of the whole."""

    try:
        name = Path(value).name or "path"
    except (OSError, ValueError):
        name = "path"
    return f"{name}#{_salted(value, size=4)}"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)


def _looks_like_path(value: str) -> bool:
    return bool(_ABS_PATH.search(value)) or ("/" in value or "\\" in value)


def scrub_mechanical(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a mapping to identifiers, digests, and fingerprints only.

    This is the hard bar of the mechanical layer and applies to the trail, the
    ledger, and every server-detected report field. It is a content rule, not a
    size optimization: this server can read arbitrary device memory, including
    provisioning and security regions it deliberately permits reading, plus device
    unique IDs, probe serials, UART traffic, and absolute local host paths.
    """

    scrubbed: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key)
        lowered = name.casefold()
        if lowered in _PAYLOAD_KEYS or isinstance(value, (bytes, bytearray)):
            scrubbed[name] = "<omitted>"
            continue
        if lowered in _IDENTIFIER_KEYS and isinstance(value, str):
            scrubbed[name] = digest_id(value)
            continue
        if isinstance(value, str):
            if lowered in _PATH_KEYS or _looks_like_path(value):
                scrubbed[name] = safe_path(value)
                continue
            if len(value) > _MAX_SCALAR_CHARS or _HEX_RUN.search(value):
                scrubbed[name] = "<omitted>"
                continue
            scrubbed[name] = value
            continue
        if isinstance(value, Mapping):
            scrubbed[name] = scrub_mechanical(value)
            continue
        if isinstance(value, (list, tuple, set)):
            scrubbed[name] = f"<{len(value)} items>"
            continue
        if value is None or isinstance(value, (int, float, bool)):
            scrubbed[name] = value
            continue
        scrubbed[name] = "<omitted>"
    return scrubbed


def check_narrative(text: str, *, field: str = "narrative") -> None:
    """Validate model-authored prose against the narrative bar.

    Real symbol names, file names, and codebase description are allowed and are
    the purpose of this layer. Only verbatim payloads are rejected.
    """

    if _HEX_RUN.search(text):
        raise NarrativeContentError(
            f"{field}: remove the raw byte dump; describe what was read instead."
        )
    if _BASE64_RUN.search(text):
        raise NarrativeContentError(
            f"{field}: remove the encoded blob; describe what it was instead."
        )
    for line in text.splitlines():
        if _ABS_PATH.search(line) and len(_FLAGGED.findall(line)) >= 2:
            raise NarrativeContentError(
                f"{field}: remove the full command line; name the step instead."
            )


def check_no_self_rating(text: str, *, field: str = "effectiveness_observed") -> None:
    """Reject self-assessment in a check-in's outcomes section.

    Observable outcomes only: what was accomplished, what it got stuck on, where it
    needed retries. Self-rating is prohibited outright, so this rejects rather than
    warns.
    """

    if _SELF_RATING.search(text):
        raise NarrativeContentError(
            f"{field}: state observable outcomes only -- no self-rating or self-grading."
        )


def normalize_signature(text: str) -> str:
    """Normalize a message so the same fault groups across runs and boards."""

    normalized = _GUID.sub("#", text)
    normalized = _HEXNUM.sub("#", normalized)
    normalized = _ABS_PATH.sub("#", normalized)
    normalized = _DIGITS.sub("#", normalized)
    return " ".join(normalized.split())[:200]


def result_text(result: object) -> str:
    """Extract text from whatever a FastMCP tool call returned.

    Tools here are registered with ``structured_output=False``, so the framework
    returns a list of content blocks rather than a string. Classifying the raw
    value would make every non-error refusal read as a plain success, which is
    exactly the mistake that buries the signal this system exists to find.
    """

    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        if (
            len(result) == 2
            and isinstance(result[0], (list, tuple))
            and isinstance(result[1], Mapping)
        ):
            return result_text(result[0])
        parts: list[str] = []
        for block in result:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    text = getattr(result, "text", None)
    return text if isinstance(text, str) else ""


__all__ = [
    "NarrativeContentError",
    "check_narrative",
    "check_no_self_rating",
    "digest_id",
    "fingerprint",
    "normalize_signature",
    "result_text",
    "safe_path",
    "scrub_mechanical",
]
