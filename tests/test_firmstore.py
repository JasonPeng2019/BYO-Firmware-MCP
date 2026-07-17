from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pyocd_debug_mcp.firmstore import store as store_module
from pyocd_debug_mcp.firmstore.store import (
    FirmStore,
    FirmStoreError,
    PersistedAuthorityError,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "pyocd_debug_mcp"


def test_layout_is_project_local_and_has_one_owner(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    layout = store.ensure_layout()

    assert layout.root == tmp_path.resolve() / ".firm"
    assert {
        layout.boards,
        layout.packs,
        layout.setup,
        layout.safety,
        layout.validation,
        layout.cache,
    } == {
        layout.root / "boards",
        layout.root / "packs",
        layout.root / "setup",
        layout.root / "safety",
        layout.root / "validation",
        layout.root / "cache",
    }
    assert all(path.is_dir() for path in layout.root.iterdir())
    assert layout.setup_attempt("setup-1") == layout.setup / "setup-1"
    assert layout.safety_board("board_a") == layout.safety / "board_a"
    assert layout.safety_reference_prefix("board_a").as_posix() == ".firm/safety/board_a"
    assert layout.validation_attempt("validation-1") == layout.validation / "validation-1"
    assert layout.cache_artifact("attachments.json") == layout.cache / "attachments.json"


def test_firmstore_is_the_only_low_level_writer_for_new_artifacts() -> None:
    """Artifact modules use FirmStore, and only the layout owner spells `.firm` paths."""

    store_path = SOURCE_ROOT / "firmstore" / "store.py"
    forbidden_calls = {
        "mkdir",
        "rename",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
    violations: list[str] = []
    literal_owners: list[str] = []

    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        for node in ast.walk(tree):
            if (
                path.parent.name == "firmstore"
                and path != store_path
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    node.func.attr in forbidden_calls
                    or (
                        node.func.attr == "replace"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "os"
                    )
                    or (
                        node.func.attr == "open"
                        and any(
                            character in str(node.args[0].value)
                            for character in "wax+"
                            if node.args and isinstance(node.args[0], ast.Constant)
                        )
                    )
                )
            ):
                violations.append(f"{relative}:{node.lineno}:{node.func.attr}")
            if (
                path != store_path
                and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ".firm" in node.value
            ):
                literal_owners.append(f"{relative}:{node.lineno}")

    assert violations == []
    assert literal_owners == []


def test_atomic_write_replaces_complete_document(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    target = store.layout.setup / "attempt-1" / "report.json"

    store.atomic_write_json(target, {"status": "first", "value": "é"})
    store.atomic_write_json(target, {"status": "complete", "value": "é"})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "status": "complete",
        "value": "é",
    }
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_interrupted_atomic_replace_preserves_previous_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = FirmStore(tmp_path)
    target = store.layout.validation / "attempt-1" / "report.json"
    store.atomic_write_json(target, {"status": "previous"})

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("simulated interruption before replace")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        store.atomic_write_json(target, {"status": "partial"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "previous"}
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_store_rejects_outside_writes_and_persisted_authority(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)

    with pytest.raises(FirmStoreError, match="must stay below"):
        store.atomic_write_text(tmp_path / "outside.txt", "no")
    with pytest.raises(PersistedAuthorityError, match="gate_open"):
        store.atomic_write_json(
            store.layout.safety / "board_a" / "state.json",
            {"nested": {"gate_open": True}},
        )

    assert not (tmp_path / "outside.txt").exists()
    assert not (store.layout.safety / "board_a" / "state.json").exists()


def test_remove_artifact_is_scoped_to_firmstore(tmp_path: Path) -> None:
    store = FirmStore(tmp_path)
    artifact = store.atomic_write_bytes(store.layout.pack_files / "staged.pack", b"pack")

    store.remove_artifact(artifact)

    assert not artifact.exists()
    with pytest.raises(FirmStoreError, match="must stay below"):
        store.remove_artifact(tmp_path / "outside.pack")
