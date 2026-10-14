"""Durable release-evidence consistency checks."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import generate_release_evidence
from scripts.verify_release_evidence import (
    RESULTS,
    _read_regular_file,
    pinned_release_directory,
    write_release_text,
    _manifest_mode,
    _valid_base_git_head,
    verify_release_logs,
)


def release_results_dir() -> Path:
    override = os.environ.get("BYO_RELEASE_EVIDENCE_RESULTS_DIR")
    return Path(override) if override else RESULTS


class ReleaseEvidenceTests(unittest.TestCase):
    def test_native_directory_relative_read_and_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "capture.log"
            write_release_text(path, "actual evidence\n")
            with pinned_release_directory(root) as pinned:
                self.assertEqual(
                    _read_regular_file(pinned / path.name, "test log"), b"actual evidence\n"
                )
            self.assertEqual(path.read_text(), "actual evidence\n")

    def test_tracked_index_mode_makes_manifest_portable_across_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ordinary.txt"
            path.write_text("same bytes\n", encoding="utf-8")
            path.chmod(0o700)
            self.assertEqual(
                _manifest_mode("ordinary.txt", path, {"ordinary.txt": "100644"}),
                b"regular\0",
            )
        self.assertFalse(_valid_base_git_head("not-a-commit"))
        self.assertFalse(_valid_base_git_head("0" * 40))
        with patch.dict(
            os.environ,
            {"TMPDIR": "/network/tmp", "TEMP": "/network/tmp", "TMP": "/network/tmp"},
        ):
            environment = generate_release_evidence._release_test_environment(
                BYO_RELEASE_EVIDENCE_GENERATING="1"
            )
        self.assertEqual(environment["TMPDIR"], "/tmp")
        self.assertEqual(environment["TEMP"], "/tmp")
        self.assertEqual(environment["TMP"], "/tmp")
        self.assertEqual(environment["BYO_RELEASE_EVIDENCE_GENERATING"], "1")

    @unittest.skipIf(
        os.environ.get("BYO_RELEASE_EVIDENCE_GENERATING") == "1",
        "self-referential retained-log check runs against immutable staged evidence",
    )
    def test_retained_release_logs_are_green_and_match_one_candidate(self) -> None:
        self.assertEqual(verify_release_logs(release_results_dir()), [])

    def test_unattested_release_looking_log_is_rejected(self) -> None:
        extra = release_results_dir() / "0.1.8rc1-unattested.log"
        try:
            extra.write_text("not attested\n", encoding="utf-8")
            errors = verify_release_logs(release_results_dir())
        finally:
            extra.unlink(missing_ok=True)
        self.assertTrue(any("unexpected release evidence log" in error for error in errors))

    def test_release_paths_reject_symlinks_and_non_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary).resolve()
            real_results = results / "real-results"
            linked_results = results / "linked-results"
            real_results.mkdir()
            try:
                linked_results.symlink_to(real_results, target_is_directory=True)
            except OSError:
                # Some Windows policies prohibit unprivileged symlink creation.
                pass
            else:
                self.assertTrue(
                    any(
                        "results directory must not be a symlink" in error
                        for error in verify_release_logs(linked_results)
                    )
                )

            release_path = results / "0.1.8rc1-static.log"
            release_path.mkdir()
            self.assertTrue(
                any("not a regular file" in error for error in verify_release_logs(results))
            )
            release_path.rmdir()

            target = results / "foreign.log"
            target.write_text("foreign\n", encoding="utf-8")
            try:
                release_path.symlink_to(target)
            except OSError:
                # Some Windows policies prohibit unprivileged symlink creation;
                # the non-regular-file assertion above still runs there.
                pass
            else:
                self.assertTrue(
                    any("must not be a symlink" in error for error in verify_release_logs(results))
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staging = root / "staging"
            results = root / "results"
            displaced = root / "displaced-results"
            staging.mkdir()
            results.mkdir()
            names = (
                "0.1.8rc1-static.log",
                "0.1.8rc1-python310-parse.log",
                "0.1.8rc1-software-suite.log",
                "0.1.8rc1-monitor-codex-e2e.log",
                "0.1.8rc1-attestation.json",
            )
            for name in names:
                (results / name).write_text(f"old {name}\n", encoding="utf-8")
                (staging / name).write_text(f"new {name}\n", encoding="utf-8")
            release_logs = tuple(results / name for name in names[:4])

            def swap_results(*_args: object, **_kwargs: object) -> tuple[str, int]:
                os.replace(results, displaced)
                results.mkdir()
                return "", 0

            with (
                patch.object(generate_release_evidence, "RELEASE_LOGS", release_logs),
                patch.object(generate_release_evidence, "ATTESTATION", results / names[4]),
                patch.object(generate_release_evidence, "verify_release_logs", return_value=[]),
                patch.object(generate_release_evidence, "_run", side_effect=swap_results),
                self.assertRaisesRegex(ValueError, "changed during evidence access"),
            ):
                generate_release_evidence._publish_with_rollback(staging)

            for name in names:
                self.assertEqual(
                    (displaced / name).read_text(encoding="utf-8"),
                    f"old {name}\n",
                )


if __name__ == "__main__":
    unittest.main()
