@echo off
setlocal EnableExtensions

pushd "%~dp0" >nul 2>&1
if not errorlevel 1 goto directory_opened
echo Error: Could not open the Shravya repository directory.
endlocal
exit /b 1

:directory_opened
where uv >nul 2>&1
if not errorlevel 1 goto uv_found
set "EXIT_CODE=1"
set "ERROR_MESSAGE=uv was not found on PATH. Install uv before building the add-on."
goto failed

:uv_found
echo Synchronizing build dependencies...
uv sync --locked --no-install-project
if not errorlevel 1 goto dependencies_synced
set "EXIT_CODE=%ERRORLEVEL%"
set "ERROR_MESSAGE=Dependency synchronization failed."
goto failed

:dependencies_synced
echo Building Shravya...
uv run --no-sync scons
if not errorlevel 1 goto build_completed
set "EXIT_CODE=%ERRORLEVEL%"
set "ERROR_MESSAGE=Add-on packaging failed."
goto failed

:build_completed
echo Build completed: dist\
popd
endlocal
exit /b 0

:failed
echo Error: %ERROR_MESSAGE%
popd
endlocal & exit /b %EXIT_CODE%
