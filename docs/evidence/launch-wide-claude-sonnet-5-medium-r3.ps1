$ErrorActionPreference = 'Stop'
$repo = 'C:\Users\Jason\Documents\Jason\FirmCLI\BYO-Server'
$root = 'C:\cs5r3'
$config = 'C:\claude-cs5r3-config'
if (Test-Path -LiteralPath $config) { throw 'task-local Claude config already exists' }
# Sanitized launch record: the one-time task-local authentication bootstrap is intentionally
# omitted. Do not copy persistent credentials from a global config. A caller replaying this record
# must provide an already-authenticated, disposable CLAUDE_CONFIG_DIR and remove it afterward.
throw 'Sanitized evidence record; provision a disposable authenticated config out of band.'
$env:CLAUDE_CONFIG_DIR = $config
$env:PYTHONUTF8 = '1'
Set-Location $root
Get-Content (Join-Path $repo 'docs\evidence\wide-acceptance-claude-sonnet-5-medium-2026-07-18-prompt-r3.md') -Raw |
    claude -p `
        --model claude-sonnet-5 `
        --effort medium `
        --permission-mode bypassPermissions `
        --dangerously-skip-permissions `
        --strict-mcp-config `
        --mcp-config (Join-Path $repo 'docs\evidence\wide-acceptance-claude-sonnet-5-medium-2026-07-18-mcp-r3.json') `
        --setting-sources local `
        --no-session-persistence `
        --output-format stream-json `
        --verbose
