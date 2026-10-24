"""Real-filesystem collector destination regressions; no path resolver shims."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp.artifact_collector import ArtifactRole, _validate_destination, main
from pyocd_debug_mcp.tools.artifacts import build_artifact_handlers
from tests.elf_symbol_fixture import ElfSymbol, elf32_symbols


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

    def _directory_link(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            if os.name != "nt":
                self.skipTest(f"directory symlink prerequisite unavailable: {exc}")
            junction = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if junction.returncode:
                self.skipTest(
                    f"directory link prerequisite unavailable: symlink={exc}; "
                    f"junction exit={junction.returncode}: {junction.stdout} {junction.stderr}"
                )
        self.assertTrue(link.samefile(target))


class ArtifactRuntimeDestinationTests(_CollectionCase):
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

    def test_relative_and_dot_segment_runtime_destinations_refused(self) -> None:
        for destination in ("../Runtime/new/bundle", self.runtime / ".." / "Runtime/new/bundle"):
            with self.subTest(destination=destination):
                self._assert_refusal(destination)
        self.assertFalse((self.runtime / "new").exists())

    def test_external_project_collection_returns_matching_elf_map_paths_and_bytes(self) -> None:
        self._assert_collection("dist/bundle", self.project / "dist" / "bundle")
        self.assertFalse((self.runtime / "dist").exists())

    def test_runtime_name_prefix_sibling_is_a_valid_destination(self) -> None:
        sibling = self.root / "Runtime-other" / "bundle"
        self._assert_collection(sibling, sibling)

    @unittest.skipIf(os.name == "nt", "POSIX case-sensitive filesystem control")
    def test_posix_distinct_case_directory_remains_external(self) -> None:
        different = self.root / "runtime"
        if different.exists():
            self.skipTest("case-sensitive directory names are unavailable on this filesystem")
        destination = different / "bundle"
        self._assert_collection(destination, destination)

    def test_links_to_runtime_and_runtime_root_alias_refused(self) -> None:
        link = self.project / "runtime-link"
        self._directory_link(link, self.runtime)
        self._assert_refusal(link / "missing" / "bundle")
        self._assert_refusal(self.runtime / "new" / "bundle", runtime=link)

    def test_empty_external_directory_link_remains_valid(self) -> None:
        target = self.root / "empty-external"
        target.mkdir()
        link = self.project / "output-link"
        self._directory_link(link, target)
        self._assert_collection(link, target)

    def test_other_destination_protections_still_refuse_before_writes(self) -> None:
        nonempty = self.project / "nonempty"
        nonempty.mkdir()
        (nonempty / "keep").write_bytes(b"keep")
        file = self.project / "not-a-directory"
        file.write_bytes(b"keep")
        for destination, message in (
            (Path(self.root.anchor), "filesystem root"),
            (Path.home(), "user's home"),
            (self.project / ".firm" / "new" / "bundle", "FirmStore"),
            (nonempty, "absent or empty"),
            (file, "not a directory"),
            (self.inputs, "absent or empty"),
        ):
            with self.subTest(destination=destination):
                self._assert_refusal(destination, message=message)

    def test_destination_helper_retains_normalized_source_within_output_protection(self) -> None:
        empty = self.project / "empty"
        empty.mkdir()
        # The public collector normalizes sources before calling this private helper.
        sources = {ArtifactRole.ELF: (empty / "inside.elf").resolve()}
        before = self._inventory(self.root)
        with self.assertRaisesRegex(ValueError, "sources cannot be inside"):
            _validate_destination(empty, sources)
        self.assertEqual(self._inventory(self.root), before)

    def test_source_cli_retains_cwd_even_with_selected_project(self) -> None:
        cwd = self.root / "cli-project"
        cwd.mkdir()
        source = cwd / "app.elf"
        source.write_bytes(elf32_symbols([ElfSymbol("cli_value", 0x20000028)]))
        before = self._inventory(self.runtime)
        os.chdir(cwd)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--elf", "app.elf", "--output-dir", "bundle"]), 0)
        reply = json.loads(output.getvalue())
        self.assertEqual(reply["status"], "artifacts_collected")
        actual = cwd / "bundle"
        self.assertTrue(Path(reply["output_dir"]).samefile(actual))
        self.assertTrue(Path(reply["manifest_path"]).samefile(actual / "build-manifest.json"))
        self.assertEqual((actual / "firmware.elf").read_bytes(), source.read_bytes())
        self.assertFalse((self.project / "bundle").exists())
        self.assertEqual(self._inventory(self.runtime), before)


@unittest.skipUnless(os.name == "nt", "requires native Windows filesystem path semantics")
class WindowsArtifactRuntimeDestinationTests(_CollectionCase):
    def _spellings(self, path: Path) -> tuple[str, str]:
        ordinary = str(path)
        if ordinary.startswith("\\\\?\\UNC\\"):
            ordinary = "\\\\" + ordinary[8:]
        elif ordinary.startswith("\\\\?\\"):
            ordinary = ordinary[4:]
        if ordinary.startswith("\\\\"):
            return ordinary, "\\\\?\\UNC\\" + ordinary[2:]
        return ordinary, "\\\\?\\" + ordinary

    def test_native_drive_canonical_verbatim_existing_and_missing_destinations(self) -> None:
        existing = self.runtime / "empty"
        existing.mkdir()
        runtime_spellings = self._spellings(self.runtime)
        self.assertTrue(Path(runtime_spellings[0]).samefile(runtime_spellings[1]))
        for runtime in runtime_spellings:
            for destination in (self.runtime, existing, self.runtime / "new" / "parent" / "bundle"):
                for spelling in self._spellings(destination):
                    with self.subTest(runtime=runtime, destination=spelling):
                        self._assert_refusal(spelling, runtime=runtime)
        self.assertFalse((self.runtime / "new").exists())

    def test_native_case_and_forward_slash_spellings_refused(self) -> None:
        runtime, verbatim_runtime = self._spellings(self.runtime)
        for destination in (
            runtime.swapcase() + "\\missing\\bundle",
            runtime.replace("\\", "/") + "/missing/bundle",
        ):
            with self.subTest(destination=destination):
                self._assert_refusal(destination, runtime=verbatim_runtime)

    def test_native_verbatim_external_sibling_collection_remains_valid(self) -> None:
        destination = self.root / "Runtime-other" / "bundle"
        ordinary_runtime, verbatim_runtime = self._spellings(self.runtime)
        self.assertTrue(Path(ordinary_runtime).samefile(verbatim_runtime))
        with patch.dict(os.environ, {"BYO_RUNTIME_ROOT": verbatim_runtime}):
            self._assert_collection(self._spellings(destination)[1], destination)

    def test_native_junction_to_runtime_refused(self) -> None:
        link = self.project / "runtime-junction"
        junction = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(self.runtime)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if junction.returncode:
            self.skipTest(
                f"junction prerequisite unavailable: exit={junction.returncode}: "
                f"{junction.stdout} {junction.stderr}"
            )
        self.assertTrue(link.samefile(self.runtime))
        self._assert_refusal(link / "missing" / "bundle")

    def test_native_unc_and_verbatim_unc_runtime_destinations(self) -> None:
        ordinary_root = self._spellings(self.root)[0]
        drive, tail = os.path.splitdrive(ordinary_root)
        if len(drive) != 2 or drive[1] != ":":
            self.skipTest(f"local drive prerequisite for loopback administrative share: {drive!r}")
        unc_root = Path("\\\\localhost\\" + drive[0] + "$" + tail)
        try:
            same_root = unc_root.samefile(self.root)
        except OSError as exc:
            self.skipTest(f"loopback administrative share prerequisite unavailable: {exc}")
        self.assertTrue(same_root, "UNC fixture must name the actual temporary directory")
        unc_runtime = unc_root / self.runtime.name
        for runtime in (*self._spellings(unc_runtime), *self._spellings(self.runtime)):
            for destination in self._spellings(unc_runtime / "missing" / "bundle"):
                with self.subTest(runtime=runtime, destination=destination):
                    self._assert_refusal(destination, runtime=runtime)
