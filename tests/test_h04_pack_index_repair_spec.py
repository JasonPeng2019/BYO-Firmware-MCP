"""Adversarial acceptance tests for H04 pack-index repair (CL-001--CL-003)."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pyocd_debug_mcp import pack_index_repair as repair


PIDX_A = b'<index><pdsc vendor="Vendor" name="PackA" version="1.2.3" url="https://packs.example/a/"/></index>'
PIDX_B = b'<index><pdsc vendor="Vendor" name="PackB" version="4.5.6" url="https://packs.example/b/"/></index>'


class _Cache:
    def __init__(self, _load: bool, _download: bool, *, json_path: str | None, data_path: str | None) -> None:
        assert data_path is not None
        self.data_path = data_path
        root = Path(data_path)
        self.index_path = str(Path(json_path) if json_path else root / "index.json")
        self.aliases_path = str(root / "aliases.json")


def _rebuild(cache: _Cache) -> tuple[int, int]:
    root = Path(cache.data_path)
    pdscs = sorted(root.glob("*.pdsc"))
    Path(cache.index_path).write_text("stable-index", encoding="utf-8")
    Path(cache.aliases_path).write_text("stable-aliases", encoding="utf-8")
    return len(pdscs), len(pdscs)


class PackIndexRepairSpecTests(unittest.TestCase):
    def _run(self, root: Path, url: str, *, refresh: bool = False) -> repair.RepairResult:
        return repair.repair_live_pack_index(
            data_path=str(root), json_path=str(root / "index.json"), index_url=url,
            missing_only=not refresh, retries=0,
        )

    def test_cl001_zero_retries_is_one_attempt_and_preserves_destination(self) -> None:
        ref = repair.PdscRef("Vendor", "Pack", "1", "https://packs.example/")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "Vendor.Pack.1.pdsc"
            destination.write_bytes(b"old-good")
            with patch.object(repair.httpx, "stream", side_effect=OSError("offline")) as stream:
                with self.assertRaisesRegex(repair.PackIndexRepairError, "https://packs.example/Vendor.Pack.pdsc"):
                    repair._download_descriptor(ref, destination, timeout=1, retries=0)
            self.assertEqual(stream.call_count, 1)
            self.assertEqual(destination.read_bytes(), b"old-good")
            self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_cl001_positive_retries_stop_after_early_success_and_negative_is_preflight(self) -> None:
        ref = repair.PdscRef("Vendor", "Pack", "1", "https://packs.example/")

        class Response:
            def raise_for_status(self) -> None:
                return None

            def iter_bytes(self, _chunk: int):
                yield b"new-good"

        class Stream:
            def __enter__(self) -> Response:
                return Response()

            def __exit__(self, *_args: object) -> bool:
                return False

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "Vendor.Pack.1.pdsc"
            with (
                patch.object(repair.httpx, "stream", side_effect=[OSError("first"), Stream()]) as stream,
                patch.object(repair.time, "sleep") as sleep,
            ):
                repair._download_descriptor(ref, destination, timeout=1, retries=3)
            self.assertEqual(stream.call_count, 2)
            sleep.assert_called_once_with(1.0)
            self.assertEqual(destination.read_bytes(), b"new-good")

            with patch.object(repair, "_fetch_master_index_content") as fetch:
                with self.assertRaisesRegex(repair.PackIndexRepairError, "zero or greater"):
                    repair.repair_live_pack_index(data_path=directory, retries=-1)
            fetch.assert_not_called()

    def test_cl002_retains_exact_raw_master_and_replays_offline_missing_only(self) -> None:
        url = "https://example.invalid/catalog?channel=stable"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests: list[str] = []

            def download(ref: repair.PdscRef, dest: Path, **_kwargs: object) -> None:
                requests.append(ref.remote_pdsc_url)
                dest.write_bytes(b"pdsc")

            with (
                patch.object(repair, "Cache", _Cache),
                patch.object(repair, "_fetch_master_index_content", side_effect=lambda got, _timeout: requests.append(got) or PIDX_A),
                patch.object(repair, "_download_descriptor", side_effect=download),
                patch.object(repair, "rebuild_cached_index", side_effect=_rebuild),
            ):
                first = self._run(root, url)
                retained = repair._master_cache_path(root, url)
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                second = self._run(root, url)
                after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

            self.assertEqual(requests, [url, "https://packs.example/a/Vendor.PackA.pdsc"])
            self.assertEqual((first.download_count, second.download_count), (1, 0))
            self.assertEqual(retained.read_bytes(), PIDX_A)
            self.assertEqual(before, after)

    def test_cl002_refresh_bypasses_retained_master_and_corrupt_evidence_refuses_before_download(self) -> None:
        url = "https://example.invalid/master"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retained = repair._master_cache_path(root, url)
            root.mkdir(exist_ok=True)
            retained.write_bytes(PIDX_A)
            (root / "Vendor.PackA.1.2.3.pdsc").write_bytes(b"old")
            download = Mock(side_effect=lambda _ref, dest, **_kwargs: dest.write_bytes(b"new"))
            with (
                patch.object(repair, "Cache", _Cache),
                patch.object(repair, "_fetch_master_index_content", return_value=PIDX_A) as fetch,
                patch.object(repair, "_download_descriptor", download),
                patch.object(repair, "rebuild_cached_index", side_effect=_rebuild),
            ):
                result = self._run(root, url, refresh=True)
            fetch.assert_called_once_with(url, 60.0)
            download.assert_called_once()
            self.assertEqual(result.download_count, 1)

            retained.write_bytes(b"not xml")
            with (
                patch.object(repair, "Cache", _Cache),
                patch.object(repair, "_download_descriptor") as offline_download,
            ):
                with self.assertRaisesRegex(repair.PackIndexRepairError, "Retained master evidence.*--refresh"):
                    self._run(root, url)
            offline_download.assert_not_called()

    def test_cl002_exact_url_evidence_cannot_collide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url_a, url_b = "https://host.invalid/a?x=1", "https://host.invalid/a?x=2"
            self.assertNotEqual(repair._master_cache_path(root, url_a), repair._master_cache_path(root, url_b))
            root.mkdir(exist_ok=True)
            repair._master_cache_path(root, url_a).write_bytes(PIDX_A)
            repair._master_cache_path(root, url_b).write_bytes(PIDX_B)
            downloaded: list[str] = []
            with (
                patch.object(repair, "Cache", _Cache),
                patch.object(repair, "_fetch_master_index_content", side_effect=AssertionError("offline must not fetch")),
                patch.object(repair, "_download_descriptor", side_effect=lambda ref, dest, **_k: downloaded.append(ref.name) or dest.write_bytes(b"pdsc")),
                patch.object(repair, "rebuild_cached_index", side_effect=_rebuild),
            ):
                self._run(root, url_a)
                self._run(root, url_b)
            self.assertEqual(downloaded, ["PackA", "PackB"])

    def test_cl003_help_and_cli_error_are_operator_facing(self) -> None:
        parser = repair.build_parser()
        help_text = " ".join(parser.format_help().lower().split())
        for option in ("--vendor", "--pack-name", "--name-contains", "--refresh", "--timeout", "--retries", "--index-url", "--json-path", "--data-path"):
            self.assertIn(option, help_text)
        for phrase in ("additional retries after the initial request", "retained validated evidence", "exact url", "offline", "recover invalid retained"):
            self.assertIn(phrase, help_text)
        self.assertEqual(parser.parse_args([]).retries, 3)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(sys, "argv", ["pyocd-pack-repair"]), patch.object(repair, "repair_live_pack_index", side_effect=repair.PackIndexRepairError("honest failure")):
            self.assertEqual(repair.main(), 1)
        self.assertEqual(output.getvalue(), "[FAIL] honest failure\n")


if __name__ == "__main__":
    unittest.main()
