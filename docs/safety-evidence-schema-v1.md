# Safety evidence schema v1

Task 12 accepts hardware evidence only through the strict `verify2` schema below. Unknown,
missing, or authority-inappropriate fields reject the complete evidence document. This schema
contains hardware facts to compare; it does not accept request-specific `allowed_ranges` or any
gate, plan, or permission authority.

Top-level fields are exactly:

```text
schema_version, role, device, sources, regions
```

- `schema_version`: integer `1`.
- `role`: `device_support` for deterministically loaded Pack/CMSIS/SVD/target facts, or
  `official_document` for official datasheet/reference-manual facts. Automatic catalog setup
  loads both from distinct repository-reviewed, SHA-256-pinned JSON documents; a research flow
  may instead validate a strict official-source response before staging it.
- `device`: exactly `mcu_part_number` and `target`. Device-support evidence requires the exact
  target; an official document may use null when it does not name a pyOCD target.
- `sources`: a nonempty list. Every item has exactly `kind`, `identifier`, `version`, and
  `revision`. Device support permits `pack`, `cmsis`, `svd`, and `target`; official evidence
  permits `datasheet` and `reference_manual`.
- `regions`: a nonempty list of hardware facts.

Each region has exactly:

```text
fact_id, name, name_aliases, kind, start, end, range_convention,
address_aliases, bank, block
```

Ranges use decimal integers or `0x`-prefixed hexadecimal strings. `range_convention` is exactly
`half_open` or `inclusive_end`; both normalize to an internal half-open range. Name differences
reconcile only through an explicitly shared normalized name/alias. Address differences reconcile
only when the same normalized half-open range occurs in the primary range or an explicit
`address_aliases` entry on both sides. Region kind, bank, and register-block identity must agree.

Reconciliation additionally requires both sources to match the profile's exact MCU part number
and target anchor. Any missing fact, ambiguous match, conflicting kind/range/bank/block, variant
mismatch, or target mismatch yields `conflict` with no promoted regions.

For automatic catalog setup, the profile must already preserve the user's exact package-level
part number (for example `nRF52840-QIAA`); a family-only value is rejected before profile commit
and is never silently expanded. The loader also verifies the official PDF bytes, evidence-asset
hashes, installed pyOCD version, target-module hash, and SVD-bundle hash before reconciliation.
Only `reconciled` provenance is promoted to `memory_map.yaml`; the two distinct source documents,
hashes, and runtime identities remain visible in `source_manifest.json`.
