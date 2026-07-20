# Plan: CMSIS-SVD Default Register Access

Specification: `docs/cmsis-svd-default-access-spec.md`

1. Add focused resolver tests first for the CMSIS-SVD default and the complete
   register/peripheral/device precedence chain, including the address-block
   fallback and existing explicit access behavior.
2. Centralize access resolution in one small helper in
   `setup_flow/device_support.py`; use `read-write` only after all applicable
   inherited values are absent.
3. Apply the helper to register rows and peripheral address-block rows without
   changing overlap, memory-overlay, or prohibited-region handling; explicitly
   only address blocks explicitly marked with `registers` usage are eligible.
4. Version the generic map-generator digest so existing maps are refreshed
   rather than silently retaining omitted access-less registers.
5. Run the focused generic-device-support and safety-map suites, then Ruff and
   Pyright for affected files.
6. Run the full locked pytest suite. Audit the diff for accidental widening:
   only exact verified SVD-described ranges may gain authority.
7. Rebuild the fresh acceptance map through normal safety refresh and retry the
   previously refused single-register read. No firmware rebuild or flash is
   required for this server-only correction.
