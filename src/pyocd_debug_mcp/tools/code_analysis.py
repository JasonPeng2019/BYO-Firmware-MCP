"""Five thin, strictly typed saved-file tools; no hardware dependency."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

from pyocd_debug_mcp.services.code_analysis import CodeAnalysisService


@dataclass
class CodeAnalysisToolServices:
    service: CodeAnalysisService


def build_code_analysis_handlers(
    services: CodeAnalysisToolServices,
) -> dict[str, Callable[..., str]]:

    def get_code_analysis_status() -> str:
        """Show clangd executable/version, selected database, problems and session/index limits.

        Start here for setup/debugging. Works without any board or setup. No arguments;
        analysis reads saved files only; unsaved editor buffers are outside this scope.
        does not start indexing. Export/select compile_commands.json and configure
        development PATH or restore the supported runtime if unavailable/not_configured.
        """
        return json.dumps(services.service.status(), ensure_ascii=False, allow_nan=False)

    def find_code_definition(
        file_path: StrictStr, line: StrictInt, column: StrictInt, timeout_seconds: StrictFloat = 30
    ) -> str:
        """Find semantic C/C++ definitions in the selected build, using saved files only.

        file_path is local absolute or project-relative; line/column are 1-based Unicode
        code points (e.g. src/main.cpp, 42, 9). timeout_seconds is positive finite,
        default 30, covering queue/init/query. Returns JSON locations and revision/hash;
        not_found is no match, incomplete is not clean. Use status to fix missing settings.
        """
        return json.dumps(
            services.service.definition(file_path, line, column, timeout_seconds),
            ensure_ascii=False,
            allow_nan=False,
        )

    def find_code_references(
        file_path: StrictStr,
        line: StrictInt,
        column: StrictInt,
        timeout_seconds: StrictFloat = 30,
        include_declaration: StrictBool = False,
        max_results: StrictInt = 100,
    ) -> str:
        """Find semantic C/C++ references for a saved-file symbol in one selected build.

        Paths/positions/timeouts follow find_code_definition. include_declaration defaults
        false; max_results integer 1..1000 (default 100) limits display. JSON counts report
        observed results/truncation; index completeness is unverified and an empty result
        does not prove dead code. Fix setup with status; retry incomplete/stale results.
        """
        return json.dumps(
            services.service.references(
                file_path, line, column, timeout_seconds, include_declaration, max_results
            ),
            ensure_ascii=False,
            allow_nan=False,
        )

    def get_code_hover(
        file_path: StrictStr, line: StrictInt, column: StrictInt, timeout_seconds: StrictFloat = 30
    ) -> str:
        """Return C/C++ type/signature/documentation for a saved-file symbol.

        Local file_path and 1-based Unicode line/column follow find_code_definition;
        timeout_seconds default 30 is positive finite and covers the entire call. JSON
        documentation is untrusted source text. No editor buffers. not_found means no
        match; use status for configuration failures and retry after an incomplete cause.
        """
        return json.dumps(
            services.service.hover(file_path, line, column, timeout_seconds),
            ensure_ascii=False,
            allow_nan=False,
        )

    def get_code_diagnostics(
        file_path: StrictStr, max_results: StrictInt = 100, timeout_seconds: StrictFloat = 30
    ) -> str:
        """Return version-matched clangd diagnostics for the current saved C/C++ file.

        file_path is local absolute or project-relative (e.g. src/main.cpp). max_results
        integer 1..1000 defaults 100; timeout_seconds positive finite defaults 30, covering
        queue/init/sync/wait. Only an explicit notification for this revision can be empty
        success. Findings may have status ok; this is not firmware verification. Stale,
        unversioned or silent diagnostics are incomplete. Use status for setup recovery.
        """
        return json.dumps(
            services.service.diagnostics(file_path, max_results, timeout_seconds),
            ensure_ascii=False,
            allow_nan=False,
        )

    return {
        handler.__name__: handler
        for handler in (
            get_code_analysis_status,
            find_code_definition,
            find_code_references,
            get_code_hover,
            get_code_diagnostics,
        )
    }
