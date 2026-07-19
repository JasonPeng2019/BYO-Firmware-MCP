# Plan: selected-board routing among multiple probes

Specification: `docs/universal-onboarding-multi-probe-routing-spec.md`

1. Refactor `setup_overview` validation so explicit assignments cover exactly the named boards while
   allowing additional visible connections to remain unused.
2. Return assignment choices whenever a named board is ambiguous among multiple visible connections;
   retain automatic mapping only for the one-name/one-connection case.
3. Add focused tests for selected-subset routing and over-subscribed names.
4. Update handshake, tool, and agent-contract prose to describe selected-board rather than exhaustive
   probe routing; regenerate the tool-description contract.
5. Run focused tests through a Terra-medium fast reviewer, vet results, then run the complete suite
   and adversarial diff audit before retrying the fresh Luna-medium acceptance in a new repository.

