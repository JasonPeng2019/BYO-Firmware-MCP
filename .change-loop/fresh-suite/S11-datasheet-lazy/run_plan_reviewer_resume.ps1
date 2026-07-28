$ErrorActionPreference = 'Stop'
$runtime = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $runtime '..\..\..')).Path
$prompt = Join-Path $runtime 'PLAN_REVIEWER_RESUME_PROMPT.md'
$jsonl = Join-Path $runtime 'plan-reviewer-resume.jsonl'
$stderr = Join-Path $runtime 'plan-reviewer-resume.stderr.log'
$last = Join-Path $runtime 'plan-reviewer.last.md'
$status = Join-Path $runtime 'plan-reviewer.status.json'

Set-Location $repo
@{
    state = 'RUNNING'
    controller_pid = $PID
    session_id = '019f9fd5-a27c-7160-af85-1a18091183cf'
    model = 'gpt-5.6-terra'
    reasoning_effort = 'medium'
    service_tier = 'priority'
    started_at = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -Encoding UTF8 $status

$exitCode = 1
try {
    $resumePrompt = Get-Content $prompt -Raw
    & codex exec resume --json --model gpt-5.6-terra `
        --config 'model_reasoning_effort="medium"' `
        --config 'service_tier="priority"' `
        --ignore-user-config `
        --output-last-message $last `
        019f9fd5-a27c-7160-af85-1a18091183cf $resumePrompt `
        1> $jsonl 2> $stderr
    $exitCode = $LASTEXITCODE
}
finally {
    @{
        state = 'EXITED'
        controller_pid = $PID
        session_id = '019f9fd5-a27c-7160-af85-1a18091183cf'
        model = 'gpt-5.6-terra'
        reasoning_effort = 'medium'
        service_tier = 'priority'
        exit_code = $exitCode
        ended_at = (Get-Date).ToString('o')
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $status
}
exit $exitCode
