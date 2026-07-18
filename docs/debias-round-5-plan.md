# De-bias round 5 implementation plan

## Approach

Change only the misleading message emitted for the provider-neutral AP initialization exception.
Keep the typed exception and all recovery/permission behavior intact. Do not introduce a target
classifier: the point is to avoid guessing.

## Steps

1. Replace the nRF52/J-Link prose in `_typed_backend_error(KeyError(1))` with neutral causes and the
   fail-closed setup/validation/typed-recovery remedy.
2. Update the focused adapter assertion.
3. In one in-process MCP `ClientSession`, call visible profile-only `connect` twice through the real
   target-control service and real pyOCD error mapper while faking only physical inventory/profile
   inputs and session objects:
   - an ordinary non-J-Link provider session whose `open()` raises `KeyError(1)`;
   - the existing parameterized J-Link UID-retry fallback, where the first UID-bound session raises
     the recognized serial-open error and the retry without a UID raises `KeyError(1)`.
   Assert both tool calls return identical target-neutral guidance, contain no Nordic, nRF, J-Link,
   or vendor-specific recovery language, and record that the fallback really retried without a UID.
4. Keep the patch surface limited to `_typed_backend_error` and the smoke's physical
   inventory/profile inputs. Do not patch `target_control.open_session`, `_connect_impl`, or the
   mapper under test.
5. Run focused connection/adapter tests, Ruff, and Pyright.

## Smoke test

The paired MCP smoke uses the live `connect` tool, real target-control service, and real pyOCD
adapter error mapping while faking only physical inventory/profile inputs and session objects. In
the same client run it proves the generic provider path and the parameterized J-Link fallback both
produce the same corrected message on the server's actual tool path rather than a side helper.
