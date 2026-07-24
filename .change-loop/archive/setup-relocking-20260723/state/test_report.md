# Neutral test gate

## Spec suite: PASS

- Command: `uv run python -m unittest tests.test_change_loop_spec`
- Exit code: `0`

```text
............
----------------------------------------------------------------------
Ran 12 tests in 1.294s

OK

```

## Regression suite: PASS

- Command: `uv run python -m unittest -v tests.test_regression_change_loop`
- Exit code: `0`

```text
test_any_final_reset_exception_is_unconfirmed_but_programmer_failure_is_fatal (tests.test_regression_change_loop.FlashStateRegressionTests.test_any_final_reset_exception_is_unconfirmed_but_programmer_failure_is_fatal) ... ok
test_final_reset_transport_loss_is_unconfirmed_not_running (tests.test_regression_change_loop.FlashStateRegressionTests.test_final_reset_transport_loss_is_unconfirmed_not_running) ... ok
test_flash_tool_propagates_unconfirmed_state_to_event_and_operator_remedy (tests.test_regression_change_loop.FlashStateRegressionTests.test_flash_tool_propagates_unconfirmed_state_to_event_and_operator_remedy) ... ok
test_observed_state_and_worker_contract_only_allow_truthful_flash_states (tests.test_regression_change_loop.FlashStateRegressionTests.test_observed_state_and_worker_contract_only_allow_truthful_flash_states) ... ok
test_explicit_artifacts_restore_selected_fields_after_ambiguous_discovery (tests.test_regression_change_loop.NativeBuildAmbiguityRegressionTests.test_explicit_artifacts_restore_selected_fields_after_ambiguous_discovery) ... ok
test_multi_image_discovery_exposes_candidates_without_selecting_or_authorizing_one (tests.test_regression_change_loop.NativeBuildAmbiguityRegressionTests.test_multi_image_discovery_exposes_candidates_without_selecting_or_authorizing_one) ... ok
test_one_sided_ambiguity_clears_all_selected_fields_in_helper_and_build_evidence (tests.test_regression_change_loop.NativeBuildAmbiguityRegressionTests.test_one_sided_ambiguity_clears_all_selected_fields_in_helper_and_build_evidence) ... ok
test_recovery_revokes_assignment_before_cleanup_failure (tests.test_regression_change_loop.RecoveryCleanupRegressionTests.test_recovery_revokes_assignment_before_cleanup_failure) ... ok
test_successful_recovery_closes_both_resources_and_requires_fresh_connection (tests.test_regression_change_loop.RecoveryCleanupRegressionTests.test_successful_recovery_closes_both_resources_and_requires_fresh_connection) ... ok
test_adapter_reset_connect_does_not_reintroduce_ambient_routing_but_open_does (tests.test_regression_change_loop.ResetConnectRoutingRegressionTests.test_adapter_reset_connect_does_not_reintroduce_ambient_routing_but_open_does) ... ok
test_reset_connect_ignores_ambient_uid_and_target_but_normal_resolution_keeps_compatibility (tests.test_regression_change_loop.ResetConnectRoutingRegressionTests.test_reset_connect_ignores_ambient_uid_and_target_but_normal_resolution_keeps_compatibility) ... ok

----------------------------------------------------------------------
Ran 11 tests in 0.098s

OK

```
