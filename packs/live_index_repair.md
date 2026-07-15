# CMSIS-Pack Live Index Repair

Reference for the deeper issue behind `pyocd pack install <x>` reporting
`No matching devices. Please make sure the pack index is up to date` even after
`pyocd pack update` "succeeds".

> You usually do not need this. The repo provisions the packs it needs
> deterministically from `packs/manifest.yaml` via
> `uv run python host_bootstrap.py --install-packs`, which does not depend on the
> live index. This guide exists for benches that still want interactive
> `pyocd pack find` / `pyocd pack install` to work.

## What failed

`pyocd pack update` delegates to `cmsis-pack-manager`, which bulk-fetches every
vendor descriptor (`*.pdsc`) from the master `index.pidx`.

On the reproduced Windows host:

- the master index listed `1775` descriptors
- only `1478` landed in the local cache
- `Keil.STM32L4xx_DFP` was one of the missing descriptors
- the resulting `index.json` had `0` `STM32L4*` devices, so
  `uv run pyocd pack find stm32l4` returned no matches

This was not an ST permission block on the STM32L4 files:

- `https://www.keil.com/pack/index.pidx` returned `200`
- `https://www.keil.com/pack/Keil.STM32L4xx_DFP.pdsc` returned `200`
- `https://www.keil.com/pack/Keil.STM32L4xx_DFP.3.1.0.pack` returned `200`

The failure is the all-vendors live update path dropping descriptors silently,
not the repo's board mapping and not a broken direct STM32L4 download URL.

## Verified repair command

The repo now ships `pyocd-pack-repair`, which:

1. downloads the master `index.pidx`
2. selects missing or requested descriptors
3. fetches those PDSCs one-by-one with retries
4. rebuilds the local cmsis-pack-manager `index.json` from every cached PDSC

Repair just the exact STM32L4 DFP:

```powershell
uv run pyocd-pack-repair --vendor Keil --pack-name STM32L4xx_DFP
uv run pyocd pack find stm32l4
```

Repair every missing descriptor currently absent from the local cache:

```powershell
uv run pyocd-pack-repair
```

## Diagnosis commands

```powershell
# Where the cache lives + how many devices the built index actually has:
uv run python -c "import cmsis_pack_manager as c; cache=c.Cache(True,False); idx=cache.index; print('devices:', len(idx)); print('data_path:', cache.data_path); print('STM32L4:', sum(1 for k in idx if k.upper().startswith('STM32L4')))"

# Is the family even present?
uv run pyocd pack find stm32
uv run pyocd pack find stm32l4

# Count cached descriptors:
Get-ChildItem -Recurse -File \"$env:LOCALAPPDATA\\cmsis-pack-manager\\cmsis-pack-manager\" | Where-Object {$_.Extension -eq '.pdsc'} | Measure-Object
```

## Other recovery options

1. Prefer the pinned-pack path for repo-owned targets:
   `uv run python host_bootstrap.py --install-packs`
2. Run `uv run pyocd-pack-repair` instead of retrying the bulk updater.
3. If you still want to try the upstream updater again, clear the cache first:

```powershell
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\cmsis-pack-manager\cmsis-pack-manager"
uv run pyocd pack update
```

## Why the shipped runtime still avoids the live index

The MCP server and Stage 0 path should not depend on a many-host, silent-failure
pack-index refresh to support a single board. That is why the runtime continues
to load exact `.pack` files from `packs/manifest.yaml`, while this document and
`pyocd-pack-repair` exist as a repair tool for benches that still use pyOCD's
interactive pack-index commands.
