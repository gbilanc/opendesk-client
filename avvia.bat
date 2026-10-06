@echo off
REM avvia.bat - avvia rapidamente OpenDesk dal repository.
REM Uso: avvia.bat [opzioni opendesk]
setlocal
set "DIR=%~dp0"

if exist "%DIR%.venv\Scripts\opendesk.exe" (
    "%DIR%.venv\Scripts\opendesk.exe" %*
) else (
    uv run --directory "%DIR%" opendesk %*
)
