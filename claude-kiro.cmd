@echo off
REM Launch Claude Code through the local kiroproxy.
REM Starts the proxy if it is not already listening on the port.

set "DIR=%~dp0"
set "PORT=9100"

powershell -NoProfile -Command "if (-not (Test-NetConnection -ComputerName 127.0.0.1 -Port %PORT% -InformationLevel Quiet -WarningAction SilentlyContinue)) { Start-Process -WindowStyle Hidden python -ArgumentList '%DIR%kiroproxy.py','--port','%PORT%' }"

set "CLAUDE_CODE_USE_BEDROCK="
set "ANTHROPIC_BASE_URL=http://127.0.0.1:%PORT%"
set "ANTHROPIC_AUTH_TOKEN=kiro-local"
set "ANTHROPIC_MODEL=claude-opus-5[1m]"
set "ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5-20251001"

claude --settings "%DIR%claude-kiro-settings.json" %*
