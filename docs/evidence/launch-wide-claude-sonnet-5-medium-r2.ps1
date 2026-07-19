$ErrorActionPreference = 'Stop'
$repo = 'C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server'
$root = 'C:\cs5r2'
Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = '1'
Set-Location $root
Get-Content (Join-Path $repo 'docs\evidence\wide-acceptance-claude-sonnet-5-medium-2026-07-18-prompt-r2.md') -Raw |
    claude -p `
        --model claude-sonnet-5 `
        --effort medium `
        --permission-mode bypassPermissions `
        --dangerously-skip-permissions `
        --strict-mcp-config `
        --mcp-config (Join-Path $repo 'docs\evidence\wide-acceptance-claude-sonnet-5-medium-2026-07-18-mcp-r2.json') `
        --setting-sources local `
        --no-session-persistence `
        --output-format stream-json `
        --verbose
