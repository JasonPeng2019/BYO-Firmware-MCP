"""Unforgeable in-process identity for the current MCP client connection."""

from __future__ import annotations

from contextvars import ContextVar, Token

_LOCAL_PRINCIPAL = "local-direct-call"
_caller_principal: ContextVar[str] = ContextVar(
    "mcp_caller_principal", default=_LOCAL_PRINCIPAL
)


def current_caller_principal() -> str:
    return _caller_principal.get()


def set_caller_principal(value: str) -> Token[str]:
    return _caller_principal.set(value)


def reset_caller_principal(token: Token[str]) -> None:
    _caller_principal.reset(token)
