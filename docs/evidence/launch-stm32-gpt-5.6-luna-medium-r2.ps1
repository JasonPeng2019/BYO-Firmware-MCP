$ErrorActionPreference = "Continue"
$root = "C:\stm32-gpt56-boot-r2"
$repo = "C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server"
$prompt = Get-Content -LiteralPath "$repo\docs\evidence\stm32-gpt-5.6-luna-medium-bootloader-prompt-2026-07-18.md" -Raw
$env:BYO_MCP_ARTIFACT_ROOT = $root
$env:PYOCD_MCP_RUNS_ROOT = "$root\runs"
$env:PYTHONPATH = "$repo\src"
$env:PYTHONUTF8 = "1"
$args = @(
  '--model','gpt-5.6-luna','--config','model_reasoning_effort="medium"',
  '--dangerously-bypass-approvals-and-sandbox','--cd',$root,
  '--config','mcp_servers.pyocd-debug.command="C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\.venv\Scripts\python.exe"',
  '--config',"mcp_servers.pyocd-debug.args=['-m','pyocd_debug_mcp.server']",
  '--config',"mcp_servers.pyocd-debug.env.BYO_MCP_ARTIFACT_ROOT='$root'",
  '--config',"mcp_servers.pyocd-debug.env.PYOCD_MCP_RUNS_ROOT='$root\runs'",
  '--config',"mcp_servers.pyocd-debug.env.PYTHONPATH='$repo\src'",
  '--config',"mcp_servers.pyocd-debug.env.PYTHONUTF8='1'",
  'exec','--ignore-user-config','--ignore-rules','--ephemeral','--json',
  '--output-last-message',"$root\journey-final.txt",'-'
)
$prompt | & codex @args 1> "$root\codex-run.jsonl" 2> "$root\codex-stderr.log"
$LASTEXITCODE | Set-Content -LiteralPath "$root\codex-exit-code.txt"
exit $LASTEXITCODE


