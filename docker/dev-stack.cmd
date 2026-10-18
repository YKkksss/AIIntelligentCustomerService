@echo off
rem Convenience entry point for machines whose execution policy blocks .ps1 files.
rem Equivalent to: powershell -NoProfile -ExecutionPolicy Bypass -File dev-stack.ps1 <action>
rem Usage: dev-stack.cmd init | up | down | status | check | logs | test | reset
rem NOTE: keep this file pure ASCII. cmd.exe parses .cmd files with the OEM code page
rem (GBK on Chinese Windows), so non-ASCII comments get mangled and executed as commands.
where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev-stack.ps1" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev-stack.ps1" %*
)
