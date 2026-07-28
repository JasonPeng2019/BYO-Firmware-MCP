Fixed the regression command’s Bash portability; it now passes under the neutral runner model:

`./.h01-venv-batchstrict/Scripts/python.exe -m unittest -q tests.test_regression_s11_datasheet_lazy`  
Result: 2 tests passed.

Covered blast-radius edges: final-page complete traversal, one-based proof locus, and `None` metadata/page-text handling before a later valid proof.

Residual risk: the neutral report’s spec command has the same backslash-path failure and needs its owner updated. Charter checkpoints recorded: initial/pre-edit, pre-verification, final verdict.
