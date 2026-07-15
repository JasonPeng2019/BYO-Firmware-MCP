# packs/ — pinned CMSIS-Pack provisioning

This directory holds the **pinned, deterministically-fetched** CMSIS-Pack DFP
files that supply target support pyOCD does not build in (e.g. the exact
`stm32l476rgtx` target for the Nucleo-L476RG).

## Why this exists instead of `pyocd pack install`

`pyocd pack update` builds its device index by bulk-fetching ~1500 vendor
descriptors from many servers, and **silently skips any that fail or time out**.
On restrictive or slow networks this yields a *partial* index that's missing whole
families — reproduced on a Windows host where every ST DFP except one failed to
download, so `pyocd pack install stm32l476` reported `No matching devices` and the
board's exact target was unavailable. The same machine downloads a single pinned
`.pack` file fine. So we provision by pinned URL + sha256, not by the live index.

If you still need the live pyOCD pack index for ad-hoc `pyocd pack find` /
`pyocd pack install` usage, repair it with:

```powershell
uv run pyocd-pack-repair
```

Or repair only the exact missing DFP:

```powershell
uv run pyocd-pack-repair --vendor Keil --pack-name STM32L4xx_DFP
```

The full diagnosis + repair reference lives in
[live_index_repair.md](./live_index_repair.md).

## How it works

- `manifest.yaml` (tracked) pins each pack: id, version, direct URL, sha256.
- `host_bootstrap.py --install-packs` (and the `setup_host.*` scripts) call
  `pyocd_debug_mcp.pack_provision.ensure_all(...)`, which downloads any missing
  pack from its pinned URL and verifies the sha256.
- At runtime the shared pyOCD backend loads every `*.pack` here via pyOCD's `pack`
  option, so `stage0_check.py`, the MCP server, and `tests.harness.stage1_smoke`
  all resolve the exact target with **no dependency on the live index**.
- The `.pack` binaries are gitignored; only `manifest.yaml` + this README are
  tracked.

## Manual fetch (if the automated step can't run)

```powershell
# Windows
Invoke-WebRequest -UseBasicParsing https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack -OutFile packs\Keil.STM32L4xx_DFP.3.1.0.pack
(Get-FileHash packs\Keil.STM32L4xx_DFP.3.1.0.pack -Algorithm SHA256).Hash   # must match manifest.yaml
```

```bash
# macOS / Linux
curl -L -o packs/Keil.STM32L4xx_DFP.3.1.0.pack https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack
shasum -a 256 packs/Keil.STM32L4xx_DFP.3.1.0.pack   # must match manifest.yaml
```

## Updating a pin

Bump `version` + `url` in `manifest.yaml`, recompute the sha256, and commit. The
old `.pack` can be deleted; the next provisioning run fetches the new one.
