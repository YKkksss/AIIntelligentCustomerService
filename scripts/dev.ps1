# 一键启动开发环境（Windows PowerShell）：api + worker + web 各开一个窗口。
# 用法：在仓库根目录执行 .\scripts\dev.ps1 ；加 -Beat 同时启动 beat。
param([switch]$Beat)
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'

Start-Process powershell -WorkingDirectory $backend -ArgumentList '-NoExit', '-Command', 'uv run aics-api'
Start-Process powershell -WorkingDirectory $backend -ArgumentList '-NoExit', '-Command',
  'uv run celery -A aics.tasks.celery_app worker -Q ingest,context,eval,maintenance --pool=threads -c 4 -l info'
if ($Beat) {
  Start-Process powershell -WorkingDirectory $backend -ArgumentList '-NoExit', '-Command',
    'uv run celery -A aics.tasks.celery_app beat -l info'
}
Start-Process powershell -WorkingDirectory $frontend -ArgumentList '-NoExit', '-Command', 'npm run dev'
Write-Host 'API http://localhost:8000/readyz  ·  后台 http://localhost:5173'
