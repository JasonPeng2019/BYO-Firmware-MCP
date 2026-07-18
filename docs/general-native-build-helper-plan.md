# General Native Build Helper Plan

1. Implement a small provider-neutral CLI with strict path validation and local-only NCS discovery.
2. Implement a Zephyr project detector/provider that runs one ordinary local `west build` through
   the discovered NCS environment without network or bootstrap behavior.
3. Return the exact parameterized general-helper argv/command in setup status.
4. Add unit tests for discovery, command construction, path rejection, no-download behavior, and
   setup-status guidance; update package/contract documentation.
5. Run focused tests, Ruff, Pyright, full pytest, package/import, and an adversarial diff review.

