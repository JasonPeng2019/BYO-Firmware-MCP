# MCP Firmware Server — Design Charter & Agent Operating Goals


## Mission
Use this charter in addition to the main prompt being given. Wait/Listen/Review for the main prompt input before doing anything. I will supply it to you or have already supplied it to you. Do not return an answer unless the main prompt is actually blocked by something unpassable, or the task in the main prompt has been fully executed to completion. When executing the main prompt, for ANY changes made to the actual MCP server, build and maintain an MCP server that lets an AI agent do real firmware
engineering — discover and configure targets, build, flash, debug, inspect,
and iterate — across **arbitrary hardware, toolchains, and host environments**,
without a human having to hand-hold the setup for each new part.

This charter is the goal to keep in mind for any active changes to the existing MCP server.
You should not edit parts that are not broken / did not ask to be edited, even if you think
parts of the server you have read did not fully line up to this charter - those parts work, and I 
don't want to break them.
This document is the standard every change is measured against. When a design
decision is unclear, resolve it toward these goals using the tie-breakers in
*When principles collide*, and **state the assumption you made** (in the tool's
output, its docstring, or a code comment — wherever the next reader will see it).


---


## The four core properties


### 1. Correctness — behave the way the idea intends
The server does what a competent firmware engineer would expect, not an
approximation of it.


- A `flash` tool actually writes **and verifies**; a `read_register` returns the
  true value; a `reset` leaves the device in a genuinely known post-reset state.
