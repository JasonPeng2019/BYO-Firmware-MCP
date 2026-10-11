#!/usr/bin/env python3
"""Verify privacy-redacted evidence and its deterministic output manifest.

The private source captures are intentionally not repository artifacts.  This
verifier records the exact bytes of every retained legacy derivative, scans all
current evidence for generic path/identifier leaks, and records the classes of
replacement used to produce those derivatives.  Generated release-candidate
logs are scanned but excluded from the transformation manifest because they are
created directly from this public checkout rather than sanitized from a private
capture.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "sentry-evidence" / "SANITIZATION_MANIFEST.json"
EVIDENCE_ROOTS = (
    ROOT / "sentry-evidence" / "results",
    ROOT / "fallback-support-evidence" / "results",
)
EVIDENCE_BUNDLES = (
    ROOT / "sentry-evidence",
    ROOT / "fallback-support-evidence",
    ROOT / "src",
    ROOT / "tests",
)
OPERATOR_PATH_PATTERNS = (
    re.compile(r"(?i)(?:[A-Za-z]:)?[\\/]+Users[\\/]+[^\\/\s]+"),
    re.compile(r"(?i)/(?:home|Users)/[^/\s]+"),
    re.compile(r"(?i)/root/[^/\s]+"),
    re.compile(r"(?i)/(?:jumbo/lisp|thayerfs/home)/[^/\s]+"),
    # A historical UTF-16 capture lost its leading ``C:\\U`` bytes before the
    # privacy pass. Catch that partial home-path shape as well as intact paths.
    re.compile(r"(?i)(?:^|[^A-Za-z])sers\\[^\\\s]+\\(?:Documents|Desktop)\\"),
)
STRUCTURED_IDENTIFIER_KEYS = frozenset(
    {
        "board_id",
        "board_identity",
        "board_serial",
        "board_serial_number",
        "board_uid",
        "connection_id",
        "connection_uid",
        "device_id",
        "device_uid",
        "jlink_serial",
        "mcu_id",
        "mcu_serial",
        "mcu_uid",
        "probe_id",
        "probe_identity",
        "probe_serial",
        "probe_serial_number",
        "probe_uid",
        "serial_id",
        "serial_number",
        "stlink_serial",
        "unique_id",
        "usb_serial",
    }
)
SAFE_STRUCTURED_IDENTIFIERS = frozenset(
    {
        "<one connections[].connection_id selected from its friendly_name>",
        "<one serial_choices[].choice_id selected from its friendly_name>",
        "connection_1",
        "left_controller",
        "nucleo_l476rg",
        "probe:redacted-stlink-probe",
        "probe:redacted-jlink-probe-a",
        "probe:redacted-jlink-probe-b",
        "probe-associated-serial-id",
        "probe:synthetic-probe-a",
        "redacted-jlink-probe-a",
        "redacted-jlink-probe-b",
        "redacted-stlink-probe",
        "synthetic-probe-a",
    }
)
ESCAPED_CODE_POINT = re.compile(r"\\U([0-9a-fA-F]{8})|\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")
IDENTIFIER_FRAGMENT = re.compile(r"[A-Za-z0-9]{1,40}")
RAW_STRUCTURED_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_.:-])"
    r"(?:"
    r"(?=[A-Za-z0-9_.:-]{8,}(?![A-Za-z0-9_.:-]))(?=[A-Za-z0-9_.:-]*\d)"
    r"[A-Za-z0-9_.:-]+"
    r"|[A-Z][A-Z_.:-]{7,}"
    r")(?![A-Za-z0-9_.:-])"
)
SOURCE_COMMENT = re.compile(r"/\*.*?\*/|//[^\r\n]*|\#[^\r\n]*", re.DOTALL)
FORBIDDEN_IDENTIFIER_SHA256 = frozenset(
    {
        "12790578625ffb8943552d5c365816d8d41222537d32f11557074fe0c271abb8",
        "b89e269cb4f42e6970f0faf2f462d38e6a22c0511b89e1c6739b29509313dc67",
        "654b7da86fa4053a51078d25e84dc8e4756c3eb0623218989b2045fbed315c29",
        "d41d42149b889c2f0b8537bfe1cb6faf1fc421cde76fe1c3bde1c8107d388c85",
        "38e16f26244943480e98490a39f18ee7b170e3902cbfcd0adc28e469cb1fee64",
        "ae64b5a24907b3b57408cbfb68731cdab9c01bf6935472cdb923c22d2a9f0dce",
    }
)
FORBIDDEN_OPERATOR_COMPONENT_SHA256 = frozenset(
    {
        # Exact account components are represented only by one-way digests so
        # the privacy gate does not recreate the private value in source.
        "a9d0111f6cfae817e12f1641914630124f74513a9b391d4110872bfd9e33ed0c",
    }
)
CONFUSABLE_SEPARATORS = str.maketrans(
    {
        "\u2044": "/",
        "\u2215": "/",
        "\u2571": "/",
        "\u29f8": "/",
        "\u2216": "\\",
        "\u2572": "\\",
        "\u29f5": "\\",
        "\u29f9": "\\",
        "\u02d0": ":",
        "\u2236": ":",
        "\u2982": ":",
        "\ua789": ":",
    }
)
GENERATED_RELEASE_LOG = re.compile(r"^0\.1\.8rc1-.*\.log$")
AUDITED_BINARY_SHA256 = {
    "sentry-evidence/results/artifacts/.firm/evidence/datasheets/"
    "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65.pdf": (
        "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"
    ),
    "sentry-evidence/results/artifacts/.firm/packs/files/Keil.STM32L4xx_DFP.3.1.0.pack": (
        "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
    ),
    "fallback-support-evidence/results/hil-artifacts/.firm/evidence/datasheets/"
    "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65.pdf": (
        "a45a857e3aa75ac166dd532c76d76d5dd8377b9c5bf6f15c03c9cf85aeec0f65"
    ),
    "fallback-support-evidence/results/hil-artifacts/.firm/packs/files/"
    "Keil.STM32L4xx_DFP.3.1.0.pack": (
        "5672383c07fbdcee0e471a33f4f8beb2e1f3200bc999244dcd6858e0e8e8203f"
    ),
}

TRANSFORMATION_RULES = (
    {
        "class": "operator-home-or-workspace-path",
        "replacement_markers": ["<redacted-home>", "<redacted-user>", "/workspace/redacted"],
    },
    {
        "class": "physical-debug-probe-identifier",
        "replacement_markers": [
            "redacted-stlink-probe",
            "redacted-jlink-probe-a",
            "redacted-jlink-probe-b",
            "identifier redacted",
        ],
    },
)


def read_evidence_text(path: Path) -> str | None:
    """Decode supported Unicode evidence without confusing UTF-32 for UTF-16."""

    payload = path.read_bytes()
    if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings = ("utf-32",)
    elif payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    elif payload and payload.count(b"\0") * 5 >= len(payload):
        candidates: list[tuple[int, int, int, str]] = []
        for width, encoding in (
            (4, "utf-32-le"),
            (4, "utf-32-be"),
            (2, "utf-16-le"),
            (2, "utf-16-be"),
        ):
            if len(payload) % width:
                continue
            try:
                decoded = payload.decode(encoding)
            except UnicodeDecodeError:
                continue
            bad = sum(
                character == "\0"
                or (unicodedata.category(character) == "Cc" and character not in "\r\n\t")
                for character in decoded
            )
            ascii_text = sum(
                character in "\r\n\t" or 0x20 <= ord(character) <= 0x7E for character in decoded
            )
            unusual = sum(
                unicodedata.category(character) in {"Cn", "Co", "Cs"} for character in decoded
            )
            # The retained evidence formats and every private path/identifier
            # marker are overwhelmingly ASCII.  Wrong-endian UTF-16 often
            # decodes cleanly into printable CJK, so control-character count
            # alone cannot choose an endianness; prefer the plausible textual
            # candidate after first minimizing invalid controls.
            candidates.append((bad, -ascii_text, unusual, decoded))
        if candidates:
            return min(candidates, key=lambda item: item[:3])[3]
        return None
    else:
        encodings = ("utf-8-sig",)
    try:
        return payload.decode(encodings[0])
    except UnicodeDecodeError:
        return None


def canonical_evidence_text(text: str) -> str:
    """Decode common escaping and confusables to a bounded fixed point."""

    def decode_code_point(match: re.Match[str]) -> str:
        encoded = next(group for group in match.groups() if group is not None)
        return chr(int(encoded, 16))

    for _ in range(64):
        decoded = html.unescape(unquote(text))
        decoded = ESCAPED_CODE_POINT.sub(decode_code_point, decoded)
        decoded = decoded.replace(r"\/", "/").replace(r"\\", "\\")
        decoded = unicodedata.normalize("NFKC", decoded).translate(CONFUSABLE_SEPARATORS)
        decoded = "".join(
            character for character in decoded if unicodedata.category(character) != "Cf"
        )
        if decoded == text:
            return decoded
        text = decoded
    raise ValueError("evidence text did not reach a canonical fixed point")


def structured_identifier_violations(path: Path, text: str) -> list[str]:
    """Reject structured hardware identity values without an exact safe prefix."""

    documents: list[object] = []
    decoder = json.JSONDecoder()

    def embedded_documents(candidate: str) -> list[object]:
        found: list[object] = []
        try:
            found.append(json.loads(candidate))
            return found
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        # Logs and JSON-encoded message fields may prefix an object with a
        # timestamp or level. Decode every complete embedded object/array.
        cursor = 0
        while cursor < len(candidate):
            starts = [
                position for token in ("{", "[") if (position := candidate.find(token, cursor)) >= 0
            ]
            if not starts:
                break
            start = min(starts)
            try:
                document, consumed = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                cursor = start + 1
                continue
            found.append(document)
            cursor = start + consumed
        return found

    documents.extend(embedded_documents(text))
    if not documents:
        for candidate in text.splitlines():
            documents.extend(embedded_documents(candidate))

    violations: list[str] = []

    def normalized_key(value: object) -> str:
        key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
        return re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").casefold()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    normalized_key(key) in STRUCTURED_IDENTIFIER_KEYS
                    and child is not None
                    and child != ""
                ):
                    normalized = str(child).strip()
                    if normalized.casefold() not in SAFE_STRUCTURED_IDENTIFIERS:
                        violations.append(f"non-redacted {key}")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            # Structured loggers sometimes JSON-encode a child object into a
            # message field. Recursively inspect only object/array strings so a
            # decoded scalar cannot recurse forever.
            for child in embedded_documents(value.strip()):
                if isinstance(child, (dict, list)):
                    walk(child)

    for document in documents:
        walk(document)

    # Some retained logs use key=value rather than JSON. Keep this deliberately
    # scoped to hardware-identity key shapes so ordinary IDs (run_id/report_id)
    # do not become false positives.
    assignment = re.compile(
        r"\b(?P<key>(?i:(?:board|connection|device|jlink|mcu|probe|stlink|usb)"
        r"[-_ ](?:id|identity|uid|serial(?:[-_ ]number)?))"
        r"|(?i:(?:board|connection|device|jlink|mcu|probe|stlink|usb))"
        r"(?:ID|Id|Identity|UID|Uid|Serial(?:Number)?))\b"
        r"\s*(?P<separator>[:=])\s*(?:"
        r'"(?P<double_value>(?:\\.|[^"\\])*)"'
        r"|'(?P<single_value>(?:\\.|[^'\\])*)'"
        r"|(?P<plain_value>[^\s,;}\]\"']+))"
    )

    def assignment_line(match: re.Match[str]) -> tuple[str, int]:
        """Return one real or JSON-escaped line and its source offset."""

        actual_start = text.rfind("\n", 0, match.start()) + 1
        escaped_start = text.rfind(r"\n", 0, match.start())
        line_start = max(actual_start, escaped_start + 2 if escaped_start >= 0 else 0)
        possible_ends = [
            position
            for position in (
                text.find("\n", match.end()),
                text.find(r"\n", match.end()),
            )
            if position >= 0
        ]
        line_end = min(possible_ends, default=len(text))
        return text[line_start:line_end].removesuffix("\r"), line_start

    for match in assignment.finditer(text):
        line, line_start = assignment_line(match)
        value = next(
            candidate
            for candidate in (
                match.group("double_value"),
                match.group("single_value"),
                match.group("plain_value"),
            )
            if candidate is not None
        ).strip()
        if (
            match.group("separator") == ":"
            and normalized_key(match.group("key")) == "board_id"
            and line == "- board_id: Stable logical board identifier."
        ):
            # Exact prose retained from the historical Markdown schema: this
            # describes the logical field rather than recording a hardware ID.
            continue
        safe_value = value.casefold() in SAFE_STRUCTURED_IDENTIFIERS
        remainder_has_identifier = (
            match.group("plain_value") is not None
            and RAW_STRUCTURED_IDENTIFIER.search(line[match.end() - line_start :]) is not None
        )
        if not safe_value or remainder_has_identifier:
            violations.append(f"non-redacted {match.group('key')}")
    return list(dict.fromkeys(violations))


def _contains_forbidden_fragment_sequence(text: str, forbidden_hashes: frozenset[str]) -> bool:
    fragments = [match.group() for match in IDENTIFIER_FRAGMENT.finditer(text)]
    # Literal-only source expressions can split a sensitive identifier across
    # several quoted fragments. Hash bounded adjacent concatenations as well as
    # individual tokens so punctuation and `+` do not defeat the known-ID gate.
    for start in range(len(fragments)):
        candidate = ""
        for fragment in fragments[start:]:
            candidate += fragment
            if len(candidate) > 40:
                break
            if len(candidate) < 6:
                continue
            digest = hashlib.sha256(candidate.casefold().encode()).hexdigest()
            if digest in forbidden_hashes:
                return True
    return False


def _source_comment_inspection_views(text: str) -> tuple[str, str]:
    """Return comment-body and comment-join views so neither can hide an ID."""

    def body(match: re.Match[str]) -> str:
        value = match.group()
        if value.startswith("/*"):
            return f" {value[2:-2]} "
        if value.startswith("//"):
            return f" {value[2:]} "
        return f" {value[1:]} "

    return SOURCE_COMMENT.sub(body, text), SOURCE_COMMENT.sub("", text)


def _contains_forbidden_identifier(text: str) -> bool:
    return any(
        _contains_forbidden_fragment_sequence(view, FORBIDDEN_IDENTIFIER_SHA256)
        for view in _source_comment_inspection_views(text)
    )


def _contains_forbidden_operator(text: str) -> bool:
    """Detect known private account components regardless of their parent path."""

    return any(
        _contains_forbidden_fragment_sequence(view, FORBIDDEN_OPERATOR_COMPONENT_SHA256)
        for view in _source_comment_inspection_views(text)
    )


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _privacy_text(path: Path, violations: list[str]) -> str | None:
    """Return canonical text, accepting only exact audited binary artifacts."""

    if "__pycache__" in path.parts:
        return None
    relative = _display_path(path)
    text = read_evidence_text(path)
    if text is None:
        expected = AUDITED_BINARY_SHA256.get(relative)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected == actual:
            return None
        violations.append(f"{relative}: undecodable or unaudited binary content")
        return None
    try:
        return canonical_evidence_text(text)
    except ValueError:
        violations.append(f"{relative}: encoding did not reach a fixed point")
        return None


def privacy_violations(additional_paths: tuple[Path, ...] = ()) -> list[str]:
    """Return deterministic, path-qualified violations across retained evidence."""

    violations: list[str] = []
    for evidence_root in EVIDENCE_ROOTS:
        for path in sorted(evidence_root.rglob("*")):
            if not path.is_file():
                continue
            text = _privacy_text(path, violations)
            if text is None:
                continue
            if any(pattern.search(text) for pattern in OPERATOR_PATH_PATTERNS):
                violations.append(f"{_display_path(path)}: operator home/workspace path")
            if _contains_forbidden_operator(text):
                violations.append(f"{_display_path(path)}: operator account identifier")
            for violation in structured_identifier_violations(path, text):
                violations.append(f"{_display_path(path)}: {violation}")

    for evidence_bundle in EVIDENCE_BUNDLES:
        for path in sorted(evidence_bundle.rglob("*")):
            if not path.is_file():
                continue
            text = _privacy_text(path, violations)
            if text is not None:
                if _contains_forbidden_identifier(text):
                    violations.append(f"{_display_path(path)}: physical probe ID")
                if _contains_forbidden_operator(text):
                    violations.append(f"{_display_path(path)}: operator account identifier")

    for path in additional_paths:
        display = _display_path(path)
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            violations.append(f"{display}: cannot inspect additional evidence: {exc}")
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            violations.append(f"{display}: additional evidence is not a regular file")
            continue
        text = _privacy_text(path, violations)
        if text is None:
            continue
        if any(pattern.search(text) for pattern in OPERATOR_PATH_PATTERNS):
            violations.append(f"{display}: operator home/workspace path")
        if _contains_forbidden_operator(text):
            violations.append(f"{display}: operator account identifier")
        if _contains_forbidden_identifier(text):
            violations.append(f"{display}: physical probe ID")
        for violation in structured_identifier_violations(path, text):
            violations.append(f"{display}: {violation}")
    return sorted(set(violations))


def _manifest_file_paths() -> list[Path]:
    files: list[Path] = []
    for evidence_root in EVIDENCE_ROOTS:
        for path in sorted(evidence_root.rglob("*")):
            if not path.is_file():
                continue
            if evidence_root == EVIDENCE_ROOTS[0] and (
                GENERATED_RELEASE_LOG.fullmatch(path.name)
                or path.name == "0.1.8rc1-attestation.json"
            ):
                continue
            files.append(path)
    return files


def expected_manifest() -> dict[str, object]:
    """Build the canonical manifest for retained sanitized derivative bytes."""

    files = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in _manifest_file_paths()
    ]
    return {
        "schema_version": 1,
        "scope": "privacy-redacted legacy evidence derivatives",
        "source_capture_policy": "private originals are intentionally not retained",
        "generated_release_logs": "scanned but not transformed or hash-manifested",
        "transformations": list(TRANSFORMATION_RULES),
        "file_count": len(files),
        "files": files,
    }


def write_manifest() -> None:
    """Atomically write the canonical output manifest."""

    content = json.dumps(expected_manifest(), indent=2, sort_keys=True) + "\n"
    temporary = MANIFEST_PATH.with_name(MANIFEST_PATH.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, MANIFEST_PATH)


def verify_manifest() -> list[str]:
    """Return manifest consistency errors without changing the working tree."""

    try:
        actual = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"missing manifest: {MANIFEST_PATH.relative_to(ROOT)}"]
    except json.JSONDecodeError as exc:
        return [f"invalid manifest JSON: {exc}"]
    expected = expected_manifest()
    if actual != expected:
        return ["sanitization manifest does not match retained derivative bytes"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="atomically replace the manifest after an intentional evidence update",
    )
    parser.add_argument(
        "--additional-evidence",
        action="append",
        default=[],
        type=Path,
        help="also scan this generated evidence file without adding it to the legacy manifest",
    )
    arguments = parser.parse_args(argv)
    violations = privacy_violations(tuple(arguments.additional_evidence))
    if violations:
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1
    if arguments.write_manifest:
        write_manifest()
    errors = verify_manifest()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(
        f"evidence privacy and manifest verification passed "
        f"({expected_manifest()['file_count']} derivative files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
