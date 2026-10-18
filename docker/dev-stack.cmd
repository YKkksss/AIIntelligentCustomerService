@echo off
rem 便捷入口：在受执行策略限制的机器上也能直接跑（等价于 powershell -ExecutionPolicy Bypass -File dev-stack.ps1）
rem 用法：dev-stack.cmd up | down | status | check | logs | test | reset
where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev-stack.ps1" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev-stack.ps1" %*
)
