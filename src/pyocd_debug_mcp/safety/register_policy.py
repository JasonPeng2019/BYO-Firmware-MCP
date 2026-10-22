"""Name-derived exclusions for CPU and SVD peripheral register writes."""

from __future__ import annotations

import re


PROHIBITED_TERMS = (
    "secure",
    "security",
    "provision",
    "lifecycle",
    "option",
    "otp",
    "fuse",
    "protect",
    "lock",
    "auth",
    "key",
    "sau",
)


def prohibited_svd_register(peripheral: str, register: str = "") -> bool:
    """Keep security/control spans out of ordinary MMIO write authority.

    CMSIS-SVD access describes hardware writability, not permission. Names are
    metadata rather than a universal proof of vendor semantics. Flash controls
    default to excluded; explicitly recognizable status/latency registers remain
    useful. The flag must be applied before aliases lose their individual names.
    """

    qualified = f"{peripheral}.{register}".casefold()
    # Do not interpret ordinary clock configuration as a protection lock.
    if any(term in qualified.replace("clock", "") for term in PROHIBITED_TERMS):
        return True
    if re.search(r"(?:^|[._])(?:rdp|wrp|pcrop|optcr|optr|fprot|fsec|uicr|ficr)", qualified):
        return True
    controller = peripheral.casefold()
    leaf = register.rsplit(".", 1)[-1].casefold()
    if controller.startswith(("flash", "nvm", "efc", "ftfa", "ftfl")):
        return re.fullmatch(r"(?:acr|sr|status|eccr)[0-9]*", leaf) is None
    if controller.startswith(("aes", "cryp", "crypto")):
        return re.fullmatch(r"k[0-9]+(?:l|h)?r?", leaf) is not None
    return False
