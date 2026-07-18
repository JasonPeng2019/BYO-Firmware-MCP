# ADR-0001: Single-file stable safety authority

## Status

Accepted for Safety Layer v2 implementation.

## Context

The original safety layer persisted a memory map, a source manifest, a safety report, per-source
fingerprints, and an aggregate fingerprint. Application artifacts were included in the aggregate.
This duplicated authority, coupled every build to refresh, and allowed bookkeeping differences to
invalidate otherwise correct safety state.

## Decision

Persist only a deterministic `memory_map.yaml` containing stable identity, reviewed source digests,
geometry, partitions, regions, and provenance. Compute its canonical digest in memory. Do not
persist gates or an aggregate fingerprint. Remove safety manifests and safety reports.

Treat firmware artifacts as per-operation inputs. Bind their bytes to accepted flash plans and
perform complete containment at execution time. Make `board_safety_refresh` deterministically
rebuild one complete candidate map for every safety-only update or failure; change classifications
remain explanatory rather than separate mutation algorithms.

## Consequences

- Existing safety directories are intentionally incompatible and require a fresh refresh/rebuild.
- Fresh test roots must run the current setup path once before acceptance.
- Normal firmware builds no longer require safety refresh.
- Runtime loaders, refresh, validation, recovery disclosure, docs, and tests must stop depending on
  source manifests or safety reports.
- Map generation must remain independently reproducible from reviewed server-owned sources.
