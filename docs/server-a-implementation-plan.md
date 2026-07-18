# Plan: implement Server A and generalize Server B

Specification: `docs/server-a-implementation-spec.md`  
Authority: `Server_A_functionality.md`

1. Add strict common-context, decision, workflow, prompt, provider-session, green-check, and
   controller modules under `pyocd_debug_mcp.turnkey`.
2. Add a Server A FastMCP composition with three load tools and three locked agentic tools,
   including full-context and in-memory delta calls.
3. Add a Server B streamable-HTTP entry point and one-command supervisor that owns both lifetimes.
4. Change board-affecting dispatch to one global lock and update concurrency tests.
5. Make target control backend-injectable and make artifact acceptance backend/content driven
   without caller-supplied address authority.
6. Replace stale runtime documentation and make non-checkout execution fail early and clearly.
7. Add focused controller, MCP, lifecycle, backend, format, and lock tests; run Ruff, Pyright, the
   full locked suite, package/import, and bounded stdio/HTTP lifecycle smokes.
8. Re-run the exact adversarial audit from a fresh Codex 5.6 Sol session and repeat until no valid
   criticism remains.
