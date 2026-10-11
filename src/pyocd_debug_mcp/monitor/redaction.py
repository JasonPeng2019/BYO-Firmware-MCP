"""Two content bars, because the two logging layers have different audiences.

**Mechanical bar** -- trail, ledger, counters, and every server-detected report.
Universal in every build and the only thing a professional build emits. No
payloads, no full host paths, no raw hardware identifiers; arguments survive only
as salted fingerprints.

**Narrative bar** -- personal builds only. This is the opt-in codebase-describing
layer, so it *allows* real symbol names, file names, and description of the code:
that is the point of it, not a leak. It rejects verbatim payloads, credential
material, absolute host paths, and full command lines rather than persisting
those sensitive values as prose.

The fingerprint salt is a privacy secret, not an integrity one. Like a local
chain key it runs as the user and does not defend against the machine's own
owner; it defends against readers of the delivered reports, which is the correct
threat model because reports leave for the team's remote.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from hashlib import blake2b
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from unicodedata import category, normalize
from urllib.parse import unquote

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
        "board",
        "board_id",
        "host",
        "port",
        "selector",
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
_SHORT_PADDED_BASE64 = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{4}){2,}"
    r"(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)(?![A-Za-z0-9+/=])"
)
_ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|usr|opt|var)/)\S+")
_NARRATIVE_ABS_PATH = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9_.:/])[A-Za-z]:[\\/][^\s]+"
    r"|(?<![\\])\\\\[^\\/\s]+[\\/][^\s]+"
    r"|(?<![A-Za-z0-9_.:/\\])\\(?!\\)[^\s]+"
    r"|(?<![A-Za-z0-9_.:/])~[A-Za-z0-9._-]*/[^\s]+"
    r"|(?<![A-Za-z0-9_.:/])/+[^\s]+"
    r")"
)
_NON_WEB_URI = re.compile(r"\b(?!https?://)[A-Za-z][A-Za-z0-9+.-]*:(?://|/)[^\s]+")
_INTERNAL_IDENTIFIER = re.compile(
    r"\bprobeid:[A-Za-z0-9_.-]+:[A-Za-z0-9_.:-]+\b",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:passwords?|passwd|pwd|passcodes?|passphrases?|credentials?|"
    r"secret\s+codes?|secrets?|pins?|tokens?|"
    r"refresh\s+tokens?|github\s+pat|api\s+keys?|access\s+keys?|"
    r"secret\s+keys?|private\s+keys?|client\s+secrets?|"
    r"aws\s+secret\s+access\s+keys?|session\s+tokens?|oauth\s+tokens?|authorization|"
    r"auth(?:entication)?\s+(?:codes?|credentials?|tokens?|secrets?)|"
    r"login\s+(?:codes?|credentials?|tokens?|secrets?))\b"
    r"(?:['’]s\s+)?(?:hash|digest|value\s*)?\s*(?:"
    r"(?P<quoted>\"[^\"\r\n]+\"|“[^”\r\n]+”|'[^'\r\n]+'|‘[^’\r\n]+’)"
    r"|(?:(?:for|of|on|to)\s+(?:(?:the|this|that)\s+)?"
    r"[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){0,2}\s*)?"
    r"(?::|=|/|\\|\||~|\((?!\s*\))|\[(?!\s*\])|•|=>|->|→|‐|‑|‒|–|—|―|"
    r"\bis\b|\bwas\b|\bequals?\b|"
    r"\bset\s+to\b|"
    r"\bcontains?\b|\bhas\s+(?:a\s+)?value\b|"
    r"\bconsist(?:s|ed)?\s+of\b|"
    r"\bencoded\s+as\b)"
    r"\s*[\"'“‘]?"
    r"(?P<plain>[^\r\n,;}\]]+)"
    r")",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE = re.compile(
    r"(?:"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]+"
    r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"|[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bgithub[_-]pat_[A-Za-z0-9_]{20,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"
    r"|\brt_(?:live|prod|test)_[A-Za-z0-9_-]{8,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{20,}\b"
    r"|\bglpat-[A-Za-z0-9_-]{16,}\b"
    r"|\bAIza[0-9A-Za-z_-]{30,}\b"
    r"|\bnpm_[A-Za-z0-9]{20,}\b"
    r"|\bpypi-[A-Za-z0-9_-]{20,}\b"
    r"|\bya29\.[A-Za-z0-9_-]{20,}\b"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    r")",
    re.IGNORECASE,
)
_CONTEXTUAL_CREDENTIAL = re.compile(
    r"\b(?:passwords?|passwd|pwd|passcodes?|passphrases?|credentials?|secrets?|tokens?|"
    r"api\s+keys?|access\s+keys?|private\s+keys?|client\s+secrets?|pins?)\b"
    r"\s+(?:used|entered|provided|supplied)\b[^\r\n,;]{0,80}?"
    r"\b(?:is|was|equals?|consist(?:s|ed)?\s+of|encoded\s+as)\b\s*"
    r"[\"'“‘`]?(?P<plain>[^\r\n,;}\]]+)",
    re.IGNORECASE,
)
_CREDENTIAL_HASH_VALUE = re.compile(
    r"\b(?:passwords?|passwd|pwd|passcodes?|passphrases?|credentials?|secrets?|tokens?|"
    r"api\s+keys?|access\s+keys?|private\s+keys?|client\s+secrets?)\b"
    r"\s+(?:hash|digest|checksum|sha(?:-?\d+)?)\s*(?::|=|-|\bis\b|\bwas\b)?\s*"
    r"[`\"']?(?P<plain>[^\r\n,;}\]]+)",
    re.IGNORECASE,
)
_MEMORY_LITERAL = re.compile(
    r"\b(?:memory|ram)(?:\s+location)?\s+(?:at|from)?\s*0[xX][0-9A-Fa-f]+\b[^\r\n]*"
    r"\b(?:contained|held|returned|read|was|is)\b[^\r\n]*\b[0-9A-Fa-f]{4,}\b",
    re.IGNORECASE,
)
_ENCODED_BLOB = re.compile(
    r"\bencoded\s+(?:firmware|memory|artifact|payload|data)?\s*blob\b\s*"
    r"[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)
_PAYLOAD_RESPONSE = re.compile(
    r"\b(?:the\s+)?(?:uart|serial|console|board|device|peripheral|firmware|memory|artifact|"
    r"can(?:\s+bus)?|can\s+frame)\s+"
    r"(?:returned|said|gave|showed|printed|emitted|produced|replied|yielded|contained|"
    r"carried|delivered|reported)\b\s+"
    r"(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_PAYLOAD_LABEL = re.compile(
    r"\b(?:the\s+)?(?:"
    r"raw(?:\s+(?:bytes?|byte|data|text|output))?"
    r"|read\s*back(?:\s+(?:bytes?|data|contents?|memory|uart|serial|artifact|text)|(?=\s*[:=]))"
    r"|(?:a\s+)?can\s+(?:frame|packet|message)"
    r"|(?:uart|serial|console|board|device|peripheral|firmware|memory|ram|artifact|"
    r"can(?:\s+bus)?|can\s+frame)\s+"
    r"(?:rx|tx|bytes?|data|contents?|text|output|payload|dump|response|reply|"
    r"stream|transcript|capture|traffic|readout|log|snapshot)"
    r"|(?:observed|received|transmitted|sent|wrote|read)\s+(?:the\s+)?"
    r"(?:bytes?|data|payload|output|response|reply)"
    r"|(?:payload|dump|contents?)"
    r")\b\s*"
    r"(?:(?P<separator>was|were|is|are|returned|read|:|=)\s*)?"
    r"(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_PAYLOAD_FROM_SOURCE = re.compile(
    r"\b(?:received|observed|captured|read|got)\s+(?:data\s+)?from\s+(?:the\s+)?"
    r"(?:uart|serial|console|board|device|peripheral|firmware|can(?:\s+bus)?)\b"
    r"\s*(?::|=|was|were|is|are)?\s*(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_PAYLOAD_CAME_FROM = re.compile(
    r"\bfrom\s+(?:the\s+)?(?:uart|serial|console|board|device|peripheral|firmware|"
    r"can(?:\s+bus)?)\s+(?:came|arrived)\s+(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_PAYLOAD_SOURCE_PREFIX = re.compile(
    r"\b(?:on|from)\s+(?:the\s+)?(?:uart|serial|console|board|device|peripheral|"
    r"firmware|memory|ram|can(?:\s+bus)?)\s*:\s*(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_PAYLOAD_FROM_RESPONSE = re.compile(
    r"\b(?:the\s+)?(?:response|reply|value|data|output)\s+from\s+(?:the\s+)?"
    r"(?:uart|serial|console|board|device|peripheral|firmware|memory|ram|can(?:\s+bus)?)"
    r"\s+(?:was|is|contained|held|carried)\s+(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_PAYLOAD_SOURCE_VALUE = re.compile(
    r"\b(?:the\s+)?(?:uart|serial|console|board|device|peripheral|firmware|memory|ram|"
    r"can(?:\s+bus)?)\s+(?:observed|held|read\s+as|value\s+(?:was|is))\s*:?\s*"
    r"(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_CAN_FRAME_CARRIED = re.compile(
    r"\b(?:the\s+)?(?:frame|packet|message)\s+(?:on|from)\s+(?:the\s+)?can(?:\s+bus)?\s+"
    r"(?:carried|contained|held|was|is)\s+(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
_HARDWARE_IDENTIFIER_LABEL = re.compile(
    r"\b(?:probe\s+(?:s/n|sn|serial|id|uid|identity|identifier)|"
    r"connection\s+(?:id|uid|identity|identifier)|"
    r"board\s+(?:s/n|sn|serial|id|uid|identity|identifier)|"
    r"device\s+(?:s/n|sn|serial|id|uid|identity|identifier)|"
    r"mcu\s+(?:serial|uid|identity)|(?:st|j)[- ]?link\s+(?:s/n|serial|uid)|"
    r"hardware\s+identifier|identifier\s+of\s+(?:the\s+)?"
    r"(?:probe|board|device|connection|hardware))\b"
    r"\s*(?:(?:is|was|equals?|:|=|,|;|\.)\s*)?[\"'“‘`]?"
    r"(?P<value>[^\s,;.}\]]+)",
    re.IGNORECASE,
)
_RAW_HARDWARE_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_.:-])"
    r"(?:"
    r"(?=[A-Za-z0-9_.:-]{8,}(?![A-Za-z0-9_.:-]))(?=[A-Za-z0-9_.:-]*\d)"
    r"[A-Za-z0-9_.:-]+"
    r"|[A-Z][A-Z_.:-]{7,}"
    r")(?![A-Za-z0-9_.:-])"
)
_SAFE_PAYLOAD_DESCRIPTION = re.compile(
    r"^(?:"
    r"an?\s+(?:error|failure)"
    r"(?:\s+instead\s+of\s+an?\s+(?:successful\s+)?(?:acknowledgement|response|reply))?"
    r"|no\s+(?:data|bytes?|output|response|reply|content)"
    r"|nothing"
    r"|(?:truncated|empty|missing|absent|unavailable|suppressed|redacted|omitted|"
    r"malformed|invalid|unexpected|delayed|late|stale|corrupt(?:ed)?)"
    r"(?:\s+(?:before|after|during)\s+(?:the\s+)?[A-Za-z][A-Za-z -]{0,60})?"
    r"|timed\s+out"
    r"|did\s+not\s+(?:arrive|respond|reply|complete)"
    r"|never\s+(?:arrived|responded|replied|completed)"
    r")[.]?$",
    re.IGNORECASE,
)
_PAYLOAD_CODE_NOUN = re.compile(
    r"^(?:handler|handling|parser|path|buffer|logic|code|routine|function|task|driver|stage|"
    r"module|test|fixture|helper|support|integration)\b",
    re.IGNORECASE,
)
_SAFE_CODE_OBJECT = (
    r"(?:common\s+formats?|multiple\s+vendors|malformed\s+inputs?|invalid\s+inputs?|"
    r"stored\s+credentials|its\s+output|offline\s+workflows?|optional|supported\s+builds?|"
    r"retries|invalid\s+samples?|identifiers?|outcomes?|errors?|failures?|formats?|inputs?)"
)
_SAFE_CODE_PROSE_VALUE = re.compile(
    r"^(?:handler|handling|parser|path|buffer|logic|code|routine|function|task|driver|stage|"
    r"module|test|fixture|helper|support|integration)\s+"
    r"(?:accepts?|avoids?|clamps?|handles?|masks?|parses?|rejects?|supports?|validates?)\s+"
    + _SAFE_CODE_OBJECT
    + r"[.]?$",
    re.IGNORECASE,
)
_SAFE_REPORTED_DESCRIPTION = re.compile(
    r"^that\s+(?:the\s+)?(?:handler|parser|driver|firmware|device|operation|request|step)\s+"
    r"(?:failed|timed\s+out|was\s+(?:unavailable|invalid|rejected|delayed))[.]?$",
    re.IGNORECASE,
)
_SAFE_CREDENTIAL_OUTCOME = re.compile(
    r"^(?:expired(?:\s+and\s+authentication\s+needs\s+a\s+retry)?|missing|absent|invalid|"
    r"rejected|refused|unavailable|redacted|revoked|rotated|stale|unset|"
    r"hashed(?:\s+before\s+storage)?|encrypted|masked|not\s+"
    r"(?:available|configured|accepted|found|present|set|valid))[.]?$",
    re.IGNORECASE,
)
_COMMAND_MARKER = re.compile(
    r"\b(?:executed?\s+)?command(?:\s*:|\s+)\s*\S+(?:\s+\S+)+", re.IGNORECASE
)
_COMMAND_EXECUTABLE = re.compile(
    r"(?P<prefix>^\s*|\b(?:executed?|ran|run|invoked?|used)\s+)"
    r"(?P<quote>[`\"']?)"
    r"(?P<executable>make|cmake|ninja|python(?:3)?|pytest|uv|cargo|git|echo|west|"
    r"bash|sh|zsh|fish|dash|ksh|powershell|pwsh|cmd(?:\.exe)?|"
    r"docker|podman|pip(?:3)?|npm|npx|pnpm|yarn|meson|bazel|gradle|sudo|nohup|builder|"
    r"time|env|"
    r"openocd|pyocd|jlinkexe|[A-Za-z0-9_.+-]*(?:gcc|g\+\+|clang|objcopy|objdump|gdb|ld))"
    r"\b(?P<tail>[^\r\n]*)",
    re.IGNORECASE | re.MULTILINE,
)
_DIGITS = re.compile(r"\d+")
_HEXNUM = re.compile(r"0[xX][0-9A-Fa-f]+")
_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_BACKSLASH_ESCAPE = re.compile(
    r"\\(?:u(?P<u>[0-9A-Fa-f]{4})|U(?P<U>[0-9A-Fa-f]{8})|x(?P<x>[0-9A-Fa-f]{2}))"
)
_COMMAND_OPTION_LINE = re.compile(
    r"^\s*[`\"']?(?P<executable>[A-Za-z0-9_./+:-]+)\b(?P<tail>[^\r\n]*"
    r"(?:^|\s)--?[A-Za-z][A-Za-z0-9_-]*(?:[=\s]|$)[^\r\n]*)",
    re.IGNORECASE,
)
_BARE_FILE_COMMAND = re.compile(
    r"^\s*[`\"']?[a-z][a-z0-9_.+-]*[`\"']?\s+"
    r"(?=[^\r\n]*(?:\b[A-Za-z0-9_.+-]+\."
    r"(?:a|bin|elf|hex|map|o|obj|so|py|sh|yaml|yml|json|log)\b|(?:^|\s)--?\w))"
    r"\S+(?:\s+\S+)*\s*$"
)
_BARE_PUNCTUATED_COMMAND = re.compile(
    r"^\s*[`\"']?[a-z][a-z0-9]*[-_./+][a-z0-9_.+/-]*[`\"']?"
    r"\s+\S+(?:\s+\S+)*\s*$",
    re.IGNORECASE,
)
_CUSTOM_EXECUTABLE_COMMAND = re.compile(
    r"^\s*[a-z][A-Za-z0-9_.+/-]*(?:builder|tool|cli|cmd|runner)\s+\S+(?:\s+\S+)*\s*$"
)
_SHELL_PROMPT_COMMAND = re.compile(r"^\s*(?:[$>]\s+|\./)[A-Za-z0-9_.+/-]+\s+\S+(?:\s+\S+)*\s*$")
_ENVIRONMENT_COMMAND = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)+"
    r"(?:make|cmake|ninja|python(?:3)?|pytest|uv|cargo|git|docker|podman|builder)\b"
    r"[^\r\n]*$",
    re.IGNORECASE,
)
_SAFE_COMMAND_PROSE = re.compile(
    r"^\s*(?:"
    r"(?:git|cargo|make)\s+(?:integration|support|targets?)\s+"
    r"(?:supports?|remains?|describes?)\s+"
    + _SAFE_CODE_OBJECT
    + r"|[A-Za-z0-9_./+:-]*[-_./+][A-Za-z0-9_./+:-]+\s+"
    r"(?:handler|handling|parser|path|buffer|logic|code|routine|function|task|driver|stage|"
    r"module|test|fixture|helper|support|integration)\s+"
    r"(?:accepts?|avoids?|clamps?|handles?|masks?|parses?|rejects?|supports?|validates?)\s+"
    + _SAFE_CODE_OBJECT
    + r"|[A-Za-z0-9_./+:-]*[-_./+][A-Za-z0-9_./+:-]+\s+"
    r"(?:accepts?|avoids?|clamps?|handles?|masks?|parses?|rejects?|supports?|validates?)\s+"
    + _SAFE_CODE_OBJECT
    + r")\.\s*$",
    re.IGNORECASE,
)
_EMBEDDED_COMMAND = re.compile(
    r"^\s*(?:next\s*,\s*|please\s+(?:run|execute|invoke)\s*:?\s+)"
    r"(?:make|cmake|ninja|python(?:3)?|pytest|uv|cargo|git|bash|sh|docker|podman|"
    r"builder|[A-Za-z0-9_.+-]*(?:gcc|clang|gdb))\b\s+\S+",
    re.IGNORECASE,
)
_MARKUP_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
_MARKDOWN_LINK = re.compile(r"\[([^\]\r\n]+)\]\(([^)\r\n]*)\)")
_PAREN_WRAPPER = re.compile(r"\(([^()\r\n]+)\)")
_MARKDOWN_DELIMITER = re.compile(
    r"`+|\*{1,3}|~~|[\[\]]|(?<![A-Za-z0-9])_{1,3}|_{1,3}(?![A-Za-z0-9])"
)
_NARRATIVE_LINE_START = frozenset(
    {
        "a",
        "an",
        "documented",
        "firmware",
        "fixed",
        "implemented",
        "parser",
        "refactored",
        "the",
        "updated",
        "verified",
    }
)

_SELF_RATING = re.compile(
    r"\b(?:i (?:did|performed|handled)\s+(?:well|poorly|badly|great)"
    r"|my performance|self-assess|i(?:'m| am) (?:confident|proud|ashamed)"
    r"|(?:rate|grade|score) (?:myself|my own))\b",
    re.IGNORECASE,
)
_LETTER_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)

# Inspection-only Unicode skeleton for common Greek/Cyrillic homoglyphs. This
# is deliberately paired with NFKC rather than used to rewrite accepted prose.
# It makes security/source words such as pаssword and mаke visible to the
# ordinary ASCII rules without banning legitimate non-Latin narrative text.
_CONFUSABLE_ASCII = str.maketrans(
    {
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Υ": "Y",
        "Χ": "X",
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "y",
        "χ": "x",
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "і": "i",
        "ј": "j",
    }
)


class NarrativeContentError(ValueError):
    """Model-authored prose carried sensitive raw material instead of a description."""


def _inspection_view(text: str, *, field: str) -> str:
    """Build bounded inspection views of encoded and markup-wrapped model text."""

    def decode_backslash(match: re.Match[str]) -> str:
        encoded = match.group("u") or match.group("U") or match.group("x")
        assert encoded is not None
        codepoint = int(encoded, 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            return match.group(0)
        return chr(codepoint)

    inspection_text = text
    for _ in range(32):
        decoded = html.unescape(unquote(inspection_text))
        decoded = _BACKSLASH_ESCAPE.sub(decode_backslash, decoded)
        canonical = (
            normalize("NFKC", decoded)
            .translate(
                str.maketrans(
                    {
                        "\u2044": "/",  # fraction slash
                        "\u2215": "/",  # division slash
                        "\u2571": "/",  # box-drawing diagonal
                        "\u29f8": "/",  # big solidus
                        "\u2216": "\\",  # set minus
                        "\u2572": "\\",  # box-drawing reverse diagonal
                        "\u29f5": "\\",  # reverse solidus operator
                        "\u29f9": "\\",  # big reverse solidus
                        "\u02d0": ":",  # modifier-letter triangular colon
                        "\u2236": ":",  # ratio
                        "\u2982": ":",  # Z-notation type colon
                        "\ua789": ":",  # modifier-letter colon
                    }
                )
            )
            .translate(_CONFUSABLE_ASCII)
        )
        # Format controls, variation selectors, and other combining marks can be
        # inserted into sensitive keywords without changing their rendered form.
        # This is an inspection-only view; the accepted narrative is not rewritten.
        canonical = "".join(
            character
            for character in canonical
            if category(character) != "Cf" and not category(character).startswith("M")
        )
        if canonical == inspection_text:
            break
        inspection_text = canonical
    else:
        raise NarrativeContentError(
            f"{field}: encoded content did not normalize; describe it without raw escapes."
        )

    # Markup is expanded only after canonicalization. Each transformation runs
    # once and grows the inspection string by a fixed factor; no expanded view
    # is fed back through a duplicating loop. The input schemas cap one field at
    # 4,000 characters, and this defensive ceiling also protects direct callers.
    decoded = inspection_text
    if _MARKUP_COMMENT.search(decoded) is not None:
        body_view = _MARKUP_COMMENT.sub(lambda match: f" {match.group(1)} ", decoded)
        comment_bodies = "\n".join(match.group(1) for match in _MARKUP_COMMENT.finditer(decoded))
        joined_view = _MARKUP_COMMENT.sub("", decoded)
        decoded = f"{body_view}\n{comment_bodies}\n{joined_view}"
    if _MARKDOWN_LINK.search(decoded) is not None:
        label_view = _MARKDOWN_LINK.sub(lambda match: match.group(1), decoded)
        link_parts = "\n".join(
            f"{match.group(1)}\n{match.group(2)}" for match in _MARKDOWN_LINK.finditer(decoded)
        )
        decoded = f"{label_view}\n{link_parts}"
    if _PAREN_WRAPPER.search(decoded) is not None:
        # Translate all wrapper layers in one linear pass. Empty function-call
        # parentheses do not trigger this branch, preserving ordinary symbols.
        spaced_view = decoded.replace("(", " : ").replace(")", " ")
        joined_view = decoded.replace("(", "").replace(")", "")
        decoded = f"{spaced_view}\n{joined_view}"
    if _MARKDOWN_DELIMITER.search(decoded) is not None:
        spaced_view = _MARKDOWN_DELIMITER.sub(
            lambda match: " : " if match.group(0) in {"[", "]"} else " ",
            decoded,
        )
        joined_view = _MARKDOWN_DELIMITER.sub("", decoded)
        decoded = f"{spaced_view}\n{joined_view}"
    if len(decoded) > 256_000:
        raise NarrativeContentError(
            f"{field}: markup expansion is too large; describe the observation as plain text."
        )
    return decoded


def _has_credential_assignment(text: str) -> bool:
    for match in _CREDENTIAL_HASH_VALUE.finditer(text):
        value = match.group("plain").strip(" \t\"'“”‘’`.")
        if _SAFE_CODE_PROSE_VALUE.fullmatch(value) is not None:
            continue
        return True
    for match in (*_CREDENTIAL_ASSIGNMENT.finditer(text), *_CONTEXTUAL_CREDENTIAL.finditer(text)):
        groups = match.groupdict()
        if groups.get("quoted") is not None:
            return True
        value = (groups.get("plain") or "").strip(" \t\"'“”‘’`.")
        if value and _SAFE_CREDENTIAL_OUTCOME.fullmatch(value) is None:
            return True
    return False


def _has_payload_literal(text: str) -> bool:
    if _MEMORY_LITERAL.search(text) or _ENCODED_BLOB.search(text):
        return True
    for match in (
        *_PAYLOAD_RESPONSE.finditer(text),
        *_PAYLOAD_FROM_SOURCE.finditer(text),
        *_PAYLOAD_CAME_FROM.finditer(text),
        *_PAYLOAD_SOURCE_PREFIX.finditer(text),
        *_PAYLOAD_FROM_RESPONSE.finditer(text),
        *_PAYLOAD_SOURCE_VALUE.finditer(text),
    ):
        value = match.group("value").strip(" \t\"'“”‘’`.")
        if (
            _SAFE_PAYLOAD_DESCRIPTION.fullmatch(value) is None
            and _SAFE_REPORTED_DESCRIPTION.fullmatch(value) is None
        ):
            return True
    for match in _CAN_FRAME_CARRIED.finditer(text):
        value = match.group("value").strip(" \t\"'“”‘’`.")
        if _SAFE_PAYLOAD_DESCRIPTION.fullmatch(value) is None:
            return True
    for match in _PAYLOAD_LABEL.finditer(text):
        value = match.group("value").strip(" \t\"'“”‘’`.")
        if _SAFE_PAYLOAD_DESCRIPTION.fullmatch(value) is not None:
            continue
        if match.group("separator") is None and _SAFE_CODE_PROSE_VALUE.fullmatch(value) is not None:
            continue
        return True
    return False


def _has_full_command(line: str) -> bool:
    if _SAFE_COMMAND_PROSE.fullmatch(line):
        return False
    if _EMBEDDED_COMMAND.search(line):
        return True
    if _COMMAND_MARKER.search(line):
        return True
    if _BARE_FILE_COMMAND.fullmatch(line):
        return True
    if _ENVIRONMENT_COMMAND.fullmatch(line):
        return True
    if _CUSTOM_EXECUTABLE_COMMAND.fullmatch(line):
        return True
    if _SHELL_PROMPT_COMMAND.fullmatch(line):
        return True
    if _BARE_PUNCTUATED_COMMAND.fullmatch(line):
        return True
    generic = re.match(
        r"^\s*(?:executed?|ran|invoked?|used)\s+[`\"']?"
        r"(?P<executable>[A-Za-z0-9_.+/-]+)\b\s+\S+",
        line,
        re.IGNORECASE,
    )
    if (
        generic is not None
        and generic.group("executable").casefold() not in _NARRATIVE_LINE_START
        and re.match(
            r"(?i)^\s*(?:"
            r"ran\s+(?:unit|integration|system|acceptance|regression|smoke)\s+tests?\s+"
            r"(?:for|against|on)"
            r"|(?:ran|used)\s+cmake\s+to\s+rebuild"
            r")\b",
            line,
        )
        is None
    ):
        return True
    shaped = _COMMAND_OPTION_LINE.search(line)
    if shaped is not None:
        executable = shaped.group("executable").strip("./").casefold()
        if executable not in _NARRATIVE_LINE_START:
            return True
    for match in _COMMAND_EXECUTABLE.finditer(line):
        prefix = match.group("prefix")
        executable = match.group("executable").casefold()
        tail = match.group("tail").lstrip(" \t`\"'")
        if prefix:
            if re.match(r"(?i)^to\s+(?:build|rebuild|check|test|inspect|run)\b", tail):
                continue
            return True
        if not tail:
            continue
        if executable.startswith("python") and not re.match(r"(?:-|[^\s]+\.py\b)", tail):
            continue
        if executable in {"cmake", "pyocd", "openocd"} and re.match(
            r"(?i)^(?:code|driver|module|server|integration|support|wrapper)\b", tail
        ):
            continue
        return True
    return False


def _salted(text: str, size: int = _FINGERPRINT_BYTES) -> str:
    return blake2b(
        deployment_salt() + text.encode("utf-8", "replace"), digest_size=size
    ).hexdigest()


def fingerprint(value: object) -> str:
    """Return a stable, private runtime fingerprint of a canonical argument set.

    Salting is what closes the reconstruction channel: an unsalted hash of a small
    or guessable input -- a register address, a short enum, a filename from a known
    set -- can be brute-forced back to its real value. Runtime stability is needed
    for loop detection; report persistence adds a separate report-scoped context.
    """

    from pyocd_debug_mcp.guardrails.plan_engine import canonical_json

    try:
        text = canonical_json(_jsonable(value))
    except Exception:  # noqa: BLE001 - a fingerprint must never break a tool call
        text = repr(value)
    return _salted(text)


def report_fingerprint(stable_fingerprint: str, report_id: str) -> str:
    """Re-key one runtime fingerprint so it cannot correlate separate reports."""

    return _salted(f"report:{report_id}:{stable_fingerprint}")


def digest_id(value: str, keep: int = 4) -> str:
    """Return a truncated salted digest for a hardware or host identifier."""

    return _salted(value, size=max(2, keep))


def safe_path(value: str) -> str:
    """Return a path reduced to its basename plus a salted digest of the whole."""

    try:
        # `Path` follows only the current host's path grammar.  Reports can
        # contain paths produced on another host, so select a pure grammar from
        # the input separators instead of accidentally retaining a Windows path
        # verbatim on POSIX (or vice versa).
        path_type = PureWindowsPath if "\\" in value else PurePosixPath
        name = path_type(value).name or "path"
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

    Real symbol names, repository-relative file names, and codebase description
    are allowed and are the purpose of this layer. Raw payloads, credentials,
    absolute host paths, and full command lines are rejected.
    """

    inspection_text = _inspection_view(text, field=field)

    if any(
        category(character) in {"Cc", "Cs"} and character not in "\r\n\t"
        for character in inspection_text
    ):
        raise NarrativeContentError(
            f"{field}: remove control characters; describe the observation as plain text."
        )
    if any(
        any(character.isascii() and character.isalpha() for character in token)
        and any(not character.isascii() and character.isalpha() for character in token)
        for token in _LETTER_TOKEN.findall(inspection_text)
    ):
        raise NarrativeContentError(
            f"{field}: mixed-script words are ambiguous; use one writing system per word."
        )

    if _HEX_RUN.search(inspection_text):
        raise NarrativeContentError(
            f"{field}: remove the raw byte dump; describe what was read instead."
        )
    if _BASE64_RUN.search(inspection_text) or _SHORT_PADDED_BASE64.search(inspection_text):
        raise NarrativeContentError(
            f"{field}: remove the encoded blob; describe what it was instead."
        )
    if _INTERNAL_IDENTIFIER.search(inspection_text):
        raise NarrativeContentError(
            f"{field}: remove the raw internal identifier; describe the device role instead."
        )
    identifier_words = re.sub(
        r"(?i)\b(connection|probe|board|device|mcu)[_-](?=(?:id|uid|identity|serial)\b)",
        r"\1 ",
        inspection_text,
    )
    for match in _HARDWARE_IDENTIFIER_LABEL.finditer(identifier_words):
        value = match.group("value").strip(" \t\"'“”‘’`.")
        # A safe outcome can be used as a decoy prefix before a raw identifier,
        # for example ``Board ID unavailable. CUSTOMER-SECRET-1234``. Inspect the
        # rest of the same line rather than treating the first safe token as the
        # end of the hardware-identity statement.
        line_end = identifier_words.find("\n", match.end())
        remainder = identifier_words[match.end() : line_end if line_end >= 0 else None]
        if _RAW_HARDWARE_IDENTIFIER.search(remainder) is not None:
            raise NarrativeContentError(
                f"{field}: remove the raw hardware identifier; describe the device role instead."
            )
        if _SAFE_CODE_PROSE_VALUE.fullmatch(f"{value} {remainder}".strip()) is not None:
            continue
        if value and _SAFE_PAYLOAD_DESCRIPTION.fullmatch(value) is None:
            raise NarrativeContentError(
                f"{field}: remove the raw hardware identifier; describe the device role instead."
            )
    if _has_payload_literal(inspection_text):
        raise NarrativeContentError(
            f"{field}: remove the raw payload; describe what was observed instead."
        )
    credential_words = re.sub(
        r"[_-]+",
        " ",
        re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", inspection_text),
    )
    if _has_credential_assignment(credential_words) or _CREDENTIAL_VALUE.search(inspection_text):
        raise NarrativeContentError(
            f"{field}: remove credential material; describe the authentication outcome instead."
        )
    if _NON_WEB_URI.search(inspection_text) or _NARRATIVE_ABS_PATH.search(inspection_text):
        raise NarrativeContentError(
            f"{field}: remove the absolute host path; use a repository-relative name instead."
        )
    for line in inspection_text.splitlines():
        if _has_full_command(line):
            raise NarrativeContentError(
                f"{field}: remove the full command line; name the step instead."
            )


def check_no_self_rating(text: str, *, field: str = "effectiveness_observed") -> None:
    """Reject self-assessment in a check-in's outcomes section.

    Observable outcomes only: what was accomplished, what it got stuck on, where it
    needed retries. Self-rating is prohibited outright, so this rejects rather than
    warns.
    """

    if _SELF_RATING.search(_inspection_view(text, field=field)):
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
