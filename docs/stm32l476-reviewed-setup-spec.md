# STM32L476 Reviewed Fresh-Setup Specification

## Problem

The repository contains an STM32L476RG catalog entry and pinned local CMSIS-Pack, but the entry has
no reviewed datasheet or runtime-evidence anchors. Fresh setup therefore cannot create a safety map
for the attached NUCLEO-L476RG.

## Required behavior

- Pin the supplied STM32L476xx DS10198 Rev 11 PDF by server-computed SHA-256 and exact
  `STM32L476RGT6` package association.
- Add independent repository evidence for flash, SRAM1, erase geometry, system memory, OTP/option
  bytes, peripheral space, and Cortex-M system space.
- Bind device-support evidence to pyOCD 0.45.0 and the already-pinned
  `Keil.STM32L4xx_DFP.3.1.0.pack` bytes. Do not download or trust the live pack index.
- Extend runtime evidence verification to accept either the existing built-in target-module pin or
  an explicit catalog CMSIS-Pack filename/hash pin. Exactly one mode must be configured.
- Keep caller-supplied ranges prohibited and keep setup/gate/permission/plan state run-scoped.
- Retain the existing standard full-flash application policy. Do not globally reserve a bootloader
  partition or make ordinary firmware link at a nonstandard address. This acceptance may place a
  test boot image and offset application within the already-reviewed full application envelope,
  but it does not claim persistent protected-bootloader authority.

## Safety and usability

Normal full-flash STM32 firmware remains the default. Pack-backed support is a general evidence
mode usable by other CMSIS-Pack targets without changing setup calls or adding a user step.
