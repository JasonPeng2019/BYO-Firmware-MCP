"""Focused regressions for the round-2 trusted-caller policy changes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from elftools.common.exceptions import ELFError

from firmware_mcp.native_build import build_firmware
from firmware_mcp.firmstore.providers import ProviderRecipe
from firmware_mcp.services.symbols import SymbolLookupError, find_symbols
from firmware_mcp.setup_flow.targets import TargetResolutionError, TargetResolver


@dataclass(frozen=True)
class _Symbol:
    name: str
    address: int
    size: int = 4
    type: str = "STT_FUNC"


class _FakeElf:
    def __init__(self, symbols: list[_Symbol]) -> None:
        self.symbol_decoder = type(
            "Decoder", (), {"symbol_dict": {symbol.name: symbol for symbol in symbols}}
        )()

    def close(self) -> None:
        pass


class RoundTwoTrustedCallerTests(unittest.TestCase):
    def test_symbol_search_returns_every_sorted_match_without_a_hidden_limit(self) -> None:
        symbols = [_Symbol(f"Match_{index:02d}", 0x2000 + (30 - index)) for index in range(25)]
        with tempfile.TemporaryDirectory() as directory:
            elf_path = Path(directory) / "symbols.elf"
            elf_path.write_bytes(b"not parsed by the fake reader")
            with patch(
                "firmware_mcp.services.symbols.ELFBinaryFile",
                return_value=_FakeElf(symbols),
            ):
                matches = find_symbols(elf_path, "match")

            self.assertEqual(len(matches), 25)
            self.assertEqual([item.name for item in matches], sorted(item.name for item in symbols))

        with self.assertRaises(SymbolLookupError):
            find_symbols("unused.elf", " ")
        with self.assertRaises(SymbolLookupError):
            find_symbols("missing.elf", "match")

    def test_symbol_search_reports_real_malformed_elf_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.elf"
            malformed.write_bytes(b"not an ELF")

            with self.assertRaisesRegex(ELFError, "Magic number does not match"):
                find_symbols(malformed, "match")

    def test_long_reviewed_target_is_admitted_only_by_exact_supported_membership(self) -> None:
        candidate = "stm32u5_" + "long_target_" * 12
        self.assertGreater(len(candidate), 128)
        self.assertEqual(
            TargetResolver.validate_candidate(
                candidate,
                expected_target=candidate,
                built_in_targets=(candidate,),
            ),
            "built_in",
        )
        with self.assertRaises(TargetResolutionError) as mismatch:
            TargetResolver.validate_candidate(
                candidate,
                expected_target=f"{candidate}_other",
                built_in_targets=(candidate,),
            )
        self.assertEqual(mismatch.exception.code, "target/reviewed-mapping-mismatch")
        with self.assertRaises(TargetResolutionError) as missing:
            TargetResolver.validate_candidate(
                candidate,
                expected_target=candidate,
                built_in_targets=(),
            )
        self.assertEqual(missing.exception.code, "target/support-missing")

    def test_long_provider_ids_are_recipe_data_not_a_source_registry(self) -> None:
        provider_id = "provider_" + "trusted_" * 10
        self.assertGreater(len(provider_id), 64)
        recipe = ProviderRecipe.from_record(
            {
                "provider_id": provider_id,
                "inventory_argv": ["invent", "connections"],
                "worker_argv": ["invent", "worker"],
            }
        )
        self.assertEqual(recipe.provider_id, provider_id)

    def test_direct_build_accepts_an_explicit_artifact_outside_the_build_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            build_dir = root / "build"
            external_artifact = root / "firmware.bin"
            project.mkdir()
            external_artifact.write_bytes(b"firmware")

            result = build_firmware(
                str(project),
                str(build_dir),
                [sys.executable, "-c", "print('build complete')"],
                artifacts={"firmware": str(external_artifact)},
                timeout_seconds=10,
            )

            self.assertEqual(result["status"], "build_succeeded")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(
                result["artifacts"],
                [
                    {
                        "role": "firmware",
                        "path": str(external_artifact.resolve()),
                        "format": "bin",
                        "size_bytes": len(b"firmware"),
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
