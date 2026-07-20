from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pyocd_debug_mcp import server
from pyocd_debug_mcp.adapters.swd_interface import TargetSessionHandle
from pyocd_debug_mcp.firmstore.profiles import ProfileRepository
from pyocd_debug_mcp.firmstore.store import FirmStore
from pyocd_debug_mcp.pack_provision import verified_registry_pack_for_target
from pyocd_debug_mcp.safety.enforce import SafetyPolicy
from pyocd_debug_mcp.safety.map_build import (
    GenericSafetyMapDocument,
    SafetyMapError,
    SafetyMapRepository,
)
from pyocd_debug_mcp.safety.refresh import SafetyRefresher
from pyocd_debug_mcp.setup_flow.preflight import (
    PreflightDecision,
    ProbeCandidate,
    SetupUserInput,
)
from pyocd_debug_mcp.setup_flow.packs import PackCandidateError
from pyocd_debug_mcp.setup_flow.setup import RunAssignmentStore, SetupPhaseContext
from pyocd_debug_mcp.setup_flow.validate import ValidationInventory, ValidationProbe


def test_fresh_root_builtin_without_peripheral_metadata_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FirmStore(tmp_path / "builtin-project")
    datasheet = store.layout.project_root / "datasheet.pdf"
    datasheet.parent.mkdir(parents=True)
    datasheet.write_bytes(b"%PDF-1.7\nofficial part datasheet\n")
    board_id = "builtin_generic"
    user_input = SetupUserInput(
        board_id,
        "probe:PROBE-BUILTIN",
        "Unknown nRF carrier",
        "nRF52840-QIAA",
        None,
        datasheet_path=str(datasheet),
        requires_uart=False,
    )
    monkeypatch.setattr(server, "_firm_store", store)
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_research_required", None),
    )
    monkeypatch.setattr(
        server,
        "_resolve_setup_support",
        server._resolve_setup_support,
    )
    server._setup_builtin_candidates.pop(board_id, None)
    server._setup_target_overrides.pop(board_id, None)
    monkeypatch.setattr(
        server,
        "_live_test_builtin_setup_target",
        lambda **kwargs: (
            kwargs["candidate"].with_identity_proof(0x410FC241),
            kwargs["requested_policy"] or (None, "attach", None),
        ),
    )
    routed = server._setup_continue(
        board_id,
        "builtin-fresh",
        {
            "pyocd_target": "nrf52840",
            "evidence": [{"source": "official pyOCD", "claim": "built-in target"}],
            "reasoning_summary": "The installed built-in target matches the exact part.",
        },
    )
    assert routed["status"] == "setup_continuation_accepted"
    assert routed["pyocd_target"] == "nrf52840"
    assert board_id in server._setup_builtin_candidates
    assert server._setup_attachment_overrides[board_id] == (None, "attach", None)


