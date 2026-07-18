# De-bias round 4 implementation plan

## Approach

Replace the one vendor register-name literal with the neutral descriptive label `silicon_id`. This
is smaller and clearer than adding a new catalog field whose value has no effect on identity or
safety. Target-specific address/value evidence remains in reviewed data.

## Steps

1. Change fresh-profile commit to use the neutral label when a reviewed silicon-ID address exists.
2. Extend the existing live-connect-before-commit test to inspect the resulting profile and prove
   its identity evidence remains exact while its label is target-neutral.
3. Through an in-process `ClientSession`, call the live `board_setup-plan` all-NULL, load it, submit
   a valid plan, and call the dynamically exposed `board_setup`. Use a temporary FirmStore plus fake
   inventory/backend and assert the profile committed by that MCP call.
4. Run focused setup/MCP tests, Ruff, and Pyright.

## Smoke test

Use an in-process MCP client and the live server tool registry to call `board_setup-plan` first with
all NULL fields, then with a valid permitted plan, then the newly exposed `board_setup`. Fake only
physical inventory/backend work and direct reports/profiles to a temporary FirmStore. Assert the
schema-v2 profile written by that MCP call contains `silicon_id_label: silicon_id` and the unchanged
reviewed address/value/mask. Run `tests/test_server_resource_binding.py` and the in-process MCP
setup-tool tests.
