# A23 same-build signed HEX companion repair

## Verified production defect

The A23 nRF52840 sysbuild produced a canonical artifact bundle through the public
`collect_build_artifacts` tool:

- `firmware.elf` is the plain application ELF;
- `firmware.hex` is the same-build signed application image; and
- both are the one same-stem companion pair required by the runtime flash path.

The server accepted a `flash_application_plan` for that bundle and the reviewed generic
application allocation was `[0x0000C000, 0x00039000)`. The signed HEX programs
`[0x0000C000, 0x00038DCB)`, wholly inside that allocation. Execution then refused before
mutation with:

```text
HEX contains address 0xc000 absent from ELF load data
```

The preserved reproducer is:

`../fresh-experiments/A23_20260726-044739/.agent-workspace/SERVER_FAILURE_REPRODUCER.md`

The canonical bundle shows two ordinary wrapper ranges absent from the plain ELF:

- a directly adjacent prefix `[0x0000C000, 0x0000C200)`; and
- a directly adjacent suffix `[0x00038D34, 0x00038DCB)`.

The HEX otherwise contains every meaningful ELF byte. Four overlapping bytes at
`0x00032FAC..0x00032FAF` differ only as ELF/HEX fill representations (`0x00` versus
`0xFF`); those addresses correspond to file padding at the start of an ELF `SHT_NOBITS`
region, not meaningful application contents.

## Required observable behavior

1. Accept a canonical same-stem ELF+HEX pair when the HEX contains the complete meaningful
   ELF image plus ordinary, directly connected wrapper metadata/padding immediately before
   and/or after the ELF-backed flash image.
2. Treat differences between the two conventional fill-byte representations (`0x00` and
   `0xFF`) as non-meaningful in the same way the existing completeness check already treats
   omitted `0x00`/`0xFF` ELF bytes as non-meaningful.
3. Continue rejecting a HEX that conflicts with any meaningful overlapping ELF byte.
4. Continue rejecting a HEX that omits any meaningful ELF byte.
5. Continue rejecting unrelated or disjoint HEX-only content rather than treating the
   same-stem filename alone as proof that every extra range belongs to the selected image.
6. Do not require all legitimate connected HEX wrapper bytes to lie inside an
   ELF-symbol-derived application partition. The policy/enforcement layer must remain
   responsible for proving every actual HEX range, execution address, and affected erase
   sector lies inside the reviewed/static partition or content-derived generic allocation
   and verified physical flash.
7. Keep malformed Intel HEX, repeated-address, checksum, EOF, target/identity, vector,
   entry-point, RAM-stack, physical geometry, reviewed-partition, erase-sector, stale-plan,
   and flash readback/verification guards unchanged.
8. Produce an operator-facing refusal that identifies disjoint supplemental HEX content and
   gives the existing artifact-selection recovery when the new relationship check fails.

## Scope and exclusions

- Make the narrow production change in the build-evidence/runtime safety path and its public
  error translation only as needed.
- Add focused automated tests for accepted connected prefix/suffix wrapper data, accepted
  `0x00`/`0xFF` fill equivalence, meaningful conflicts, meaningful omissions, disjoint
  supplemental ranges, and preservation of policy-level partition/geometry enforcement.
- Do not hardcode MCUboot, Nordic, nRF52840, slot addresses, header/trailer sizes, Zephyr, or
  any particular toolchain.
- Do not weaken target identity, reviewed allocation, physical flash, erase-sector, or
  post-flash verification checks.
- Do not change fresh-experiment firmware, fixtures, SDKs, documents, or evidence.
- Do not commit, push, deploy, or operate hardware.

## Compatibility assumption

The accepted general relationship is content based, not filename or platform based: a
supplemental HEX range is legitimate only when the HEX's own contiguous data connects it to
ELF-backed flash content, while overlapping meaningful bytes agree and all meaningful ELF
bytes remain represented. Final authority over where those bytes may be programmed remains
the reviewed safety map and verified device geometry.