def test_fresh_root_agent_pack_reply_builds_generic_profile_and_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No board YAML/catalog record is needed; target and proof come from candidate bytes."""

    store = FirmStore(tmp_path / "empty-project")
    profiles = ProfileRepository(store, legacy_board_dir=tmp_path / "no-legacy-boards")
    safety = SafetyMapRepository(store)
    datasheet = tmp_path / "empty-project" / "datasheet.pdf"
    datasheet.parent.mkdir(parents=True)
    datasheet.write_bytes(b"%PDF-1.7\nlocal official datasheet fixture\n")
    source = verified_registry_pack_for_target("stm32l476rgtx")
    assert source is not None
    part = "STM32L476VGT6"  # another exact PDSC leaf; absent from the server registry bindings
    board_id = "fresh_generic"
    user_input = SetupUserInput(
        board_id,
        "probe:PROBE-NEW",
        "Fresh Generic Board",
        part,
        None,
        datasheet_path=str(datasheet),
        requires_uart=False,
    )
    monkeypatch.setattr(server, "_firm_store", store)
    monkeypatch.setattr(server, "_profile_repository", profiles)
    monkeypatch.setattr(server, "_safety_repository", safety)
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_research_required", None),
    )
    server._setup_pack_pipelines.clear()
    server._setup_target_overrides.pop(board_id, None)
    server._setup_attachment_overrides.pop(board_id, None)
    calls: list[dict[str, object]] = []

    def open_session(**kwargs: object) -> object:
        calls.append(dict(kwargs))
        return SimpleNamespace(probe_uid="PROBE-NEW")

    monkeypatch.setattr(server.target_control, "open_session", open_session)
    monkeypatch.setattr(server.target_control, "close_session", lambda _handle: None)
    monkeypatch.setattr(
        server.target_control,
        "read_memory",
        lambda _handle, address, _width: 0x410FC241 if address == 0xE000ED00 else 0,
    )

    accepted = server._setup_continue(
        board_id,
        "continuation-fresh",
        {
            "pack_id": source.spec.id,
            "version": source.spec.version,
            "filename": source.spec.filename,
            "url": source.spec.url,
            "source_path": str(source.path),
            "official_sha256": source.spec.sha256,
            "evidence": [{"source": source.spec.url, "claim": "official vendor pack"}],
            "reasoning_summary": "The official pack declares the exact requested device leaf.",
        },
    )

    assert accepted["status"] == "setup_continuation_accepted"
    assert accepted["pyocd_target"] == "stm32l476vgtx"
    assert calls[0]["pack_path"] == store.layout.pack_files / source.spec.filename
    assert calls[0]["pack_sha256"] == source.spec.sha256
    assert store.layout.pack_manifest.is_file()
    assert not list(store.layout.boards.glob("*.yaml"))


    context = cast(
        SetupPhaseContext,
        SimpleNamespace(
            user_input=user_input,
            preflight=PreflightDecision(
                "preflight_ready",
                "setup/preflight-ready",
                "ready",
                selected_probe=ProbeCandidate(
                    "PROBE-NEW", "Generic CMSIS-DAP", "cmsisdap", "PROBE-NEW"
                ),
                selected_target="stm32l476vgtx",
            ),
        ),
    )
    connected = server._setup_connection_phase(context)
    assert connected.verified is True
    assert calls[-1]["pack_path"] == store.layout.pack_files / source.spec.filename
    assert calls[-1]["pack_sha256"] == source.spec.sha256
    profile = profiles.load(board_id, include_legacy=False)
    assert profile.device_support is not None
    assert profile.board.pyocd_target == "stm32l476vgtx"
    assert profile.board.silicon_id_label == "Cortex-M4 CPUID compatibility identity"
    assert profile.to_document()["datasheet_ref"].startswith(".firm/evidence/datasheets/")

    document = server._build_automatic_catalog_safety(context)
    assert isinstance(document, GenericSafetyMapDocument)
    assert document.partitions.application is None
    assert document.deployment_policy == {"kind": "none"}
    assert document.geometry.erase_available is True
    assert safety.load_current(board_id) == document

    assert profiles.load(board_id, include_legacy=False).safety_ref is None
    monkeypatch.setattr(
        server,
        "_safety_refresher",
        SafetyRefresher(
            store,
            derive=server._derive_safety_map,
            has_live_identity=lambda _board: False,
            on_commit=server._restamp_after_refresh,
        ),
    )
    monkeypatch.setattr(server, "_safety_continuation", lambda _prefix: "refresh-test")
    monkeypatch.setattr(server.connection_manager, "maybe_connection", lambda _board: None)

    refreshed = server._run_board_safety_refresh(board_id)

    assert refreshed["status"] == "safety_refresh_completed"
    profile = profiles.load(board_id, include_legacy=False)
    assert profile.safety_ref == ".firm/safety/fresh_generic/memory_map.yaml"
    monkeypatch.setattr(server, "assignment_store", RunAssignmentStore({}))
    monkeypatch.setattr(
        server,
        "_validation_inventory",
        lambda: ValidationInventory(
            probes=(ValidationProbe("PROBE-NEW", "Generic CMSIS-DAP", "cmsisdap", "PROBE-NEW"),)
        ),
    )
    overview = server._setup_overview(["Fresh Generic Board"])
    routes = cast(list[dict[str, object]], overview["routes"])
    assert routes[0]["route"] == "validate"

    stale = replace(
        document,
        source_digests=replace(document.source_digests, semantic_profile="0" * 64),
    )
    safety.commit(board_id, stale)
    stale_overview = server._setup_overview(["Fresh Generic Board"])
    stale_routes = cast(list[dict[str, object]], stale_overview["routes"])
    assert stale_routes[0]["route"] == "refresh"
    safety.commit(board_id, document)

    safety.path(board_id).write_text("not: [valid", encoding="utf-8")
    with pytest.raises(
        SafetyMapError, match="refusing to discard possible one-way deployment ownership"
    ):
        server._derive_generic_safety_map(board_id)
    safety.commit(board_id, document)

    # The same fresh-root composition exercises the allocation builder boundary:
    # containment and driver checks happen before a pending allocation exists.
    artifact = (
        Path(__file__).resolve().parents[1] / "firmware/nucleo_l476rg/reference/build/firmware.elf"
    )
    handle = TargetSessionHandle(
        session=SimpleNamespace(),  # type: ignore[arg-type]
        board=profile.board,
        probe_uid="PROBE-NEW",
        route_used="test",
        target_override=profile.board.pyocd_target,
    )
    monkeypatch.setattr(
        server, "_safety_policy", SafetyPolicy(safety, authority_verifier=lambda _doc: None)
    )
    monkeypatch.setattr(server, "_current_target", lambda _board: profile.board.pyocd_target)
    monkeypatch.setattr(
        server.connection_manager,
        "connection_for",
        lambda _board: SimpleNamespace(connection_id="connection-fresh"),
    )
    monkeypatch.setattr(
        server.connection_manager,
        "maybe_connection",
        lambda _board: SimpleNamespace(connection_id="connection-fresh"),
    )
    monkeypatch.setattr(
        server.target_control,
        "read_memory_block",
        lambda *_args: pytest.fail("artifact allocation must not require whole-device reads"),
    )
    monkeypatch.setattr(server.gate_manager, "refresh_map_stamp", lambda *_args: True)
    server._pending_generic_allocations.clear()

    server._stage_generic_allocation(board_id, artifact, handle)
    pending = server._prepare_generic_allocation("flash_application", board_id, artifact, handle)
    assert pending is not None
    assert safety.load_current(board_id).partitions.application is None
    server._commit_generic_allocation(board_id, pending)
    allocated = safety.load_current(board_id)
    assert isinstance(allocated, GenericSafetyMapDocument)
    assert allocated.partitions.application is not None

    original_geometry_resolver = server.resolve_device_support_geometry
    current_geometry = original_geometry_resolver(
        server.resolve_available_pack_support(store, part), store
    )
    monkeypatch.setattr(
        server,
        "resolve_device_support_geometry",
        lambda *_args, **_kwargs: replace(current_geometry, driver_proof_digest="0" * 64),
    )
    with pytest.raises(SafetyMapError, match="bounded flash driver"):
        server._derive_generic_safety_map(board_id)
    monkeypatch.setattr(server, "resolve_device_support_geometry", original_geometry_resolver)

    # A previously programmed device uses the same artifact-derived allocation path.
    safety.commit(board_id, document)
    server._pending_generic_allocations.clear()
    server._stage_generic_allocation(board_id, artifact, handle)
    assert board_id in server._pending_generic_allocations
    assert safety.load_current(board_id).partitions.application is None
def test_pack_default_attach_failure_requests_same_pack_with_explicit_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FirmStore(tmp_path / "attachment-project")
    source = verified_registry_pack_for_target("stm32l476rgtx")
    assert source is not None
    part = "STM32L476VGT6"
    board_id = "attachment_generic"
    user_input = SetupUserInput(
        board_id,
        "probe:PROBE-ATTACH",
        "Attachment Generic Board",
        part,
        None,
        datasheet_path=str(store.layout.project_root / "datasheet.pdf"),
        requires_uart=False,
    )
    store.layout.project_root.mkdir(parents=True, exist_ok=True)
    Path(user_input.datasheet_path).write_bytes(b"%PDF-1.7\nofficial part datasheet\n")
    monkeypatch.setattr(server, "_firm_store", store)
    monkeypatch.setattr(
        server._setup_workflow,
        "continuation_context",
        lambda _token: (user_input, "setup_research_required", None),
    )
    policies: list[tuple[str | None, str, int | None] | None] = []

    class Pipeline:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail

        def validate_device(self, *_args: object, **_kwargs: object) -> object:
            if self.fail:
                raise PackCandidateError(
                    "package/live-connect-failed", "default attachment policies failed"
                )
            return object()

        def promote(self, _validated: object, *, board_id: str) -> None:
            assert board_id == "attachment_generic"

    def pipeline(
        _board_id: str,
        _continuation: str,
        _probe_uid: str,
        requested_policy: tuple[str | None, str, int | None] | None,
    ) -> tuple[Pipeline, list[tuple[str | None, str, int | None]]]:
        policies.append(requested_policy)
        return (
            Pipeline(fail=requested_policy is None),
            [] if requested_policy is None else [requested_policy],
        )

    monkeypatch.setattr(server, "_setup_pack_pipeline", pipeline)
    base_response: dict[str, object] = {
        "pack_id": source.spec.id,
        "version": source.spec.version,
        "filename": source.spec.filename,
        "url": source.spec.url,
        "source_path": str(source.path),
        "official_sha256": source.spec.sha256,
        "evidence": [{"source": "official pack index", "claim": "exact part support"}],
        "reasoning_summary": "The exact PDSC leaf matches the supplied part.",
    }

    retry = server._setup_continue(board_id, "attachment-retry", base_response)

    assert retry["status"] == "setup_research_required"
    assert "Keep this same package" in cast(str, retry["agent_prompt"])
    assert set(cast(list[str], retry["exact_response_fields"])) >= {
        "pack_id",
        "debug_protocol",
        "debug_connect_mode",
        "debug_clock_hz",
    }
    assert retry["rejected_candidates"] == []

    accepted = server._setup_continue(
        board_id,
        "attachment-retry",
        base_response
        | {
            "debug_protocol": "swd",
            "debug_connect_mode": "pre-reset",
            "debug_clock_hz": 500_000,
        },
    )

    assert accepted["status"] == "setup_continuation_accepted"
    assert policies == [None, ("swd", "pre-reset", 500_000)]
    assert server._setup_attachment_overrides.pop(board_id) == (
        "swd",
        "pre-reset",
        500_000,
    )
    server._setup_target_overrides.pop(board_id, None)