- **No silent failure and no fabrication.** If a flash fails verification, the
  tool reports failure — it never returns success. If the server cannot
  determine something (e.g. it can't read a chip ID), it says so rather than
  guessing a plausible-looking answer.
- Anything that deviates from intended behavior — wrong logic, a functionality
  gap, a bug, a swallowed error — gets replaced with an implementation that
  matches the intent.
- If the spec is ambiguous, resolve to the behavior the design most clearly
  implies (e.g. "reset" almost always means "get the part into a clean known
  state," so do the reset that achieves that) and record the assumption.


### 2. Simplicity — Occam's razor
The simplest form that is still highly effective.


- Each tool does **one** job well. No 40-knob config where 3 knobs cover the
  real cases.
- No speculative abstraction, no gold-plating, no plugin framework built for a
  single plugin, no machinery that isn't earning its keep.
- Prefer flat, obvious control flow. If wrapping an existing, battle-tested tool
  (OpenOCD, probe-rs, esptool, avrdude, etc.) does the job, wrap it — don't
  reimplement a protocol.
- Litmus test: a new engineer should be able to understand a tool's
  implementation in one sitting.


### 3. Generalizability — works for setups it has never seen
The code must work for any reasonable hardware, OS, and software environment —
not just the one it was tested on.


- **No environment-specific constants.** No hardcoded `/dev/ttyUSB0` or `COM3`,
  no "Linux-only" assumptions, no baked-in core/GPU counts, no fixed toolchain
  Paths, no fixed build environments, no specific toolchains EVER. This should work for EVERY board, EVERY build system, EVERY repo, EVERY environment, and EVERY part.
- **Detect, parameterize, or degrade gracefully — in that order.** Discover
  serial ports, debug probes, chip IDs, and installed toolchains at runtime.
  Parameterize what varies. If auto-detection fails, expose a tool for the agent
  to supply the missing piece — and persist it so the gap closes permanently.
- **Autonomous onboarding.** The setup path should take an unknown board and
  bring it to the same known-good state a supported board reaches, on its own.
  Onboarding a new MCU must never require editing source.


### 4. Neatness — organized for agent and human alike
No sloppiness, no tangled web.


- Consistent naming: tools read as `verb_noun`; shared parameters keep the same
  name and units across every tool (`port`, `target`, `elf_path`, `baud`).
- One home per responsibility — detection logic lives in one place, not smeared
  across five tools.
- No dead code, no commented-out experiments, no confusing control flow. Tool
  interfaces are predictable enough that the shape of the next tool is guessable
  from the last.


---


## 5. Usability — the server must teach its user how to use it
Simplicity includes being intuitive to operate, for both an agent and a human.


- Every tool description states: **what it does, when to reach for it, its
  parameters (with units and an example), what it returns, and its common
  failure modes with the recovery step.**
- Ship a top-level "start here" resource the agent can read first: the normal
  workflow (`detect → configure → build → flash → verify → debug`) and how the
  tools chain together.
- The agent should never have to guess a contract or discover behavior by
  trial. If it needs outside knowledge to use a tool correctly, the tool's docs
  are incomplete.


---


## 6. Dynamism — solve the novel case with the tools, not a code change
This is the differentiator. When the agent meets a board, chip, or toolchain
the server has never seen, it should be able to succeed using the tools that
already exist.


- **Introspection first:** tools to enumerate what's connected and identify what
  it is (probe list, port scan, chip-ID read, toolchain discovery).
- **A generic path** to talk to a target over its standard interface
  (SWD/JTAG/UART/DFU) even when no named profile for that exact part exists yet.
- **Learn and persist:** a way for the agent to register what it figured out so
  the previously-unknown part is *known* on the next run.


Design tools to be composable and general enough that "I've never seen this
part" is handled by the agent orchestrating existing tools — **not** by someone
adding another branch to a `switch` statement.


---


## 7. Operating assumptions — design for the normal case
Build for ordinary firmware work under normal conditions, not for exotic edge
cases that won't happen.


- Assume a **compliant, non-malicious agent** and a **cooperative user** doing
  regular firmware tasks with regular firmware parts. *Compliant does not mean
  infallible.* Assume the agent is only OK at its job: it follows tool contracts
  in good faith but will make honest mistakes — misread returned state, act on a
  stale or unverified assumption, select the wrong target, or thrash in a retry
  loop. The threat model excludes the **hostile** actor; it does **not** exclude
  the **fallible** one. Guarding against the agent's own mistakes is in scope;
  hardening against an attacker is not.
- **The agent and the user are inside the trust boundary — but the boundary is
  about hostility, not competence.** Anything they hand the server — a pack file,
  a path, an ELF, a device selection, a build command — is *trusted input* in the
  sense that it is not an attack. It is **not** assumed to be *correct*. Validate
  it for **correctness** (is it the right, real thing; does it match verified
  state; will it make the tool behave correctly?), never for **hostility**. The
  server does not treat its operator as an attacker, but it does treat the
  operator as capable of error — it catches the mistake rather than faithfully
  executing it.
- **Handle the failure modes that actually occur** every day: no device
  connected, wrong port, insufficient permission to open the port, build errors,
  flash/verify mismatch, a probe that dropped off the bus, a brownout mid-flash.
  **Also handle the mistakes a mediocre agent actually makes:** flashing an ELF
  whose target doesn't match the detected chip, issuing a destructive step
  against an unverified or wrong target, acting on detection results that have
  gone stale, or repeating a failing operation in a loop instead of surfacing the
  real error. These are all part of correctness.
- **Don't** spend the complexity budget defending against adversarial input,
  impossible states, or bizarre configurations that won't arise in normal use.
  Robust where reality demands it; not paranoid where it doesn't.


### 8. No paternalistic or adversarial guarding — but do catch the agent's mistakes
The server protects **correctness**. It does **not** protect the engineer from
their own legitimate project, and it does **not** harden trusted input against
attacks the deployment model says cannot happen. Over-defensive guarding is a
defect: it burns the complexity budget and, worse, it blocks real work.
Paternalism is refusing or second-guessing an operation the agent *intended* and
*correctly aimed*. It is **not** paternalism to catch an operation the agent did
*not* intend — one that contradicts verified state, targets the wrong or an
unverified device, or is the product of a thrash loop. Guarding against the
fallible agent's own mistakes is a correctness duty, not nannying.


- **Safety of the project is the engineer's job; catching mistakes is the
  server's.** If the engineer is *intentionally* doing something risky — erasing a
  bootloader, writing option bytes, flashing a part in a way that could brick it —
  the server performs the requested operation (with honest verification and honest
  reporting of what happened) and lets the engineer own the risk. It does not
  refuse, second-guess, gate a coherent correctly-targeted request behind "are you
  sure," or nanny. **But an intended risky operation and a mistaken destructive
  one look different to the server, and it must tell them apart by checking
  against what it has verified.** Before a destructive or hard-to-reverse step,
  confirm the operation is consistent with verified state — the target the agent
  named is the one actually on the bus, the ELF matches the detected part, the
  port is the one that was detected. If the request contradicts verified state,
  that is a mistake, not a brave choice: stop and report the mismatch instead of
  faithfully executing it. The server never blocks the risk the engineer *chose*;
  it does block the damage the agent *didn't mean to do*.
- **No adversarial-input hardening on trusted inputs.** ZIP-bomb guards,
  path-traversal rejection, XML-entity-expansion limits, member-count ceilings,
  compression-ratio checks, sandbox walls — none of these belong on a pack or
  file that a compliant agent selected. They defend against an attacker this
  deployment does not have, and they routinely break legitimate work (e.g. an
  official STM32U5 pack that expands past an arbitrary size cap).
- **Every limit must trace to a real constraint.** A cap, buffer size, timeout,
  or count limit is allowed only when it comes from actual hardware, a protocol, a
  correctness requirement, or the need to stop a fallible agent from thrashing (a
  bounded retry/loop that ends in an honest error, so a mistake surfaces instead
  of spinning) — never from a hypothetical adversary or "just in case." A limit
  set by the target's real flash size is correctness; a retry bound that ends in a
  truthful failure report is correctness. An arbitrary magic number — a 128 MiB
  expanded-size ceiling with no hardware basis — is still an anti-pattern; raise
  it out of the way or delete it.
- **Keep checks that catch the tool *or the agent* getting it wrong.** Rejecting a
  genuinely corrupt or wrong file so the tool doesn't silently do the wrong thing
  is correctness, and it stays. Catching a requested operation that contradicts
  verified state — wrong target, mismatched ELF, stale detection — is correctness,
  and it stays. Rejecting a valid file because it *could* have been malicious is
  paternalism, and it goes. Cheap malformed-file hygiene is fine when its purpose
  is "don't misprocess a broken file" or "don't execute an obvious mistake," not
  "assume the operator is hostile."


**The test for any guard:** *"Does removing this let a compliant but fallible
agent — one that may thrash, misread state, target the wrong device, or act on an
unverified assumption — or a cooperative user get a wrong, destructive, or
misreported outcome they did not intend?"* If yes, it is correctness — keep it.
If it only ever stops an attacker who isn't in this threat model, or stops the
user from doing something *risky-but-intended and correctly-targeted*, delete it.
The dividing line is intent versus mistake: guard the mistake, permit the
intended risk.


#### Worked example — `flash_target` with a mismatched ELF

The same scenario resolved three ways. An agent calls
`flash_target(port="/dev/ttyACM0", elf_path="build/app.elf")`. The server has
already detected, via a chip-ID read on that port, that the connected part is an
**STM32L4**. The ELF's headers say it was built for an **STM32F4**. Flashing an
F4 image to an L4 will not run and may leave the part in a state the agent then
misdiagnoses.

- **Naive / stripped bare (wrong).** The tool flashes whatever bytes it was
  handed and reports success because the write-and-verify against its own image
  passed. The agent, being only OK at its job, now believes it has working
  firmware on the L4 and burns the next hour debugging a "runtime" problem that is
  really a build-target mistake. This is a **correctness failure**: the server
  faithfully executed a mistake and misled the reader about what happened.

- **Paternalistic (also wrong).** The tool refuses to flash any image whose
  target it doesn't have an allow-listed profile for, or pops "this could brick
  your board — are you sure? [y/N]" on every flash. This blocks the engineer who
  *deliberately* flashes an unusual or bring-up image to a correctly-identified
  part, and it defends against nothing real. Delete it.

- **Mistake-guard (correct).** Before writing, the tool compares the ELF's target
  against the **verified** chip ID it already read from that port. They disagree,
  so it stops and returns an honest, actionable error: detected `STM32L4xx` on
  `/dev/ttyACM0`, but `build/app.elf` targets `STM32F4xx` — refusing to flash a
  mismatched image; rebuild for the L4 or select the correct port/ELF. It does
  **not** ask "are you sure"; it reports a contradiction with reality. Note the
  precise boundary: if chip-ID detection was **unavailable** (no readback on this
  interface), the tool cannot assert a mismatch — so it does not invent one. It
  flashes, states in its result that the target could not be verified against the
  device, and lets the engineer own that gap. The guard fires only on a *verified*
  contradiction, never on an *unverified* suspicion.

The test applied: removing the mismatch check lets a compliant-but-fallible agent
brick or mis-flash a board and be told it succeeded → correctness → keep it.
Removing the "are you sure" prompt costs a compliant agent nothing except a
speed bump on work it meant to do → paternalism → delete it. Same tool, opposite
verdicts, decided entirely by *verified state* and *intent versus mistake*.


---


## When principles collide
- **Simplicity vs Generalizability** → choose the simplest design that still
  handles the unknown *by detection*, not the most abstract one. Runtime
  detection beats an abstraction layer.
- **Generalizability / Dynamism vs Correctness** → generality never licenses
  guessing. On novel hardware, **verify** (read back, confirm IDs) instead of
  assuming; if it can't be verified, report the uncertainty and let the user decide how to proceed.
- **Correctness vs Simplicity** → correctness wins — but once the real
  requirement is understood, the correct implementation is usually also the
  simpler one.
- **Normal-case assumption vs Correctness** → assuming the normal case decides
  *which situations to handle*. It is never an excuse for a tool to misreport
  what happened.
- **Defensive guarding vs Functionality** → within the trust boundary,
  functionality wins. A guard that blocks legitimate work to defend against an
  actor this deployment model excludes — or to protect the engineer from an
  intended, correctly-targeted risky operation — is removed, not tuned. But a
  guard that catches the fallible agent's own mistake — an operation contradicting
  verified state, a destructive step on an unverified target, an unbounded thrash
  loop — is a **correctness** guard, not defensive guarding, and it stays.
  Correctness guards (verify, don't fabricate, don't misreport, don't misprocess a
  broken file, don't faithfully execute a mistake) are non-negotiable.


---


## Definition of done
A change is done when all of these hold:


1. It does the intended thing and reports honestly (no silent failure, no
   fabricated results).
2. It is the simplest form that is still effective — nothing speculative.
3. It contains no environment-specific constants; it detects or parameterizes
   everything that varies.
4. It is cleanly named and lives in the one place that owns its responsibility.
5. Its tool docs let an agent use it correctly with zero outside knowledge,
   including how to recover from its common failures.
6. A novel-but-reasonable input is handled by composing existing tools, not by
   editing source.
7. Every guard, limit, and rejection it introduces traces to a correctness or
   real hardware/protocol constraint — none of it hardens against a hostile actor
   outside the threat model, and none of it nannies the engineer out of a
   legitimate, correctly-targeted operation they chose to run. Guards that catch
   the fallible agent's own mistakes — a request contradicting verified state, a
   destructive step on an unverified target, an unbounded retry loop — are
   correctness guards and are expected to be present, not stripped.


---


## Anti-patterns — reject these
- Hardcoded ports/paths, OS checks that break elsewhere, fixed core or toolchain
  assumptions.
- Silent failure, swallowed errors, or fabricated success/data.
- Speculative abstraction, unused config knobs, a plugin framework with one
  plugin.
- Detection or config logic scattered across many tools; inconsistent parameter
  names or units.
- Tools with missing docs, or docs that omit failure modes and recovery.
- A `switch`/`if` ladder that must grow every time a new chip appears — that is a
  detection-plus-generic-path job instead.
- Any hardcoded systems or boards or part numbers, or any features that are designed to target only a specific few listed systems and bias towards those systems without offering the same improvement to all other possible systems in existence.
- **Adversarial-input hardening on trusted input** — ZIP-bomb / entity-expansion
  / path-traversal / member-count / compression-ratio guards applied to a pack
  or file the compliant agent chose. That defends against an actor outside this
  threat model.
- **Paternalistic refusals, prompts, or caps** that block a legitimate,
  correctly-targeted, *intended* risky operation on the engineer's own hardware.
  The engineer owns the risk of their own project. (This is **not** a license to
  drop guards that catch the agent's *mistakes* — an operation contradicting
  verified state, a destructive step on an unverified target, or a thrash loop.
  Those are correctness guards and must stay.)
- **Arbitrary magic-number limits** (size / buffer / count / timeout) not derived
  from a real hardware, protocol, or correctness constraint — e.g. a fixed
  expanded-size ceiling that blocks an official current vendor pack.
