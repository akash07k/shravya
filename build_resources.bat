@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. Install uv before building resources.
    set "result=1"
    goto finish
)
call uv sync --locked
if errorlevel 1 (
    set "result=%errorlevel%"
    goto finish
)
call uv run --no-sync python -B -m tools.build_resources %*
set "result=%errorlevel%"
:finish
if not "%result%"=="0" echo Resource build failed with exit code %result%.
if "%~1"=="" pause
exit /b %result%
