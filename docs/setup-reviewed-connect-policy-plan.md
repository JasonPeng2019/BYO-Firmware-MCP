# Reviewed setup connection policy plan

1. Build one ephemeral `BoardConfig` from reviewed catalog and selected probe facts.
2. Pass it to `target_control.open_session` in the existing pre-commit callback.
3. Add STM32 and cross-probe regression assertions.
4. Run focused checks, hostile diff review, and the complete locked suite.
5. Retry fresh hardware setup with a new external-agent repository.
