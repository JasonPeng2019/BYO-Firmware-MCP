# New Brain Spec implementation audit

Audit date: 2026-07-17

This audit compares the current server to `New_Brain_Spec.md` at the behavior and
agent-usage level. A module or similarly named test was not counted as coverage
unless the public MCP path and its enforcement behavior were inspected.

## Product-level coverage

| New Brain area | Implementation evidence | Audit result |
| --- | --- | --- |
| Layer 1 plans | `guardrails/plan_defs.py`, `plan_engine.py`, `permissions.py`, generated plan handlers | Implemented: all-NULL first, immutable exact binding, budgets, permissions, relock and per-board/run scope |
| Layer 2 safety | `safety/regions.py`, `verify2.py`, `fingerprints.py`, `map_build.py`, `refresh.py`, `guardrails/gate.py` | Implemented fail-closed with prohibited precedence, per-call write freshness and no persisted authority |
| Dynamic visibility | `kernel/registry.py`, guarded dispatch in `server.py`, accepted-plan fallback | Implemented; hidden handlers remain physically locked, with exact guarded transport for static-binding clients |
| Board isolation | `services/connections.py`, board-scoped plans/gates/permissions | Implemented with one-to-one assignments and per-board locks |
| Setup persistence | `firmstore/*` and `.firm` layout | Implemented with atomic profile/map writes, immutable reports and ignored attachment/pack payloads |
| Startup and setup routing | `initialization_handshake`, `setup_overview`, setup tool descriptions | Implemented after this audit; every matching YAML validates first and unknown names route to setup without asking the user for internal IDs |
| Setup state machine | `setup_flow/preflight.py`, `setup.py`, `validate.py` | Implemented with deterministic inventory, friendly choices, paired repair and terminal statuses |
| Research and pack support | `research.py`, `targets.py`, `packs.py`, public `continue_setup` | Implemented after this audit; exact response schemas now have a real MCP return path |
| Cleanup/cancellation | `kernel/operations.py`, `finalizers.py`, `hygiene.py`, `processes.py` | Implemented with finite bounds, one cleanup path, process groups and reset-line release |
| Revised action surface | `tools/session.py`, `execution.py`, `registers.py`, `memory.py`, `flash.py`, `serial.py`, `unlock.py`, `batch.py` | Implemented; legacy actions are absent and guarded actions share standard dispatch |
| Recovery | `tools/unlock.py` and permission/gate integration | Implemented as typed, disclosed, fresh one-time recovery with the gate left closed |
| Agent-facing prose | plan definitions, handshake, setup continuation/status payloads | Improved in this audit; every plan index entry now explains purpose, trigger, NULL flow, budget and permission |

## Gaps found

### NB-G01 — Valid Zephyr GNU linker-map rows were rejected

The parser treated any safety-looking line it could not parse as a malformed
definition. Zephyr's GNU ld map emits evaluated assignments such as an address,
symbol, equals sign and expression. These are valid and common.

Impact: a correct Zephyr build could not reach safety refresh or guarded flash.

Resolution: accept evaluated GNU map assignments and literal `PROVIDE` rows,
while retaining fail-closed rejection for genuinely malformed safety-symbol
definitions. A real generated Zephyr ELF/map/HEX was parsed successfully and a
focused regression fixture covers the syntax.

### NB-G02 — Ordinary successful actions reset the target during cleanup

The resource binder registered `reset_and_run` as an unconditional final-state
callback for every ordinary operation. A successful UART command therefore
rebooted the application before the next command and erased volatile state.

Impact: direct calls behaved differently from an agent's mental model, and an
ON/OFF console conversation looked unreliable even though both UART calls were
individually valid.

Resolution: successful calls preserve the action's documented target state.
Reset is now explicit through a reset tool or structured `on_exit`; abnormal
started failure, timeout and cancellation still close the unsafe connection,
and mandatory cleanup still releases reset lines and locks.

### NB-G03 — UART defaults encouraged unnecessary reopen and buffer loss

Bounded capture retried by reopening the port, and serial exchange cleared the
input buffer unconditionally. Separate write/read calls also made a multi-step,
stateful protocol awkward.

Impact: opening some board UARTs can pulse reset/DTR, while clearing input can
discard the prompt or immediate response an agent is trying to prove.

Resolution: capture defaults to one open; retry is explicit. Input is preserved
unless `clear_input=true`. The plan-guarded `serial_exchange` validates one to
eight command/expected-response steps before opening the UART, holds one handle,
supports explicit line endings and readiness probes, and stops on the first
mismatch. The separate read/write tools remain available for stateless use.
`get_setup_status` now reports UART attachment readiness separately from general
code readiness, so console-dependent automation can fail its barrier early
without making UART mandatory for unrelated projects.

