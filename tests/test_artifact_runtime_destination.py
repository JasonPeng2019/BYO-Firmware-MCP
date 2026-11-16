"""Real-filesystem collector destination regressions; no path resolver shims."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.tools.artifacts import build_artifact_handlers
from tests.elf_symbol_fixture import ElfSymbol, elf32_symbols

import pytest


class _CollectionCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project = self.root / "project"
        self.runtime = self.root / "Runtime"
        self.project.mkdir()
        self.runtime.mkdir()
        (self.runtime / "release-manifest.json").write_bytes(b"immutable-runtime-sentinel\n")
        self.inputs = self.project / "build"
        self.inputs.mkdir()
        self.elf = self.inputs / "app.elf"
        self.map = self.inputs / "app.map"
        self.elf.write_bytes(elf32_symbols([ElfSymbol("collection_value", 0x20000024)]))
        self.map.write_bytes(b".data 0x20000000 0x200\n 0x20000024 collection_value\n")
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.runtime)
        environment = patch.dict(
            os.environ,
            {"BYO_MCP_ARTIFACT_ROOT": str(self.project), "BYO_RUNTIME_ROOT": str(self.runtime)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.collect = build_artifact_handlers()["collect_build_artifacts"]

    def _inventory(self, root: Path) -> dict[str, str | None]:
        return {
            path.relative_to(root).as_posix(): (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            )
            for path in root.rglob("*")
        }

    def _assert_refusal(
        self,
        destination: Path | str,
        *,
        runtime: Path | str | None = None,
        message: str = "runtime",
    ) -> None:
        before = self._inventory(self.root)
        # These are mutation tripwires, not replacement path/collector implementations.
        with (
            patch.dict(os.environ, {"BYO_RUNTIME_ROOT": str(runtime or self.runtime)}),
            patch.object(
                Path, "mkdir", side_effect=AssertionError("mkdir before refusal")
            ) as mkdir,
            patch(
                "pyocd_debug_mcp.artifact_collector.shutil.copyfile",
                side_effect=AssertionError("copy before refusal"),
            ) as copy,
        ):
            reply = json.loads(
                self.collect(
                    output_dir=str(destination),
                    elf_path="build/app.elf",
                    map_path="build/app.map",
                    expected_roles=["elf", "map"],
                )
            )
        self.assertEqual(reply["status"], "artifact_collection_refused", reply)
        self.assertIn(message, reply["message"])
        mkdir.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(self._inventory(self.root), before)

    def _assert_collection(self, destination: Path | str, actual: Path) -> None:
        before = self._inventory(self.runtime)
        reply = json.loads(
            self.collect(
                output_dir=str(destination),
                elf_path="build/app.elf",
                map_path="build/app.map",
                expected_roles=["elf", "map"],
            )
        )
        self.assertEqual(reply["status"], "artifacts_collected", reply)
        self.assertTrue(Path(reply["output_dir"]).samefile(actual))
        manifest_path = Path(reply["manifest_path"])
        self.assertTrue(manifest_path.samefile(actual / "build-manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["present_roles"]), {"elf", "map"})
        self.assertEqual(manifest["producer"], "mcp-native-build")
        for role, source in (("elf", self.elf), ("map", self.map)):
            target = Path(reply["canonical_paths"][role])
            self.assertTrue(target.samefile(actual / f"firmware.{role}"))
            self.assertEqual(target.read_bytes(), source.read_bytes())
            record = reply["artifacts"][role]
            self.assertEqual(record["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(record["size_bytes"], source.stat().st_size)
            self.assertTrue((actual / record["path"]).samefile(target))
            self.assertEqual(manifest["artifacts"][role]["sha256"], record["sha256"])
        self.assertEqual(reply["authority"], "provenance_only")
        self.assertFalse(reply["safety_handoff"]["opens_hardware_gate"])
        self.assertFalse(reply["safety_handoff"]["manifest_consumed_automatically"])
        self.assertEqual(self._inventory(self.runtime), before)


class ArtifactRuntimeDestinationTests(_CollectionCase):
    @pytest.mark.mvp
    def test_runtime_itself_existing_child_and_missing_parents_refused_before_writes(self) -> None:
        existing = self.runtime / "empty"
        existing.mkdir()
        for destination in (
            self.runtime,
            existing,
            self.runtime / "bundle",
            self.runtime / "missing" / "parent" / "bundle",
        ):
            with self.subTest(destination=destination):
                self._assert_refusal(destination)

    @pytest.mark.mvp
    def test_external_project_collection_returns_matching_elf_map_paths_and_bytes(self) -> None:
        self._assert_collection("dist/bundle", self.project / "dist" / "bundle")
        self.assertFalse((self.runtime / "dist").exists())
