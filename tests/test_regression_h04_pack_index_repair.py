"""Regression coverage for durable pack-index repair failure boundaries."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyocd_debug_mcp import pack_index_repair as repair


_OLD_MASTER = (
    b'<index><pdsc vendor="Example" name="Known" version="1" '
    b'url="https://fixture.invalid/old/" /></index>'
)
_NEW_MASTER = (
    b'<index><pdsc vendor="Example" name="Candidate" version="2" '
    b'url="https://fixture.invalid/new/" /></index>'
)


class _Cache:
    def __init__(self, _load: bool, _download: bool, *, json_path: str | None, data_path: str | None) -> None:
        assert data_path is not None
        self.data_path = data_path
        root = Path(data_path)
        self.index_path = str(Path(json_path) if json_path else root / "index.json")
        self.aliases_path = str(root / "aliases.json")


def _rebuild(cache: _Cache) -> tuple[int, int]:
    root = Path(cache.data_path)
    (Path(cache.index_path)).write_bytes(b"stable-index")
    (Path(cache.aliases_path)).write_bytes(b"stable-aliases")
    count = len(list(root.glob("*.pdsc")))
    return count, count


class PackIndexRepairFailureBoundaryRegressionTests(unittest.TestCase):
    def test_failed_refresh_download_keeps_prior_master_for_offline_recovery(self) -> None:
        """An incomplete refresh must not replace evidence for a completed repair."""

        url = "https://fixture.invalid/index.pidx"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(exist_ok=True)
            retained = repair._master_cache_path(root, url)
            retained.write_bytes(_OLD_MASTER)

            with (
                patch.object(repair, "Cache", _Cache),
                patch.object(repair, "_fetch_master_index_content", return_value=_NEW_MASTER),
                patch.object(
                    repair,
                    "_download_descriptor",
                    side_effect=repair.PackIndexRepairError("fixture transport failure"),
                ),
                patch.object(repair, "rebuild_cached_index", side_effect=AssertionError("must not rebuild")),
            ):
                with self.assertRaisesRegex(repair.PackIndexRepairError, "fixture transport failure"):
                    repair.repair_live_pack_index(
                        data_path=str(root), index_url=url, missing_only=False, retries=0
                    )

            self.assertEqual(retained.read_bytes(), _OLD_MASTER)

            replayed: list[str] = []
            with (
                patch.object(repair, "Cache", _Cache),
                patch.object(
                    repair,
                    "_fetch_master_index_content",
                    side_effect=AssertionError("retained master should replay offline"),
                ),
                patch.object(
                    repair,
                    "_download_descriptor",
                    side_effect=lambda ref, dest, **_kwargs: replayed.append(ref.name)
                    or dest.write_bytes(b"known-pdsc"),
                ),
                patch.object(repair, "rebuild_cached_index", side_effect=_rebuild),
            ):
                result = repair.repair_live_pack_index(
                    data_path=str(root), index_url=url, missing_only=True, retries=0
                )

        self.assertEqual(replayed, ["Known"])
        self.assertEqual((result.master_count, result.download_count), (1, 1))

    def test_failed_rebuild_publishes_no_new_master_evidence(self) -> None:
        """A newly fetched PIDX becomes durable only after the complete rebuild succeeds."""

        url = "https://fixture.invalid/index.pidx"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(repair, "Cache", _Cache),
                patch.object(repair, "_fetch_master_index_content", return_value=_NEW_MASTER),
                patch.object(
                    repair, "_download_descriptor", side_effect=lambda _ref, dest, **_kwargs: dest.write_bytes(b"pdsc")
                ),
                patch.object(
                    repair,
                    "rebuild_cached_index",
                    side_effect=repair.PackIndexRepairError("fixture rebuild failure"),
                ),
            ):
                with self.assertRaisesRegex(repair.PackIndexRepairError, "fixture rebuild failure"):
                    repair.repair_live_pack_index(data_path=str(root), index_url=url, retries=0)

            self.assertFalse(repair._master_cache_path(root, url).exists())


if __name__ == "__main__":
    unittest.main()
