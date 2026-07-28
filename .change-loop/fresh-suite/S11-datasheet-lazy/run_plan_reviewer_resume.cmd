@echo off
cd /d "%~dp0..\..\.."
codex exec resume --json --model gpt-5.6-terra -c model_reasoning_effort="medium" -c service_tier="priority" --ignore-user-config -o ".change-loop\fresh-suite\S11-datasheet-lazy\plan-reviewer.last.md" 019f9fd5-a27c-7160-af85-1a18091183cf - < ".change-loop\fresh-suite\S11-datasheet-lazy\PLAN_REVIEWER_RESUME_PROMPT.md" > ".change-loop\fresh-suite\S11-datasheet-lazy\plan-reviewer-resume.jsonl" 2> ".change-loop\fresh-suite\S11-datasheet-lazy\plan-reviewer-resume.stderr.log"
echo %ERRORLEVEL% > ".change-loop\fresh-suite\S11-datasheet-lazy\plan-reviewer.exit.code"
