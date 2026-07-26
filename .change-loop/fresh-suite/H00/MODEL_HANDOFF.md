# H00 model handoff

- Recorded: 2026-07-24
- Reason: the user directed future test and repair-agent work to use `gpt-5.4-mini`
  instead of `gpt-5.6-terra`.
- Existing work is preserved. The sealed H00 spec, server-repair plan, role thread IDs,
  accepted Windows evidence, actual POSIX failure evidence, and prior test results remain valid.
- The existing change-loop doer, spec-tester, and regression-tester thread IDs are resumed with
  `gpt-5.4-mini`; their prior turns are not discarded or repeated merely because the model changes.
- After the server repair passes its gates, one persistent `gpt-5.4-mini` H00 test-agent session
  becomes the recorded owner and resumes only the failed/touched requirements from the durable
  evidence boundary.
