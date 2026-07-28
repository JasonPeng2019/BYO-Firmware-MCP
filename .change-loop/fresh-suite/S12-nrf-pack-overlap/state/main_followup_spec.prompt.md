Authorized local firmware validation. This is a host-only adversarial server test in the named
BYO-Firmware-MCP workspace. No hardware action is authorized or needed.

You are the same persistent change-loop spec tester. Main-model evidence review found a concrete
coverage gap despite the green neutral gate: `tests/test_s12_nrf_pack_overlap_spec.py` currently
tests `_canonical_physical_regions` directly, but CL-002/CL-003 require an integration-level test
through the real `resolve_registered_pack_geometry` adapter using verified, offline,
in-memory CMSIS-Pack/PDSC bytes.

Read the complete plan, plan review, latest neutral report, current source/diff, and full
`../.codex/design_charter.md`. Edit only your manifested S12 spec test. Add a deterministic offline
fixture/test that:

1. supplies a valid minimal CMSIS-Pack/PDSC leaf through the production verified-pack replay
   boundary used by `resolve_registered_pack_geometry`;
2. describes Nordic-shaped physical flash rows `[0, 0x100000)` marked
   default/boot/testable and `[0, 0x200000)` unmarked, plus valid writable RAM;
3. calls the actual `resolve_registered_pack_geometry` production function rather than calling
   `_canonical_physical_regions` as the system under test;
4. proves the returned physical flash collection contains only `[0, 0x100000)`, preserves the
   scalar default flash/RAM values, and does not fabricate `[0x100000, 0x200000)`;
5. feeds those returned collections into the unchanged `GenericMapGeometry` boundary and proves
   construction succeeds without widening erase/write authority.

Use existing fixture mechanisms when possible; do not use the network, live boards, external SDKs,
or the S12 experiment directory. Keep existing adversarial unit cases. Update only your own
manifest and portable forward-slash command as required. Run the exact recorded command. Append
all required dated design-charter checkpoints to the runtime log before analysis, immediately
before editing, between distinct test features, before verification, and before the verdict. Do
not edit production source or commit.
