"""Direct, build-system-neutral firmware build MCP tool."""

from __future__ import annotations

import json
from collections.abc import Callable

from firmware_mcp.native_build import build_firmware as run_build_firmware


def build_build_handlers() -> dict[str, Callable[..., str]]:
    """Build the direct argv firmware-build handler."""

    def build_firmware(
        project_dir: str,
        build_dir: str,
        command: list[str],
        working_dir: str | None = None,
        environment: dict[str, str] | None = None,
        artifacts: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """**What** Run one caller-supplied firmware build argv without a shell.

        **When** Use after setup to build a project with its own installed toolchain; for example,
        `command=["cmake", "--build", "build"]`.

        **Parameters** `project_dir` and `build_dir` are real directories; `command` is a nonempty
        exact argv list; commands receive closed stdin and obtain any required input only from argv,
        cwd, and `environment` (never the MCP protocol stream); optional `working_dir` defaults to
        `project_dir`; optional `environment` maps string keys to string values; optional `artifacts` maps caller roles to output paths;
        optional `timeout_seconds` is an unclamped duration in seconds (for example `600.0`);
        omitted means the build has no server-invented deadline.

        **Returns** Exact argv, cwd, environment-key overrides, exit code, stdout, stderr,
        duration, timeout outcome, and sorted artifact evidence. A zero-exit build with no outputs
        reports `artifacts=[]` rather than inventing firmware.

        **Failures and recovery** Invalid directories or argv fail before execution; a nonzero exit
        returns structured failure evidence—inspect stderr, correct the project command, then call
        `build_firmware` again. Use `collect_build_artifacts` for existing outputs.
        """

        result = run_build_firmware(
            project_dir=project_dir,
            build_dir=build_dir,
            command=command,
            working_dir=working_dir,
            environment=environment,
            artifacts=artifacts,
            timeout_seconds=timeout_seconds,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    return {"build_firmware": build_firmware}
