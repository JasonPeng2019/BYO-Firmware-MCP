# Neutral test gate

## Spec suite: PASS

- Command: `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -v tests.test_h04_stale_datasheet_plan_spec`
- Exit code: `0`

```text
test_changed_pdf_after_primary_refuses_paired_fix_without_completion_authority (tests.test_h04_stale_datasheet_plan_spec.StaleDatasheetPlanSpecTests.test_changed_pdf_after_primary_refuses_paired_fix_without_completion_authority) ... ok
test_changed_primary_pdf_relocks_both_actions_before_work_or_permission_use (tests.test_h04_stale_datasheet_plan_spec.StaleDatasheetPlanSpecTests.test_changed_primary_pdf_relocks_both_actions_before_work_or_permission_use) ... ok
test_definition_and_accepted_payload_declare_exact_pdf_byte_binding (tests.test_h04_stale_datasheet_plan_spec.StaleDatasheetPlanSpecTests.test_definition_and_accepted_payload_declare_exact_pdf_byte_binding) ... ok
test_unchanged_pdf_keeps_primary_and_one_paired_allowance (tests.test_h04_stale_datasheet_plan_spec.StaleDatasheetPlanSpecTests.test_unchanged_pdf_keeps_primary_and_one_paired_allowance) ... ok

----------------------------------------------------------------------
Ran 4 tests in 0.047s

OK

```

## Regression suite: PASS

- Command: `./.h01-venv-batchstrict/Scripts/python.exe -m unittest -v tests.test_regression_h04_stale_datasheet_plan`
- Exit code: `0`

```text
test_removed_datasheet_relocks_primary_and_paired_before_setup_preconditions (tests.test_regression_h04_stale_datasheet_plan.StaleDatasheetPlanRegressionTests.test_removed_datasheet_relocks_primary_and_paired_before_setup_preconditions) ... ok

----------------------------------------------------------------------
Ran 1 test in 0.015s

OK

```
