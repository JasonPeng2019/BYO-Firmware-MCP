# UART capture-duration correctness specification

## Observed defect

A live acceptance run requested `read_serial(read_seconds=15, expected_text=null)` and the server
returned after 0.22 seconds. A second request used a deliberately absent match string and returned
after 0.89 seconds. The implementation both returned on the first bytes when no match string was
provided and capped its sole port-open window at 0.75 seconds.

This contradicts the public contract that `read_seconds` is the bounded capture duration and makes
long-running UART evidence, interval measurement, and concurrency observation impossible.

## Required behavior

1. A state-preserving capture with `expected_text=null` keeps the one UART handle open and
   accumulates bytes until `read_seconds` expires or the explicit byte limit is reached.
2. A capture with non-null `expected_text` may return early only after that text is observed or the
   explicit byte limit is reached. If the text never appears, it keeps capturing until
   `read_seconds` expires.
3. The default zero-reopen path opens and closes the port exactly once; extending the capture must
   not add reset-prone reopen behavior.
4. When explicit reopen attempts are requested, intermediate attempts may use the bounded
   per-open window, but the final attempt consumes the remaining overall capture window so the
   helper does not silently truncate the requested observation.
5. Cancellation checkpoints, decoding, bounded byte handling, port cleanup, and exception
   reporting retain their existing behavior.
6. Tool-call results continue to report the measured duration honestly.

## Non-goals

- Do not change `serial_exchange`, whose per-step `read_seconds` is a response timeout and may end
  when the expected response arrives.
- Do not add a second duration/mode parameter. `read_seconds` already expresses the required
  behavior.
- Do not alter setup, plan, permission, gate, or hardware-authority rules.

