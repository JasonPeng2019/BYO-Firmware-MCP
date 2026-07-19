# Universal Device Onboarding Implementation Plan

Specification: `docs/universal-device-onboarding-spec.md`

Authority decision: `decisions/ADR-0003-quarantined-runtime-device-support-onboarding.md`

## Slice 1 - Generic support discovery and promotion

1. Replace the admin-only runtime assumption with ADR-0003's quarantined setup transaction. The
   agent supplies candidate bytes/source metadata; none is authority until deterministic validation.
2. Separate immutable repository registry lookup from project-scoped, server-validated support.
3. Extend pack validation to prove the exact requested PDSC leaf and canonical target before
   promotion, and persist that binding in the promoted manifest.
4. Make profile replay use its owning project store rather than only the repository registry.
5. Bind quarantine live attach to the already-computed digest, bound raw archive memory use, and
   retain only promoted immutable pack objects in the process-global cache.
6. Keep dynamic project support under `.firm/packs`, never the checkout registry, and serialize the
   manifest read/merge/write while rebinding the validated payload immediately before publication.
7. Add synthetic multi-vendor pack tests, tamper tests, concurrent-promotion tests, and restart
   replay tests.

The project manifest uses the existing strict `PackSpec` schema plus exactly one generated
`DeviceBinding(part_number, pdsc_device, pyocd_target, identity_proof?)` for the selected leaf.
`PackCandidatePipeline.validate` returns that leaf/binding and `promote` writes it. Add
`resolve_project_support(store, part)` and `resolve_project_geometry(store, candidate)`; both use
the store's manifest/files and exact digest replay. `ProfileRepository` constructs its default
device-support verifier from its own `FirmStore`. Repository-registry resolution remains a fast
first source; project support is the second source, and conflicting exact candidates refuse.

## Slice 2 - Setup research routing

1. Treat no local exact candidate as `setup_research_required`, not invalid datasheet evidence.
2. Remove the reviewed-board prerequisite from target/pack continuation validation.
3. Accept only official-source pack candidate fields; derive target and PDSC leaf server-side.
4. Quarantine the candidate, derive its binding, and perform the bounded non-destructive live attach
   before promotion. Promote atomically only after attach succeeds; failed attach removes newly
   staged bytes and leaves no manifest/evidence record. Resume setup using the promoted candidate.
5. Replace raw candidate strings with run-scoped candidate IDs after validation; URLs, paths, and
   checksums are never accepted by normal connect/validate/refresh calls.

## Slice 3 - Generic evidence, identity, and maps

1. Add a typed bounded datasheet-capture record containing PDF/parser/reference evidence; persist
   only its canonical digest in the map.
2. Derive conservative physical geometry from the exact support leaf and typed datasheet evidence.
3. Persist schema-v3 source digests and make ProfileRepository, SafetyMapRepository, refresh, and
   the runtime authority verifier replay pack bytes, PDSC leaf, target, PDF, parser, and semantics.
4. Flow support identity proof into BoardValidator and GateManager; stamp exact/compatible explicitly;
   missing proof produces diagnostics-only state.
5. Keep partitions null and destructive recovery unavailable without independent policy.

On setup, copy the accepted PDF bytes atomically to
`.firm/evidence/datasheets/<sha256>.pdf`; this is immutable source evidence, not a second safety
authority. The profile stores its digest and project-relative evidence reference. Replay refuses a
missing or changed file. Datasheet capture is deterministic and bounded: fixed parser version, byte
limit, PDF signature, canonical content-addressed path, and no OCR/network fallback. It binds the
research source rather than turning arbitrary prose into numeric authority. Pack geometry is usable
for diagnostics; deployment additionally requires the closed allocation proof. The schema-v3 map
persists only canonical evidence digests.

Generic profiles remain schema v2 for compatibility but use a closed `device_support` discriminator.
Their `BoardConfig` adapter stores only selected runtime facts: actual probe family/type, canonical
target, empty wiring hints, UART baud only when used, no recovery mode, and nullable test/identity
fields. `mcu_family` is a non-authorizing display token. Existing catalog profiles are not silently
migrated; refresh preserves their compatibility path.

## Slice 4 - Board facts and deployment

1. Keep actual probe/UART and attach hints separate from device authority.
2. As refined by ADR-0004 and the flexibility follow-up plan, extend schema v3 with a closed
   `artifact_application_allocation` policy containing exact sectors, bounded-driver proof,
   creation map/artifact digests, optional parent allocation, and allocation digest.
3. Create or monotonically expand it only through an approved artifact-bound flash plan after
   physical/sector/vector/target checks and before backend mutation. Failed validation consumes no
   permission/budget and performs no mutation.
4. Retain capability-specific refusal when proof is unavailable; refresh can reproduce but cannot
   create or widen an allocation.

Allocation creation runs under the existing board execution lock. It captures the current canonical
map digest, performs all driver/artifact checks, then calls a compare-and-swap repository method
immediately before backend mutation. It re-loads the map and commits only if the digest is unchanged
and the parent allocation still matches. The candidate map is built fully in memory and atomically replaced. A
programming failure retains the narrow allocation so partially written flash cannot become ownerless.
The persisted policy contains every replay input. Refresh rederives the physical map, copies the exact
allocation only if every sector is still valid and contained, and otherwise refuses without writing.

## Slice 5 - Surface, compatibility, and verification

1. Update setup prompts so the agent, never the user, resolves support.
2. Preserve schema-v2 profiles as compatibility-only and prohibit automatic reference-board policy
   inheritance for new generic profiles.
3. Add fresh-root MCP tests for no-candidate research, successful pack promotion/restart replay,
   generic map creation, identity levels, and first-flash allocation. Add zero-backend-call tests for
   unsupported identity, absent deployment, pack/PDF drift, nonblank flash, unbounded driver, and
   artifact/range rejection.
4. Run focused tests, Ruff, Pyright, full locked pytest, package/import, stdio smoke, and an
   adversarial diff review. Perform a second outer codebase audit and repeat if it finds a defect.
