# STM32L476 Reviewed Fresh-Setup Plan

1. Add explicit optional CMSIS-Pack runtime identity fields to reviewed catalog records.
2. Verify exactly one local pinned pack filename/hash and use that digest for target/SVD evidence;
   preserve the built-in-module verification path unchanged.
3. Add STM32L476 official/device-support evidence assets and catalog anchors.
4. Add catalog/runtime/reconciliation/refresh tests, including tampered/missing/ambiguous pack
   refusal and unchanged Nordic module-backed behavior.
5. Run focused tests, Ruff, Pyright, full pytest, and hostile diff review before live setup.
