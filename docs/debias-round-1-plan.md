# De-bias round 1 implementation plan

> Historical: build-guidance portions are superseded by
> `docs/universal-native-build-command-plan.md`.

Status: implemented and verified

## Approach

Use data and existing typed interfaces rather than a provider framework. Moving reviewed boards to
one resource file is simpler than a plugin system. Explicit board facts are simpler and safer than
family inference. Generic-first serial resolution only reorders existing evidence and narrows the
vendor fallback. Generic build guidance reuses the already-live MCP artifact collector rather than
inventing build adapters.

Rejected simpler-looking alternatives:

- A new wrapper around the same pyOCD singleton would not add a working generic backend.
- Inferring addresses or recovery from target-name regexes only relocates MCU bias.
- Accepting arbitrary backend options or shell commands would be flexible but unsafe.
- Pretending HEX/BIN collection is guarded-flash readiness would bypass linker containment.

## Steps

1. Serialize reviewed catalog entries into a packaged JSON resource and replace `_CATALOG` literals
   with a strict generic loader plus duplicate/range/type validation.
2. Make safe-read evidence optional until enrichment, remove family-derived recovery/address
   defaults, add validated attach-mode/clock facts, and update validation to refuse missing evidence
   before connection.
3. Rename the automated recovery mechanism to `backend_mass_erase` throughout live plans, typed
   execution, profiles, examples, and tests; remove vendor/target substring checks. Add a backend
   capability check before disclosure and a warning-only legacy read alias for existing profiles.
4. Replace the one board-ID attach branch with the new profile facts. Keep pyOCD/J-Link workarounds
   only inside the labeled adapter fallback.
5. Make generic serial candidates win before vendor helpers. Move helper selection, executable,
   argv, parser, and preference into a validated server-owned fallback registry; remove MCU-family
   selection and fixed install-path tables and resolve binaries from PATH or explicit environment.
6. Make `get_setup_status` always return native-build plus visible collector guidance, and put the
   parameterized Zephyr command under `toolchain_fallback`. Remove nRF/STM board-prefix project
   update code from the Zephyr helper.
7. Update NULL-plan guidance, contracts, docs, and focused tests.

## Smoke test

One bounded software run will prove:

- catalog data loads through MCP setup routing with no board/part/address literals in
  `board_catalog.py`;
- an incomplete profile is refused before backend connection, while explicit address/attach facts
  reach a fake backend;
- generic serial identity resolves without invoking a vendor helper, and an ambiguous case invokes
  one parameterized helper fallback in the same test run;
- an MCP client reads provider-neutral build guidance, calls `collect_build_artifacts`, and also
  executes a bounded Zephyr fallback fixture in the same run, with both paths producing the same
  coherent artifact roles;
- `backend_mass_erase` follows the existing disclosure/one-time-permission path and no old
  MCU-named recovery mechanism remains in production code.

Focused pytest, Ruff, Pyright, package build, and stdio MCP smokes remain hardware-free.
The package smoke installs/imports the built wheel and loads both the catalog resource and every
referenced evidence resource.
