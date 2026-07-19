$ErrorActionPreference = 'Stop'
$repo = 'C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server'
$root = 'C:\cs5r1'
$env:CLAUDE_CONFIG_DIR = Join-Path $root '.claude-config'
$env:PYTHONUTF8 = '1'
New-Item -ItemType Directory -Force -Path $env:CLAUDE_CONFIG_DIR | Out-Null
Set-Location $root
Get-Content (Join-Path $repo 'docs\evidence\wide-acceptance-claude-sonnet-5-medium-2026-07-18-prompt.md') -Raw |
    claude -p `
        --model claude-sonnet-5 `
        --effort medium `
        --permission-mode bypassPermissions `
        --dangerously-skip-permissions `
        --strict-mcp-config `
        --mcp-config (Join-Path $repo 'docs\evidence\wide-acceptance-claude-sonnet-5-medium-2026-07-18-mcp.json') `
        --no-session-persistence `
        --output-format stream-json `
        --verbose
