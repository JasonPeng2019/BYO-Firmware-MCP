"""Build-time feature profile.

Edit ``NARRATIVE_LOGGING`` when cutting a build. This is deliberately a plain
module constant rather than an environment variable, a config file, or a build
hook: nothing at runtime -- no operator, no agent, no misconfigured variable, and
no bug in a flag check -- can re-enable narrative in a build that was cut without
it. Once the code is compiled to a binary the constant is no longer editable.

Distribution distinguishes the two builds by filename convention; the server also
declares its own profile through the health check and every summary.
"""

from __future__ import annotations

# True  -> personal build: model-authored narrative is present.
# False -> professional build: no routine check-in tool, no narrative fields on the
#          issue-report form, and no conversation-level S-4..S-14 reports. The
#          server-only mechanical layer (ledger, trail, counters, S-1/S-2/S-3) is
#          unchanged, because it is code-content-free by construction.
NARRATIVE_LOGGING: bool = True


def profile_name() -> str:
    """Return the build's self-declared narrative capability."""

    return "enabled" if NARRATIVE_LOGGING else "not_built"


__all__ = ["NARRATIVE_LOGGING", "profile_name"]
