$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server'
$root = 'C:\stm32-claude-rtos-r3'
$env:PYTHONUTF8 = '1'
Set-Location $root
Get-Content (Join-Path $repo 'docs\evidence\stm32-claude-sonnet-5-medium-rtos-prompt-2026-07-18.md') -Raw |
    claude -p --model claude-sonnet-5 --effort medium --permission-mode bypassPermissions --dangerously-skip-permissions --strict-mcp-config --mcp-config (Join-Path $repo 'docs\evidence\stm32-claude-sonnet-5-medium-rtos-mcp-r3.json') --setting-sources local --no-session-persistence --output-format stream-json --verbose 1> (Join-Path $root 'claude-run.jsonl') 2> (Join-Path $root 'claude-stderr.log')
$LASTEXITCODE | Set-Content -LiteralPath (Join-Path $root 'claude-exit-code.txt')
exit $LASTEXITCODE
