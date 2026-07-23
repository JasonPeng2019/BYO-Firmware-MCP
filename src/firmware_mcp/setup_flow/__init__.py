"""Deterministic board setup and repair workflow primitives."""

from firmware_mcp.setup_flow.preflight import (
    PreflightDecision,
    PreflightEngine,
    PreflightInventory,
    SetupUserInput,
)
from firmware_mcp.setup_flow.setup import SetupWorkflow
from firmware_mcp.setup_flow.packs import PackCandidate, PackCandidatePipeline
from firmware_mcp.setup_flow.research import ResearchTracker
from firmware_mcp.setup_flow.targets import ProfileCommitCoordinator, TargetResolver
from firmware_mcp.setup_flow.validate import BoardValidator, ValidationResult

__all__ = [
    "BoardValidator",
    "PreflightDecision",
    "PreflightEngine",
    "PreflightInventory",
    "ProfileCommitCoordinator",
    "PackCandidate",
    "PackCandidatePipeline",
    "ResearchTracker",
    "SetupUserInput",
    "SetupWorkflow",
    "TargetResolver",
    "ValidationResult",
]
