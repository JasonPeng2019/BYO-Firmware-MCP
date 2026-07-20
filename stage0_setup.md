# Stage 0 board setup

Use this guide only after [init.md](init.md). Every command assumes the current
directory is the `BYO-Server/` checkout and the independent environment is
synced with `uv sync --locked`.

## Safety boundary

Host bootstrap is observational except for dependency/pack installation. Stage
0 normally connects, reads identity, resolves UART, and reports manual gaps.
Supplying `--reference-firmware` authorizes a real flash for that board.
Supplying `--recover-test` authorizes a destructive erase/unlock cycle. Do not
run those commands against an unknown board, shared bench, or uncommitted target
state.

The MCP server keeps the same server-owned guardrails regardless of which model
or client connects. Tool contracts and confirmation rules are the docstrings in
`src/pyocd_debug_mcp/server.py`.

## 1. Host readiness for one board

```text
uv run --locked python host_bootstrap.py --board-id <board-id> --install-packs
```

The checkout includes `nrf52833dk`, `nrf52840dk`, and `nucleo_l476rg` profiles.
Resolve every FAIL before continuing. A WARN about no
probe or serial port means the attached-board path is not ready even if Python
dependencies pass.

## 2. Non-destructive Stage 0 pass

```text
uv run --locked python stage0_check.py --board-id <board-id> --install-packs
```

If discovery is ambiguous, add `--port <board-id>=<port>` or use a local
`PYOCD_PROBE_UID`. Add `--confirm-shared-usb <board-id>` only after a human has
confirmed that the visible probe and COM port belong to the same physical
board.

Without a reference artifact, the flash/UART parts remain MANUAL. That is an
honest partial result, not a Stage 0 pass.

## 3. Authorized Stage 0 reference flash

Inspect the artifact and board selection first. Then, only with explicit
authorization:

```text
uv run --locked python stage0_check.py \
  --board-id <board-id> \
  --reference-firmware <board-id>=<path-to-approved-hex-or-bin> \
  --expect <board-id>=<expected-uart-substring> \
  --confirm-shared-usb <board-id>
```

On PowerShell, place the command on one line or use PowerShell backticks rather
than the shell continuations shown above.

For Nordic boards requiring recovery proof, the separate destructive flag is:

```text
uv run --locked python stage0_check.py --board-id nrf52833dk --recover-test nrf52833dk
```

This may erase flash. Never infer authorization from a general request to test
or inspect the board.

## 4. Start the MCP server

Register the checkout command from [README.md](README.md), or start it from an
MCP-aware client with:

```text
uv run --locked pyocd-debug-mcp
```

This is a stdio protocol process, not an interactive shell. Use the MCP client
to initialize, list tools, connect one board, and explicitly call `disconnect`
before stopping the client.

## Troubleshooting

| Symptom | Likely cause | Fix and rerun |
| --- | --- | --- |
| `uv sync --locked` rejects the lock | Metadata and lockfile differ | Do not update casually; regenerate with `uv lock`, review the diff, then rerun `uv lock --check`. |
| pyOCD sees no probe | USB/driver/vendor support or another process owns it | Close other probe tools, reconnect one board, repair vendor support, rerun host bootstrap. |
| Multiple probes or serial ports match | Discovery cannot safely choose | Disconnect extras or supply a board-scoped probe UID/`--port`, then rerun Stage 0. |
| The only visible VCP explicitly belongs to another probe family | Another attached board exposes a serial port, but the selected board does not | Do not confirm or reuse that VCP. Attach the selected board's UART or provide its reviewed `--port <board-id>=<port>`, then rerun. |
| Target is unavailable | Pinned pack is absent or failed verification | Rerun host bootstrap/Stage 0 with `--install-packs`; inspect `packs/manifest.yaml` and the reported checksum error. |
| STM32 programmer helper is missing | STM32CubeProgrammer is not installed/on PATH | Install it from the vendor, reopen the shell, rerun the host setup script. |
| Flash input is refused | Missing/non-file/unsupported or policy-disallowed path | Use a reviewed local `.hex`/`.bin` artifact and follow the server/Stage 0 refusal text. |
| Recover is refused | Board policy disallows it or confirmation is absent | Confirm the selected profile and use the explicit confirmation path only when destructive recovery is authorized. |
| A command timed out | The direct operation exceeded its budget | Treat cleanup as uncertain, audit processes/probe ownership, disconnect/reconnect the board if safe, then rerun from host bootstrap. |
