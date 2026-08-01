"""Focused regressions for the round-2 trusted-caller policy changes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from elftools.common.exceptions import ELFError

from pyocd_debug_mcp.kernel.operations import operation_timeout_seconds
from pyocd_debug_mcp.native_build import _validate_declared_artifacts, _validate_paths
from pyocd_debug_mcp.probe_families import (
    ProbeFamilyRegistryError,
    load_probe_family_registry,
    provider_qualified_family,
)
from pyocd_debug_mcp.safety.linker import (
    BuildArtifactSelection,
    BuildRole,
    LinkerEvidenceError,
)
from pyocd_debug_mcp.services.symbols import SymbolLookupError, find_symbols
from pyocd_debug_mcp.setup_flow.targets import TargetResolutionError, TargetResolver


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
    def test_default_uart_finalizer_timeout_matches_explicit_one_second(self) -> None:
        base = {"timeout_seconds": 31.0}
        self.assertEqual(
            operation_timeout_seconds(
                "write_serial",
                {**base, "on_exit": {"action": "uart_write", "text": "exit"}},
            ),
            operation_timeout_seconds(
                "write_serial",
                {
                    **base,
                    "on_exit": {
                        "action": "uart_write",
                        "text": "exit",
                        "timeout_seconds": 1.0,
                    },
                },
            ),
        )

    def test_symbol_search_returns_every_sorted_match_without_a_hidden_limit(self) -> None:
        symbols = [_Symbol(f"Match_{index:02d}", 0x2000 + (30 - index)) for index in range(25)]
        with tempfile.TemporaryDirectory() as directory:
            elf_path = Path(directory) / "symbols.elf"
            elf_path.write_bytes(b"not parsed by the fake reader")
            with patch(
                "pyocd_debug_mcp.services.symbols.ELFBinaryFile",
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

    def test_long_configuration_and_provider_ids_keep_their_grammar_and_uniqueness(self) -> None:
        configuration_id = "build-" + "release_" * 20
        self.assertGreater(len(configuration_id), 128)
        selection = BuildArtifactSelection(
            configuration_id, BuildRole.APPLICATION, Path("firmware.elf")
        )
        self.assertEqual(selection.configuration_id, configuration_id)
        with self.assertRaises(LinkerEvidenceError):
            BuildArtifactSelection("invalid id", BuildRole.APPLICATION, Path("firmware.elf"))

        provider_id = "provider_" + "trusted_" * 10
        self.assertGreater(len(provider_id), 64)
        document = {
            "schema_version": 1,
            "cli_fallback": {
                "executable": "pyocd",
                "executable_env": "PYOCD_EXE",
                "inventory_argv": [["list"]],
            },
            "families": [
                {"provider_id": provider_id, "label": "Provider", "text_aliases": ["probe"]}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe_families.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            registry = load_probe_family_registry(path)
            self.assertEqual(registry.families[0].provider_id, provider_id)

            document["families"].append(
                {
                    "provider_id": provider_id.upper(),
                    "label": "Duplicate",
                    "text_aliases": ["duplicate"],
                }
            )
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ProbeFamilyRegistryError):
                load_probe_family_registry(path)

            document["families"] = [
                {"provider_id": "invalid.id", "label": "Invalid", "text_aliases": ["probe"]}
            ]
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ProbeFamilyRegistryError):
                load_probe_family_registry(path)

        self.assertEqual(provider_qualified_family(f"{provider_id}:opaque:unique:id"), provider_id)

    def test_stable_directory_link_is_allowed_but_retarget_and_escape_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            target = root / "build-target"
            replacement = root / "replacement"
            link = root / "build-link"
            project.mkdir()
            target.mkdir()
            replacement.mkdir()
            try:
                os.symlink(target, link, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symbolic links are unavailable: {exc}")

            _, build_dir = _validate_paths(str(project), str(link))
            self.assertEqual(build_dir, link)
            captured_root = build_dir.resolve(strict=True)
            self.assertEqual(captured_root, target.resolve())
            self.assertEqual(
                _validate_declared_artifacts(build_dir, {}, expected_root=captured_root), {}
            )

            outside = root / "outside.bin"
            outside.write_bytes(b"outside")
            with self.assertRaisesRegex(RuntimeError, "outside the captured build root"):
                _validate_declared_artifacts(
                    build_dir,
                    {"custom": str(outside)},
                    expected_root=captured_root,
                )

            link.unlink()
            os.symlink(replacement, link, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "replaced or redirected"):
                _validate_declared_artifacts(build_dir, {}, expected_root=captured_root)


if __name__ == "__main__":
    unittest.main()
