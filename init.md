# BYO Server initialization

This guide prepares a `BYO-Server/` checkout. It does not flash, recover, or
otherwise mutate a board by itself. The setup scripts can install host software
and download dependencies, so inspect or dry-run them before execution.

## Prerequisites

- A Windows or macOS host with network access for the initial dependency and
  CMSIS-Pack setup.
- A local clone containing the complete `BYO-Server/` tree. Installed-wheel
  operation is not supported because checkout data is intentionally not
  package data.
- Python 3.12 and `uv`. The setup scripts can install these on their supported
  host.
- USB access to the intended board when you proceed to host/board checks.
- Vendor support appropriate to the selected probe:
  - Nordic J-Link boards need SEGGER J-Link support. Windows setup can install
    it through winget; on macOS, install the appropriate SEGGER support before
    board validation if pyOCD cannot use the probe.
  - ST-LINK boards may need STM32CubeProgrammer. Both setup scripts repair PATH
    for an existing install, but neither provides a verified unattended
    STM32CubeProgrammer installation.
  - `nrfjprog` is an optional Nordic discovery/fallback helper, not the primary
    MCP flash contract.

Do not store provider credentials in this project. Ordinary MCP operation does
not require a model-provider key.

## 1. Enter the checkout and inspect setup

Run all commands from `BYO-Server/`.

Windows PowerShell:

```powershell
Get-Content .\setup_host.ps1
.\setup_host.ps1 -BoardId nrf52833dk -DryRun
```

macOS:

```bash
less ./setup_host.sh
./setup_host.sh --board-id nrf52833dk --dry-run
```

Select only the board you intend to prepare. The scripts otherwise select all
non-example board profiles.

## 2. Run bounded host setup

Windows PowerShell:

```powershell
.\setup_host.ps1 -BoardId nrf52833dk
```

macOS:

```bash
./setup_host.sh --board-id nrf52833dk
```

Replace the board id with `nucleo_l476rg` when preparing the official ST board.
The scripts sync the independent lockfile, inspect the board through the shared
loader, set up host dependencies they know how to manage, provision pinned
packs, and run `host_bootstrap.py`. Project build SDKs and toolchains are not
selected or provisioned by host setup; the agent resolves them from project
metadata and available host resources.

If you do not want the setup script to install host software, install Python
3.12, `uv`, probe/vendor support, and libusb where applicable yourself, then
run:

```text
uv sync --locked
uv run --locked python host_bootstrap.py --board-id <board-id> --install-packs
```

`host_bootstrap.py` checks dependencies, pyOCD target/probe visibility, serial
ports, board config, and pinned packs. It does not prove the full board path.

## 3. Optional machine-local defaults

Copy `.env.example` to `.env` only if discovery needs a machine-specific probe
UID or target override. Leave values empty when normal board-aware discovery
works. `.env` is ignored and must not be committed.

```text
PYOCD_PROBE_UID=<whole-or-partial-probe-uid>
PYOCD_TARGET=<optional-pyocd-target-override>
```

Board facts belong in `boards/*.yaml`; serial ports, probe UIDs, and host paths
do not.

## 4. Continue with board validation

Follow [stage0_setup.md](stage0_setup.md). Read every destructive flag before
using it. In particular, Stage 0 `--recover-test` and MCP `target_unlock` can
erase the target and always require explicit opt-in/confirmation.
