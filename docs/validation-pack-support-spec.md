# Validation pack-support specification

## Problem

Fresh reviewed setup can authorize and connect a CMSIS-Pack-backed target, yet the immediately
following validation rejects that same target because validation consults only pyOCD built-ins and
the fresh project's empty pack manifest. Repository-pinned pack authority is therefore lost between
setup and validation.

## Required behavior

- Validation accepts a target when it is a pyOCD built-in or when the existing single-provider
  verified-pack selector returns a pinned, digest-verified local pack for that exact target.
- A manifest declaration alone, a process-global target registered from another pack, missing pack
  bytes, changed bytes, or ambiguous providers never establish validation support.
- The check remains local-only and adds no setup step, download, user choice, or board-specific
  special case.
- Existing built-in targets retain their current zero-pack path.

## Acceptance

Focused tests prove built-in acceptance, verified repository-pack acceptance, and refusal for
missing or invalid pack authority. A fresh STM32 setup must then pass validation without changing
normal firmware or debugging workflows.
