# Generic Make Native-Build Provider Plan

1. Add Makefile detection and provider-specific local-environment discovery behind the existing
   helper interface.
2. Add bounded cross-platform executable discovery with explicit-variable and PATH precedence.
3. Build into the caller's fresh build root and generically resolve one ELF/map pair.
4. Add focused tests for provider selection, argv/environment construction, ambiguity, missing
   tools/artifacts, and unchanged Zephyr behavior.
5. Run focused tests, Ruff, Pyright, full pytest, package/import, and hostile diff review.
