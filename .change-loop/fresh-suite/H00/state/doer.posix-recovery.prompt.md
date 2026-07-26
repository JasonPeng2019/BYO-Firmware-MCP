# H00 POSIX implementation recovery — act on the current plan

This thread's earlier H00 work concerned CL-001 through CL-003. That work is preserved, but it is
not the current task. Do not report those earlier metadata/docs changes as completion.

Read these files from disk now:

1. `.change-loop/fresh-suite/H00/plan.md`
2. `.change-loop/fresh-suite/H00/state/test_report.md`
3. `../.codex/design_charter.md`
4. `src/pyocd_debug_mcp/kernel/processes.py`

The current plan is CL-004/CL-005. The recovery report proves CL-004 is unimplemented:
`processes.py` still directly reads POSIX-absent `subprocess.CREATE_NEW_PROCESS_GROUP`,
`ctypes.windll`, and `ctypes.get_last_error`.

Implement CL-004 now. You exclusively own
`src/pyocd_debug_mcp/kernel/processes.py`, and that is the only file you may edit.

Required behavior:

- `process_group_options(platform="nt")` returns exact `0x00000204` even on POSIX.
- Centralize lazy Windows `ctypes` loader/DLL/last-error access behind the smallest narrowly typed
  local boundary.
- Actual Windows calls keep their real semantics.
- Calling a Windows native operation without those APIs raises contextual `OSError`; never
  fabricate success.
- Preserve all cleanup phases, deadlines, marker lifecycle, primary exception identity, public
  signatures, and POSIX session/process-group behavior.
- Add no `type: ignore`, `# pyright`, `Any` import/annotation/cast, Pyright/config change, second
  production helper, test edit, or unrelated refactor.

Reread and attest to the design charter before implementation, between the flag and native-access
features, before verification, and in the final response. Run focused Pyright/Ruff/process-cleanup
checks if available. Do not claim the tester-owned CL-005 work is yours.
