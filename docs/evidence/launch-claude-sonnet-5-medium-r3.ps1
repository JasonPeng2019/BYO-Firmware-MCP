$ErrorActionPreference = "Stop"
$prompt = Get-Content -LiteralPath "C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\docs\evidence\from-scratch-claude-sonnet-5-medium-2026-07-18-prompt-r3.md" -Raw
$prompt | & claude -p --model claude-sonnet-5 --effort medium --permission-mode bypassPermissions --dangerously-skip-permissions --strict-mcp-config --mcp-config "C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server\docs\evidence\from-scratch-claude-sonnet-5-medium-2026-07-18-mcp-r3.json" --no-session-persistence --output-format stream-json --verbose 1> "claude-run.jsonl" 2> "claude-stderr.log"
$LASTEXITCODE | Set-Content -LiteralPath "claude-exit-code.txt"
