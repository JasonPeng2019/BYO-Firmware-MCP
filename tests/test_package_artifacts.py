from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import psutil

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI lane
    import tomli as tomllib

from scripts.verify_release_artifacts import (
    _assert_pair_digest_matches,
    _publish_verified_pair,
    _release_artifacts,
    _snapshot_release_pair,
    _verify_generated_metadata_alignment,
    _verify_source_alignment,
)
import scripts.verify_release_artifacts as release_verifier


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.8rc1"
CAN_MODULES = {
    "pyocd_debug_mcp/adapters/can_interface.py",
    "pyocd_debug_mcp/adapters/can_pythoncan.py",
    "pyocd_debug_mcp/adapters/can_socketcan.py",
    "pyocd_debug_mcp/can_capabilities.py",
    "pyocd_debug_mcp/can_config.py",
    "pyocd_debug_mcp/can_errors.py",
    "pyocd_debug_mcp/can_secure_files.py",
    "pyocd_debug_mcp/can_startup.py",
    "pyocd_debug_mcp/guardrails/can_gate.py",
    "pyocd_debug_mcp/services/can_session.py",
    "pyocd_debug_mcp/services/can_worker.py",
    "pyocd_debug_mcp/tools/can.py",
}


class ReleaseArtifactInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="byo-package-test-")
        cls.dist = Path(cls._temporary.name)
        subprocess.run(
            ["uv", "build", "--out-dir", str(cls.dist)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        cls.wheel = next(cls.dist.glob("*.whl"))
        cls.sdist = next(cls.dist.glob("*.tar.gz"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_wheel_has_current_metadata_and_runtime_resources(self) -> None:
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
            metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
            metadata = BytesParser().parsebytes(archive.read(metadata_name))
            notice = archive.read("pyocd_debug_mcp/licenses/THIRD_PARTY_NOTICES.md").decode("utf-8")
            apache_license = archive.read("pyocd_debug_mcp/licenses/Apache-2.0.txt").decode("utf-8")

        self.assertEqual(metadata["Version"], VERSION)
        self.assertIn(
            "Repository, https://github.com/JasonPeng2019/BYO-Firmware-MCP",
            metadata.get_all("Project-URL", []),
        )
        self.assertIn(
            "Issues, https://github.com/JasonPeng2019/BYO-Firmware-MCP/issues",
            metadata.get_all("Project-URL", []),
        )
        self.assertIsNone(metadata["License-Expression"])
        self.assertIn("can", metadata.get_all("Provides-Extra", []))
        requirements = metadata.get_all("Requires-Dist", [])
        self.assertTrue(
            any("python-can==4.6.1" in item and "extra == 'can'" in item for item in requirements)
        )
        self.assertTrue(
            any(
                "pyroute2==0.9.6" in item
                and "sys_platform == 'linux'" in item
                and "extra == 'can'" in item
                for item in requirements
            )
        )
        self.assertTrue(CAN_MODULES.issubset(names))
        self.assertIn("pyocd_debug_mcp/examples/can/virtual_rx_only.yaml", names)
        self.assertIn("pyocd_debug_mcp/licenses/Apache-2.0.txt", names)
        self.assertIn("pyocd_debug_mcp/licenses/Python.txt", names)
        self.assertIn("pyocd_debug_mcp/licenses/THIRD_PARTY_NOTICES.md", names)
        self.assertIn("pyocd_debug_mcp/docs/SERVER_GUIDE.md", names)
        self.assertIn("pyocd_debug_mcp/docs/client-contract.md", names)
        self.assertIn("pyocd_debug_mcp/docs/server-reference.md", names)
        self.assertIn("pyocd_debug_mcp/docs/RELEASE_STATUS.md", names)
        self.assertIn("Firmware-CLI repository at commit\n`0e8a45d`", notice)
        self.assertIn("Apache License", apache_license)

    def test_can_extra_smoke_is_verified_for_both_distribution_formats(self) -> None:
        with (
            patch.object(release_verifier, "_verify_source_alignment"),
            patch.object(release_verifier, "_verify_generated_metadata_alignment"),
            patch.object(release_verifier, "_verify_base_install"),
            patch.object(release_verifier, "_verify_can_extra") as verify_extra,
        ):
            release_verifier._verify_pair(
                self.dist,
                self.wheel,
                self.sdist,
                reference_wheel=self.wheel,
                reference_sdist=self.sdist,
            )

        self.assertEqual(
            [(call.args[1], call.args[2]) for call in verify_extra.call_args_list],
            [(self.wheel, "wheel"), (self.sdist, "sdist")],
        )

    def test_sdist_has_current_metadata_source_docs_and_provenance(self) -> None:
        prefix = f"pyocd_debug_mcp-{VERSION}/"
        with tarfile.open(self.sdist, "r:gz") as archive:
            names = {member.name for member in archive.getmembers() if member.isfile()}
            pyproject = archive.extractfile(prefix + "pyproject.toml")
            self.assertIsNotNone(pyproject)
            assert pyproject is not None
            project_text = pyproject.read().decode("utf-8")

        self.assertIn(f'version = "{VERSION}"', project_text)
        expected = {
            prefix + "README.md",
            prefix + "uv.lock",
            prefix + "SERVER_GUIDE.md",
            prefix + "THIRD_PARTY_NOTICES.md",
            prefix + "LICENSES/Apache-2.0.txt",
            prefix + "LICENSES/Python.txt",
            prefix + "fixtures/can/virtual_rx_only.yaml",
            prefix + "docs/client-contract.md",
            prefix + "docs/server-reference.md",
            prefix + "docs/RELEASE_STATUS.md",
            prefix + "client-workspace/.gitignore",
            prefix + "client-workspace/README.md",
            prefix + "client-workspace/bin/attach-project",
            prefix + "scripts/verify_release_artifacts.py",
            prefix + "client-workspace/modes/firmware.toml",
            prefix + "client-workspace/skills-src/firmware/downgrade/SKILL.md",
            prefix + "client-workspace/skills-src/firmware/downgrade/agents/openai.yaml",
            prefix + "client-workspace/skills-src/firmware/mass-erase/SKILL.md",
            prefix + "client-workspace/skills-src/firmware/mass-erase/agents/openai.yaml",
            prefix + "client-workspace/skills-src/firmware/byo-issue-report/SKILL.md",
            prefix + "client-workspace/skills-src/firmware/byo-issue-report/agents/openai.yaml",
            prefix
            + "client-workspace/skills-src/firmware/byo-issue-report/templates/issue-report.json",
            prefix
            + "client-workspace/skills-src/firmware/byo-issue-report/templates/routine-checkin.json",
            prefix + "skills/byo-issue-report/SKILL.md",
            prefix + "skills/byo-issue-report/agents/openai.yaml",
            prefix + "skills/byo-issue-report/templates/issue-report.json",
            prefix + "skills/byo-issue-report/templates/routine-checkin.json",
        }
        expected.update(prefix + "src/" + path for path in CAN_MODULES)
        self.assertTrue(expected.issubset(names), sorted(expected - names))

    def test_client_workspace_is_an_explicit_raw_sdist_source_handoff(self) -> None:
        prefix = f"pyocd_debug_mcp-{VERSION}/"
        with zipfile.ZipFile(self.wheel) as wheel_archive:
            self.assertFalse(any("client-workspace/" in name for name in wheel_archive.namelist()))
        with tarfile.open(self.sdist, "r:gz") as sdist_archive:
            names = set(sdist_archive.getnames())
        self.assertIn(prefix + "client-workspace/bin/attach-project", names)
        readme = (ROOT / "README.md").read_text(encoding="utf-8-sig")
        self.assertIn("source handoff", readme)
        self.assertIn("does not attach those client assets", readme)

    def test_source_and_lock_versions_match_the_release(self) -> None:
        source_namespace: dict[str, object] = {}
        exec(
            (ROOT / "src/pyocd_debug_mcp/__init__.py").read_text(encoding="utf-8"),
            source_namespace,
        )
        self.assertEqual(source_namespace["__version__"], VERSION)

        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        project = next(
            package for package in lock["package"] if package["name"] == "pyocd-debug-mcp"
        )
        self.assertEqual(project["version"], VERSION)

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["build-system"]["requires"], ["hatchling==1.32.4"])

        workflow = (ROOT / ".github/workflows/software-can.yml").read_text(encoding="utf-8")
        self.assertIn(f"name: pyocd-debug-mcp-{VERSION}", workflow)

        upload_step = workflow.split("- name: Retain exact verified release handoff bytes", 1)[
            1
        ].split("\n  native-can-simulation:", 1)[0]
        self.assertEqual(upload_step.count("retention-days: 30"), 1)
        self.assertIn(
            "uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            upload_step,
        )
        self.assertIn(f"name: pyocd-debug-mcp-{VERSION}", upload_step)
        self.assertIn("path: release-dist/", upload_step)
        self.assertIn("if-no-files-found: error", upload_step)
        self.assertIn("retention-days: 30", upload_step)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_verifier_runner_retires_spawned_descendants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-verifier-descendant-") as temporary:
            root = Path(temporary)
            pid_path = root / "descendant.pid"
            script = (
                "import subprocess,sys; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"Path({str(pid_path)!r}).write_text(str(p.pid), encoding='ascii')"
            )
            release_verifier._run([sys.executable, "-c", script], cwd=root)
            descendant_pid = int(pid_path.read_text(encoding="ascii"))
            deadline = time.monotonic() + 5.0
            while psutil.pid_exists(descendant_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(
                psutil.pid_exists(descendant_pid),
                "release verifier left a spawned descendant alive",
            )

    def test_archives_match_every_current_packaged_source_file(self) -> None:
        _verify_source_alignment(self.wheel, self.sdist)

    def test_same_version_stale_wheel_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-stale-wheel-") as temporary:
            stale = Path(temporary) / self.wheel.name
            with zipfile.ZipFile(self.wheel) as source, zipfile.ZipFile(stale, "w") as destination:
                for info in source.infolist():
                    content = source.read(info.filename)
                    if info.filename == "pyocd_debug_mcp/docs/RELEASE_STATUS.md":
                        content = b"stale same-version release status\n"
                    destination.writestr(info, content)

            with self.assertRaisesRegex(
                RuntimeError, "stale metadata|does not match current source"
            ):
                _verify_source_alignment(stale, self.sdist)

            stale_entry_points = Path(temporary) / ("entry-points-" + self.wheel.name)
            with (
                zipfile.ZipFile(self.wheel) as source,
                zipfile.ZipFile(stale_entry_points, "w") as destination,
            ):
                for info in source.infolist():
                    content = source.read(info.filename)
                    if info.filename.endswith(".dist-info/entry_points.txt"):
                        content = b"[console_scripts]\npyocd-debug-mcp = missing.module:main\n"
                    destination.writestr(info, content)

            with self.assertRaisesRegex(RuntimeError, "does not match current console scripts"):
                _verify_source_alignment(stale_entry_points, self.sdist)

    def test_same_version_archives_with_deleted_source_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-stale-extra-") as temporary:
            root = Path(temporary)
            stale_wheel = root / self.wheel.name
            with (
                zipfile.ZipFile(self.wheel) as source,
                zipfile.ZipFile(stale_wheel, "w") as destination,
            ):
                for info in source.infolist():
                    destination.writestr(info, source.read(info.filename))
                destination.writestr(
                    "pyocd_debug_mcp/stale_deleted_module.py",
                    b"raise RuntimeError('obsolete code')\n",
                )
            with self.assertRaisesRegex(RuntimeError, "inventory differs from the current package"):
                _verify_source_alignment(stale_wheel, self.sdist)

            stale_sdist = root / self.sdist.name
            prefix = f"pyocd_debug_mcp-{VERSION}/"
            with (
                tarfile.open(self.sdist, "r:gz") as source,
                tarfile.open(stale_sdist, "w:gz") as destination,
            ):
                for member in source.getmembers():
                    extracted = source.extractfile(member) if member.isfile() else None
                    destination.addfile(member, extracted)
                content = b"raise RuntimeError('obsolete code')\n"
                member = tarfile.TarInfo(prefix + "src/pyocd_debug_mcp/stale_deleted_module.py")
                member.mode = 0o644
                member.size = len(content)
                destination.addfile(member, io.BytesIO(content))
            with self.assertRaisesRegex(RuntimeError, "inventory differs from the current package"):
                _verify_source_alignment(self.wheel, stale_sdist)

    def test_sdist_non_regular_members_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-unsafe-sdist-") as temporary:
            unsafe = Path(temporary) / self.sdist.name
            with (
                tarfile.open(self.sdist, "r:gz") as source,
                tarfile.open(unsafe, "w:gz") as destination,
            ):
                for member in source.getmembers():
                    extracted = source.extractfile(member) if member.isfile() else None
                    destination.addfile(member, extracted)
                link = tarfile.TarInfo(f"pyocd_debug_mcp-{VERSION}/rogue-link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside"
                destination.addfile(link)

            with self.assertRaisesRegex(RuntimeError, "non-regular archive member"):
                _verify_source_alignment(self.wheel, unsafe)

    def test_wheel_symlink_member_is_rejected_even_when_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-unsafe-wheel-") as temporary:
            unsafe = Path(temporary) / self.wheel.name
            target = "pyocd_debug_mcp/server.py"
            with zipfile.ZipFile(self.wheel) as source, zipfile.ZipFile(unsafe, "w") as destination:
                for info in source.infolist():
                    content = source.read(info.filename)
                    if info.filename == target:
                        info.external_attr = 0o120777 << 16
                    destination.writestr(info, content)

            with self.assertRaisesRegex(RuntimeError, "unexpected archive type metadata"):
                _verify_source_alignment(unsafe, self.sdist)

    def test_wheel_dos_member_without_regular_type_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-dos-wheel-") as temporary:
            unsafe = Path(temporary) / self.wheel.name
            target = "pyocd_debug_mcp/server.py"
            with zipfile.ZipFile(self.wheel) as source, zipfile.ZipFile(unsafe, "w") as destination:
                for info in source.infolist():
                    content = source.read(info.filename)
                    if info.filename == target:
                        info.create_system = 0
                        info.external_attr = 0o644 << 16
                    destination.writestr(info, content)

            with self.assertRaisesRegex(RuntimeError, "unexpected archive type metadata"):
                _verify_source_alignment(unsafe, self.sdist)

    def test_oversized_generated_wheel_member_is_rejected_before_content_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-oversized-wheel-") as temporary:
            unsafe = Path(temporary) / self.wheel.name
            with zipfile.ZipFile(self.wheel) as source, zipfile.ZipFile(unsafe, "w") as destination:
                for info in source.infolist():
                    content = source.read(info.filename)
                    if info.filename.endswith(".dist-info/METADATA"):
                        content = b"X" * (2 * 1024 * 1024 + 1)
                    destination.writestr(info, content)

            with self.assertRaisesRegex(RuntimeError, "declared size bound"):
                _verify_source_alignment(unsafe, self.sdist)

    def test_oversized_generated_sdist_member_is_rejected_before_body_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-oversized-sdist-") as temporary:
            unsafe = Path(temporary) / self.sdist.name
            oversized = b"X" * (2 * 1024 * 1024 + 1)
            with tarfile.open(unsafe, "w:gz") as destination:
                member = tarfile.TarInfo(f"pyocd_debug_mcp-{VERSION}/PKG-INFO")
                member.mode = 0o644
                member.size = len(oversized)
                destination.addfile(member, io.BytesIO(oversized))

            with self.assertRaisesRegex(RuntimeError, "declared size bound"):
                _verify_source_alignment(self.wheel, unsafe)

    def test_paired_tampered_core_metadata_is_rejected_against_fresh_build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-metadata-pair-") as temporary:
            root = Path(temporary)
            altered_wheel = root / self.wheel.name
            metadata_name = f"pyocd_debug_mcp-{VERSION}.dist-info/METADATA"
            record_name = f"pyocd_debug_mcp-{VERSION}.dist-info/RECORD"
            with zipfile.ZipFile(self.wheel) as source:
                contents = {name: source.read(name) for name in source.namelist()}
                infos = {info.filename: info for info in source.infolist()}
            contents[metadata_name] += b"X-Tampered-Metadata: yes\n"
            rows: list[list[str]] = []
            for name in contents:
                if name == record_name:
                    rows.append([name, "", ""])
                    continue
                digest = base64.urlsafe_b64encode(hashlib.sha256(contents[name]).digest()).rstrip(
                    b"="
                )

                rows.append([name, f"sha256={digest.decode()}", str(len(contents[name]))])
            record_buffer = io.StringIO(newline="")
            csv.writer(record_buffer, lineterminator="\n").writerows(rows)
            contents[record_name] = record_buffer.getvalue().encode()
            with zipfile.ZipFile(altered_wheel, "w") as destination:
                for name, content in contents.items():
                    destination.writestr(infos[name], content)

            altered_sdist = root / self.sdist.name
            package_info_name = f"pyocd_debug_mcp-{VERSION}/PKG-INFO"
            with (
                tarfile.open(self.sdist, "r:gz") as source,
                tarfile.open(altered_sdist, "w:gz") as destination,
            ):
                for member in source.getmembers():
                    if member.name == package_info_name:
                        content = contents[metadata_name]
                        member.size = len(content)
                        destination.addfile(member, io.BytesIO(content))
                    else:
                        extracted = source.extractfile(member) if member.isfile() else None
                        destination.addfile(member, extracted)

            _verify_source_alignment(altered_wheel, altered_sdist)
            with self.assertRaisesRegex(RuntimeError, "fresh trusted build"):
                _verify_generated_metadata_alignment(
                    altered_wheel,
                    altered_sdist,
                    self.wheel,
                    self.sdist,
                )

    def test_non_reference_zip_and_tar_member_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-member-metadata-") as temporary:
            root = Path(temporary)
            altered_wheel = root / self.wheel.name
            with (
                zipfile.ZipFile(self.wheel) as source,
                zipfile.ZipFile(altered_wheel, "w") as destination,
            ):
                for info in source.infolist():
                    content = source.read(info.filename)
                    if info.filename == "pyocd_debug_mcp/server.py":
                        info.extra = b"\x01\x00\x00\x00"
                    destination.writestr(info, content)
            _verify_source_alignment(altered_wheel, self.sdist)
            with self.assertRaisesRegex(RuntimeError, "member metadata"):
                _verify_generated_metadata_alignment(
                    altered_wheel,
                    self.sdist,
                    self.wheel,
                    self.sdist,
                )

            altered_sdist = root / self.sdist.name
            target = f"pyocd_debug_mcp-{VERSION}/src/pyocd_debug_mcp/server.py"
            with (
                tarfile.open(self.sdist, "r:gz") as source,
                tarfile.open(altered_sdist, "w:gz") as destination,
            ):
                for member in source.getmembers():
                    extracted = source.extractfile(member) if member.isfile() else None
                    if member.name == target:
                        member.pax_headers = {"comment": "untrusted"}
                    destination.addfile(member, extracted)
            _verify_source_alignment(self.wheel, altered_sdist)
            with self.assertRaisesRegex(RuntimeError, "member metadata"):
                _verify_generated_metadata_alignment(
                    self.wheel,
                    altered_sdist,
                    self.wheel,
                    self.sdist,
                )

    def test_private_pair_snapshot_is_unchanged_when_originals_are_replaced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-artifact-snapshot-") as temporary:
            root = Path(temporary)
            original_wheel = root / self.wheel.name
            original_sdist = root / self.sdist.name
            original_wheel.write_bytes(self.wheel.read_bytes())
            original_sdist.write_bytes(self.sdist.read_bytes())
            private = root / "private"
            private.mkdir(mode=0o700)
            snapshot_wheel, snapshot_sdist = _snapshot_release_pair(
                private, original_wheel, original_sdist
            )
            original_wheel.write_bytes(b"replaced wheel after snapshot")
            original_sdist.write_bytes(b"replaced sdist after snapshot")

            self.assertNotEqual(snapshot_wheel.read_bytes(), original_wheel.read_bytes())
            self.assertNotEqual(snapshot_sdist.read_bytes(), original_sdist.read_bytes())
            _verify_source_alignment(snapshot_wheel, snapshot_sdist)

            recheck_root = root / "recheck"
            recheck_root.mkdir(mode=0o700)
            rechecked = _snapshot_release_pair(recheck_root, original_wheel, original_sdist)
            with self.assertRaisesRegex(RuntimeError, "changed after"):
                _assert_pair_digest_matches(
                    (snapshot_wheel, snapshot_sdist), rechecked, label="source"
                )

            published = _publish_verified_pair(root / "published", snapshot_wheel, snapshot_sdist)
            _assert_pair_digest_matches(
                (snapshot_wheel, snapshot_sdist), published, label="published"
            )

    def test_raw_archive_prefixes_and_trailers_cannot_match_a_fresh_build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-artifact-raw-bytes-") as temporary:
            root = Path(temporary)
            mutations = (
                (b"untrusted-prefix\n", b""),
                (b"", b"\nuntrusted-trailer"),
            )
            for index, (prefix, trailer) in enumerate(mutations):
                with self.subTest(prefix=bool(prefix), trailer=bool(trailer)):
                    wheel = root / f"{index}-{self.wheel.name}"
                    sdist = root / f"{index}-{self.sdist.name}"
                    wheel.write_bytes(prefix + self.wheel.read_bytes() + trailer)
                    sdist.write_bytes(prefix + self.sdist.read_bytes() + trailer)
                    with self.assertRaisesRegex(RuntimeError, "candidate artifact pair changed"):
                        _assert_pair_digest_matches(
                            (self.wheel, self.sdist),
                            (wheel, sdist),
                            label="candidate",
                        )

    def test_main_rejects_raw_mismatch_before_candidate_archive_parsing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-artifact-parser-order-") as temporary:
            root = Path(temporary)

            def build_reference(arguments, **_kwargs):
                output = Path(arguments[arguments.index("--out-dir") + 1])
                output.mkdir(parents=True)
                (output / self.wheel.name).write_bytes(self.wheel.read_bytes())
                (output / self.sdist.name).write_bytes(self.sdist.read_bytes())
                return subprocess.CompletedProcess(arguments, 0, "", "")

            mutations = (
                ("wheel-trailer", self.wheel.read_bytes() + b"untrusted trailer", None),
                ("unparseable-sdist", None, b"oversized PAX extension placeholder"),
            )
            for label, wheel_bytes, sdist_bytes in mutations:
                with self.subTest(label=label):
                    candidate = root / label
                    candidate.mkdir()
                    (candidate / self.wheel.name).write_bytes(
                        self.wheel.read_bytes() if wheel_bytes is None else wheel_bytes
                    )
                    (candidate / self.sdist.name).write_bytes(
                        self.sdist.read_bytes() if sdist_bytes is None else sdist_bytes
                    )
                    with (
                        patch.object(
                            release_verifier.sys,
                            "argv",
                            ["verify_release_artifacts.py", str(candidate)],
                        ),
                        patch.object(
                            release_verifier,
                            "_run",
                            side_effect=build_reference,
                        ),
                        patch.object(release_verifier, "_verify_pair") as verify_pair,
                        self.assertRaisesRegex(
                            RuntimeError,
                            "candidate artifact pair changed",
                        ),
                    ):
                        release_verifier.main()
                    verify_pair.assert_not_called()

    def test_publication_failure_restores_the_complete_previous_pair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-publish-rollback-") as temporary:
            destination = Path(temporary) / "dist"
            destination.mkdir()
            old_wheel = destination / self.wheel.name
            old_sdist = destination / self.sdist.name
            old_wheel.write_bytes(b"previous wheel")
            old_sdist.write_bytes(b"previous sdist")
            original_replace = os.replace
            new_replacements = 0

            def fail_second_new(source, target, *args, **kwargs):
                nonlocal new_replacements
                if str(source).endswith(".new"):
                    new_replacements += 1
                    if new_replacements == 2:
                        raise OSError("injected second-artifact publication failure")
                return original_replace(source, target, *args, **kwargs)

            with (
                patch.object(release_verifier.os, "replace", side_effect=fail_second_new),
                self.assertRaisesRegex(OSError, "second-artifact"),
            ):
                _publish_verified_pair(destination, self.wheel, self.sdist)

            self.assertEqual(old_wheel.read_bytes(), b"previous wheel")
            self.assertEqual(old_sdist.read_bytes(), b"previous sdist")
            self.assertEqual(
                {path.name for path in destination.iterdir()},
                {self.wheel.name, self.sdist.name},
            )

    def test_destination_swap_cannot_redirect_publication_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-publish-swap-") as temporary:
            root = Path(temporary)
            destination = root / "dist"
            destination.mkdir()
            (destination / self.wheel.name).write_bytes(b"previous wheel")
            (destination / self.sdist.name).write_bytes(b"previous sdist")
            moved = root / "moved"
            outside = root / "outside"
            outside.mkdir()
            original_snapshot = release_verifier._snapshot_artifact_at
            swapped = False

            def swap_then_copy(source, directory_descriptor, name):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    destination.rename(moved)
                    destination.symlink_to(outside, target_is_directory=True)
                return original_snapshot(source, directory_descriptor, name)

            with (
                patch.object(
                    release_verifier,
                    "_snapshot_artifact_at",
                    side_effect=swap_then_copy,
                ),
                self.assertRaisesRegex(RuntimeError, "changed during publication"),
            ):
                _publish_verified_pair(destination, self.wheel, self.sdist)

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual((moved / self.wheel.name).read_bytes(), b"previous wheel")
            self.assertEqual((moved / self.sdist.name).read_bytes(), b"previous sdist")

    def test_symlinked_destination_ancestor_cannot_redirect_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-publish-ancestor-") as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            redirected_parent = root / "redirected-parent"
            try:
                redirected_parent.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("directory symlinks unavailable")

            with self.assertRaises(OSError):
                _publish_verified_pair(
                    redirected_parent / "dist",
                    self.wheel,
                    self.sdist,
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_concurrent_extra_archive_prevents_successful_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-publish-inventory-") as temporary:
            destination = Path(temporary) / "dist"
            destination.mkdir()
            original_snapshot = release_verifier._snapshot_artifact_at
            injected = False

            def inject_extra_archive(source, directory_descriptor, name):
                nonlocal injected
                original_snapshot(source, directory_descriptor, name)
                if not injected:
                    injected = True
                    extra_descriptor = os.open(
                        "other-malware.whl",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                    try:
                        os.write(extra_descriptor, b"unexpected archive")
                    finally:
                        os.close(extra_descriptor)

            with (
                patch.object(
                    release_verifier,
                    "_snapshot_artifact_at",
                    side_effect=inject_extra_archive,
                ),
                self.assertRaisesRegex(RuntimeError, "inventory changed"),
            ):
                _publish_verified_pair(destination, self.wheel, self.sdist)

            self.assertEqual(
                {path.name for path in destination.iterdir()},
                {"other-malware.whl"},
            )

    def test_concurrent_publishers_are_serialized_and_leave_one_complete_pair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-publish-concurrent-") as temporary:
            root = Path(temporary)
            destination = root / "dist"
            source_a = root / "source-a"
            source_b = root / "source-b"
            source_a.mkdir()
            source_b.mkdir()
            pairs = []
            for source, marker in ((source_a, b"publisher-a"), (source_b, b"publisher-b")):
                wheel = source / self.wheel.name
                sdist = source / self.sdist.name
                wheel.write_bytes(marker + b"-wheel")
                sdist.write_bytes(marker + b"-sdist")
                pairs.append((wheel, sdist))

            start = threading.Barrier(3)
            results: list[BaseException | tuple[Path, Path]] = []

            def publish(pair: tuple[Path, Path]) -> None:
                start.wait()
                try:
                    results.append(_publish_verified_pair(destination, *pair))
                except BaseException as exc:  # retain the exact competing failure
                    results.append(exc)

            threads = [threading.Thread(target=publish, args=(pair,)) for pair in pairs]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(results), 2)
            self.assertTrue(all(not isinstance(result, BaseException) for result in results))
            observed = (
                (destination / self.wheel.name).read_bytes(),
                (destination / self.sdist.name).read_bytes(),
            )
            self.assertIn(
                observed,
                (
                    (b"publisher-a-wheel", b"publisher-a-sdist"),
                    (b"publisher-b-wheel", b"publisher-b-sdist"),
                ),
            )

    def test_publication_lock_release_between_probe_and_stat_is_retried(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-publish-lock-race-") as temporary:
            parent = Path(temporary)
            destination_name = "dist"
            lock_name = f".{destination_name}.release-publication.lock"
            (parent / lock_name).mkdir()
            parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            original_stat = release_verifier.os.stat
            raced = False

            def release_before_stat(path, *args, **kwargs):
                nonlocal raced
                if path == lock_name and not raced:
                    raced = True
                    os.rmdir(lock_name, dir_fd=parent_descriptor)
                return original_stat(path, *args, **kwargs)

            try:
                with patch.object(release_verifier.os, "stat", side_effect=release_before_stat):
                    lock = release_verifier._acquire_publication_lock(
                        parent_descriptor, destination_name
                    )
                self.assertTrue(raced)
                release_verifier._release_publication_lock(parent_descriptor, *lock)
            finally:
                os.close(parent_descriptor)

            self.assertFalse((parent / lock_name).exists())


class ExactReleasePairSelectionTests(unittest.TestCase):
    def test_exact_pair_is_selected_and_any_stale_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-pair-selection-") as temporary:
            directory = Path(temporary)
            wheel = directory / f"pyocd_debug_mcp-{VERSION}-py3-none-any.whl"
            sdist = directory / f"pyocd_debug_mcp-{VERSION}.tar.gz"
            wheel.touch()
            sdist.touch()
            self.assertEqual(_release_artifacts(directory), (wheel, sdist))

            stale = directory / "pyocd_debug_mcp-0.1.7-py3-none-any.whl"
            stale.touch()
            with self.assertRaisesRegex(RuntimeError, "exactly"):
                _release_artifacts(directory)

    def test_unrelated_archive_in_release_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-pair-selection-") as temporary:
            directory = Path(temporary)
            (directory / f"pyocd_debug_mcp-{VERSION}-py3-none-any.whl").touch()
            (directory / f"pyocd_debug_mcp-{VERSION}.tar.gz").touch()
            (directory / "other-malware.whl").touch()

            with self.assertRaisesRegex(RuntimeError, "exactly"):
                _release_artifacts(directory)

    def test_symlinked_candidate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="byo-pair-selection-") as temporary:
            directory = Path(temporary)
            real_wheel = directory / "real-wheel.bin"
            real_wheel.touch()
            wheel = directory / f"pyocd_debug_mcp-{VERSION}-py3-none-any.whl"
            try:
                wheel.symlink_to(real_wheel)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks unavailable")
            (directory / f"pyocd_debug_mcp-{VERSION}.tar.gz").touch()

            with self.assertRaisesRegex(RuntimeError, "singly linked regular file"):
                _release_artifacts(directory)


if __name__ == "__main__":
    unittest.main()
