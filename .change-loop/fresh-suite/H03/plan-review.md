# H03 plan review

Plan SHA-256: e1e8ecdc0dd5e59c5afaeaf84f681e1f8df014b59d8b044bab02a5f5c7621e10

- Reviewed plan SHA-256: `e1e8ecdc0dd5e59c5afaeaf84f681e1f8df014b59d8b044bab02a5f5c7621e10`
- Codex thread/session ID: `019f99e7-44b9-78b2-91e3-a13ee5d9abcc`
- Persistence note: the read-only reviewer returned this review verbatim but its read-only sandbox
  rejected the requested file write; the main orchestrator persisted the returned content and
  controller-extracted identity without changing the verdict or review points.

## Execution risks and tester targets

1. Exact-byte write: use byte-oriented UTF-8 serialization plus `b"\n"`; test full byte equality,
   final `0a`, no `0d`, no BOM.
2. Preserve schema/provenance: test parsed values, role ordering, copied bytes, sizes, hashes, and
   existing result behavior.
3. Refusal wording: a pre-existing nonempty destination must remain unchanged; test missing-role
   creates no destination/stage, and nonempty refusal creates no stage residue.
4. Preserve staging and atomic replace: retain success and refusal regression coverage.

## Charter second check

The reviewer reread `.change-loop/design_charter.md` immediately before its verdict. The plan
remains aligned: it fixes cross-host correctness in the owning module without OS branches or extra
abstraction, preserves existing refusals, and has no hardware scope.

PROCEED