### NB-G04 — Plan tools were discoverable but poorly self-describing

Most generated MCP descriptions only said to initialize or submit a plan. An
agent could not tell when to use a plan, its safety posture, budget mode or
permission requirement without first guessing the right tool.

Resolution: index descriptions are rendered from the same declarative plan
definition as NULL guidance. They now include purpose, trigger, setup-first
routing, all-NULL protocol, exact action binding, budget and permission.

### NB-G05 — The handshake indexed names rather than usable routes

The initialization response listed visible names but did not explain their MCP
descriptions or provide a server-owned way to resolve familiar names.

Resolution: the handshake now includes bounded tool descriptions, explains the
two-call plan protocol, and directs the agent through `setup_overview` before
hardware plans. Every matching profile now validates first; validation alone
chooses an incomplete-profile repair, while unknown names enter setup.

### NB-G06 — Startup still made the agent invent internal setup routing

There was no public route from ordinary board names to profile state,
server-generated `board_id`, friendly physical inventory and the correct next
tool.

Resolution: `setup_overview` accepts familiar Unicode names (or NULL before the
answer), inventories profiles/probes/UARTs, generates stable candidate IDs for
new names, and returns per-board validation or new-setup routes. Validation may
then return a specific repair, safety, attachment, mismatch, or retry remedy.
User-facing prose must not expose its structured identifiers.

### NB-G07 — Setup choice/research responses had no public continuation tool

The preflight, research, target and package primitives existed, but a real MCP
agent could not submit a friendly choice or official-source candidate to the
live workflow.

Resolution: always-visible `continue_setup` is bound to one live continuation
and board. It accepts exactly one returned `choice_id`, the exact target research
object, or the exact local pack-candidate object. It rejects extra fields,
changed board scope, informal evidence and unsupported candidates. Validated
pack data is staged/promoted through FirmStore; the continuation grants no
permission or gate state. The agent then uses the existing paired
`board_fix_setup` allowance.

### NB-G08 — Project-local promoted packs were not a complete runtime path

Candidate packs were written under `.firm`, while ordinary pyOCD discovery and
some server manifest reads used only checkout-level pack paths.

Resolution: server manifest consumers use the authoritative project-local
manifest, and runtime pack discovery includes the selected artifact root's
`.firm/packs/files` in addition to shipped packs. A candidate is still loaded
only after checksum, target enumeration and live-connect validation.

### NB-G09 — Initial safety setup guessed an application partition and lacked base classes

The automatic catalog map used the catalog deployment ceiling as application
flash and did not contain all required prohibited, peripheral and CPU/system
classifications.

Impact: this blurred physical/deployment policy with linker-owned application
evidence and could make the map appear more complete than it was.

Resolution: setup persists physical flash/RAM, writable RAM, reviewed prohibited,
ROM, peripheral and CPU/system regions. The deployment envelope remains source
evidence only. `board_safety_refresh` creates exact build-provenance application
flash from a selected ELF/map/HEX and proves it is inside the unchanged ceiling.
Setup status and validation fail closed if required base classes are absent.

### NB-G10 — Setup and UART guidance did not explain volatile conversations

The agent was not told that separate port opens can reset some boards or that a
state-dependent test belongs in one exchange.

Resolution: the serial plan's NULL return now states that behavior explicitly,
documents line endings/readiness/input preservation, and includes a complete
multi-step example. The setup and handshake descriptions now give explicit
next-tool routing instead of assuming hidden server knowledge.

### NB-G11 — Static MCP function bindings stranded accepted plans

A fresh autonomous agent correctly completed setup discovery and obtained an
accepted `board_setup-plan`. The server emitted `tools/list_changed`, but that
client retained its startup callable-function snapshot and could not invoke the
newly visible `board_setup`. It correctly refused to guess a hidden call, so
the end-to-end acceptance stopped before setup or code generation.

Resolution: every accepted plan now returns machine-readable preferred and
stable-client calls generated from the immutable plan snapshot. The fallback
is exactly one `action_batch` child and traverses the identical guarded child
dispatch. It carries no permission state. Paired setup repair is separate and
conditioned; destructive recovery receives a fallback only after unchanged
fresh one-time approval. NULL and rejected plans remain non-executable.

## Deliberate fail-closed boundary

An entirely unreviewed MCU or board cannot become writable from agent-supplied
allowed ranges. The New Brain source-authority rules prohibit that. Target and
official pack research can proceed through `continue_setup`, but complete safety
support still requires independently reviewable device-support and official
document evidence. The server reports that as unresolved rather than guessing.
This is a safety boundary, not a hidden partial success. Reviewed catalog boards
remain the fully automated path.
