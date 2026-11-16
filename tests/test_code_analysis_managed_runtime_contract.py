"""Behavior contracts from the accepted target runtime mapping, independently.

Resolve through the production launch context, never a replacement resolver.
Valid controls precede failures to prevent an unresolved stub passing rejection
tests. Synthetic payloads prove only unit/protocol contracts, not distribution
completeness, Windows DLL closure, analyzer compatibility or installed acceptance.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.code_analysis_managed_support import ManagedCase, RuntimeFixture, default_mapping, protocol_redirect

RESOURCE = "analysis/clangd/lib/clang/23/include/stddef.h"
EXECUTABLE = default_mapping()["programs"]["clangd"]["executable"]


class PayloadIntegrity(ManagedCase):

    def test_missing_extra_wrong_bytes_and_wrong_inventory_are_rejected_before_spawn(self) -> None:
        with self.observer() as observed:
            for case in (
                "missing-resource",
                "missing-cfg",
                "empty-platforms",
                "missing-license",
                "extra-leaf",
                "wrong-data",
                "wrong-executable",
                "wrong-size",
                "wrong-hash",
                "missing-files-entry",
                "wrong-kind",
                "wrong-executable-flag",
            ):
                with self.subTest(case=case):
                    self.require_valid_control(observed)
                    self.fixture = RuntimeFixture(self.base / case)
                    self.context = self.fixture.context(self.project)
                    observed.redirect = protocol_redirect(self.fixture, self.fake)
                    name, hint = "clangd", "stddef.h"
                    if case == "missing-resource":
                        (self.fixture.root / RESOURCE).unlink()
                    elif case == "missing-cfg":
                        (self.fixture.root / "analysis/cppcheck/cfg/std.cfg").unlink()
                        name, hint = "cppcheck", "std.cfg"
                    elif case == "empty-platforms":
                        (self.fixture.root / "analysis/cppcheck/platforms/win64.xml").unlink()
                        name, hint = "cppcheck", "platforms"
                    elif case == "missing-license":
                        (self.fixture.root / "analysis/clangd/LICENSE.TXT").unlink()
                        hint = "LICENSE.TXT"
                    elif case == "extra-leaf":
                        self.fixture.put("analysis/clangd/lib/clang/23/include/extra.h", b"extra")
                        hint = "extra.h"
                    elif case in ("wrong-data", "wrong-executable"):
                        target = RESOURCE if case == "wrong-data" else EXECUTABLE
                        self.fixture.put(
                            target, (self.fixture.root / target).read_bytes() + b"changed"
                        )
                        hint = Path(target).name
                    elif case == "missing-files-entry":
                        self.fixture.inventory.remove(self.fixture.inventory_entry(RESOURCE))
                        self.fixture.save_manifest()
                    else:
                        key, value = {
                            "wrong-size": ("size", 99),
                            "wrong-hash": ("sha256", "0" * 64),
                            "wrong-kind": ("kind", "metadata"),
                            "wrong-executable-flag": ("executable", True),
                        }[case]
                        self.fixture.inventory_entry(RESOURCE)[key] = value
                        self.fixture.save_manifest()
                    self.assert_rejected_without_spawn(observed, hint, name)


if __name__ == "__main__":
    unittest.main()
