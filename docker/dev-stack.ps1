<#
.SYNOPSIS
  管理 AICS 本机开发依赖服务（PostgreSQL / Redis / RabbitMQ / Qdrant）。

.DESCRIPTION
  在 docker/ 目录下用 docker compose 管理四个依赖服务。数据落在 docker/volumes/ 下（已忽略，不入库）。
  · init   生成 docker/.env（从 .env.example 复制并把占位符换成随机密码）；已存在则不覆盖
  · up     启动（必要时先 init）并等到四个服务健康
  · down   停止（保留数据）
  · status 容器状态
  · check  逐个探活，并打印应用/测试要用的环境变量
  · logs   查看日志（-Service 指定单个服务）
  · test   把 AICS_TEST_* 指向本机服务后跑集成测试（backend 下 pytest -m integration）
  · reset  停止并删除 docker/volumes/ 下的全部数据（需要 -Force 确认，不可恢复）

.EXAMPLE
  .\dev-stack.ps1 up
  .\dev-stack.ps1 test -- -x -k apikeys
#>
param(
  [ValidateSet('init', 'up', 'down', 'status', 'check', 'logs', 'reset', 'test')]
  [string]$Action = 'up',
  [string]$Service = '',
  [switch]$Force,
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Rest
)

# 用 Continue：docker 的原生命令会把进度写到 stderr，Stop 会把它当致命错误中断脚本；
# 真正的失败由 Invoke-Compose 检查退出码并 throw。
$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$envFile = Join-Path $here '.env'
$backend = Join-Path (Split-Path -Parent $here) 'backend'
$containers = @('aics-dev-postgres', 'aics-dev-redis', 'aics-dev-rabbitmq', 'aics-dev-qdrant')

function Invoke-Compose {
  param([string[]]$ComposeArgs)
  & docker compose --env-file $envFile -f (Join-Path $here 'compose.yaml') @ComposeArgs
  if ($LASTEXITCODE -ne 0) { throw "docker compose $($ComposeArgs -join ' ') 失败（退出码 $LASTEXITCODE）" }
}

function Get-StackEnv {
  if (-not (Test-Path $envFile)) { throw "缺少 $envFile：先执行 .\dev-stack.ps1 init" }
  $map = @{}
  foreach ($line in Get-Content $envFile -Encoding UTF8) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#')) { continue }
    $i = $t.IndexOf('=')
    if ($i -gt 0) { $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim() }
  }
  return $map
}

function New-Secret {
  param([int]$Length = 32)
  $chars = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  $bytes = New-Object byte[] $Length
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Invoke-Init {
  if (Test-Path $envFile) { Write-Host "已存在 $envFile，未覆盖"; return }
  $text = Get-Content (Join-Path $here '.env.example') -Raw -Encoding UTF8
  foreach ($key in @('POSTGRES_ADMIN_PASSWORD', 'AICS_DB_PASSWORD', 'AICS_REDIS_PASSWORD', 'AICS_MQ_PASSWORD', 'AICS_QDRANT_API_KEY')) {
    $secret = New-Secret
    $text = [regex]::Replace($text, "(?m)^$key=.*$", "$key=$secret")
  }
  [System.IO.File]::WriteAllText($envFile, $text, (New-Object System.Text.UTF8Encoding($false)))
  Write-Host "已生成 $envFile（随机密码，未入库）"
}

function Wait-Healthy {
  param([int]$TimeoutSeconds = 120)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $pending = @()
    foreach ($name in $containers) {
      $status = docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $name 2>$null
      if (-not $status) { $status = 'missing' }
      if ($status -ne 'healthy' -and $status -ne 'running') { $pending += "$name=$status" }
    }
    if ($pending.Count -eq 0) { Write-Host '四个服务均已就绪'; return }
    Start-Sleep -Seconds 3
  }
  Write-Warning "等待超时，仍未就绪：$($pending -join ', ')（用 .\dev-stack.ps1 logs 查看）"
}

function Invoke-Check {
  $stackEnv = Get-StackEnv
  $dbPort = if ($stackEnv['AICS_DB_PORT']) { $stackEnv['AICS_DB_PORT'] } else { '5432' }
  $redisPort = if ($stackEnv['AICS_REDIS_PORT']) { $stackEnv['AICS_REDIS_PORT'] } else { '6379' }
  $mqPort = if ($stackEnv['AICS_MQ_PORT']) { $stackEnv['AICS_MQ_PORT'] } else { '5672' }
  $qPort = if ($stackEnv['AICS_QDRANT_PORT']) { $stackEnv['AICS_QDRANT_PORT'] } else { '6333' }

  $pg = docker exec aics-dev-postgres pg_isready -U aics -d aics 2>&1
  "postgres  : $pg"
  $redis = docker exec aics-dev-redis sh -c 'redis-cli -u redis://aics:$AICS_REDIS_PASSWORD@127.0.0.1:6379/5 ping' 2>&1
  "redis     : $redis"
  $mq = docker exec aics-dev-rabbitmq rabbitmq-diagnostics -q ping 2>&1
  "rabbitmq  : $mq"
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$qPort/readyz" -Headers @{ 'api-key' = $stackEnv['AICS_QDRANT_API_KEY'] } -TimeoutSec 5 -UseBasicParsing
    "qdrant    : HTTP $($r.StatusCode)"
  } catch { "qdrant    : 失败 $($_.Exception.Message)" }

  Write-Host ''
  Write-Host '应用 .env（仓库根目录）：'
  Write-Host "  AICS_DB_URL=postgresql+asyncpg://aics:$($stackEnv['AICS_DB_PASSWORD'])@127.0.0.1:$dbPort/aics"
  Write-Host "  AICS_REDIS_URL=redis://aics:$($stackEnv['AICS_REDIS_PASSWORD'])@127.0.0.1:$redisPort/5"
  Write-Host "  AICS_CELERY_BROKER_URL=amqp://aics:$($stackEnv['AICS_MQ_PASSWORD'])@127.0.0.1:$mqPort/aics"
  Write-Host "  AICS_CELERY_RESULT_BACKEND=redis://aics:$($stackEnv['AICS_REDIS_PASSWORD'])@127.0.0.1:$redisPort/6"
  Write-Host "  AICS_QDRANT_URL=http://127.0.0.1:$qPort"
  Write-Host "  AICS_QDRANT_API_KEY=$($stackEnv['AICS_QDRANT_API_KEY'])"
  Write-Host '集成测试（AICS_TEST_* 由 dev-stack.ps1 test 自动注入）：'
  Write-Host "  AICS_TEST_DB_URL=postgresql+asyncpg://aics:***@127.0.0.1:$dbPort/aics_test"
  Write-Host "  AICS_TEST_REDIS_URL=redis://aics:***@127.0.0.1:$redisPort/7"
  Write-Host "  AICS_TEST_QDRANT_URL=http://127.0.0.1:$qPort"
}

function Invoke-Test {
  $stackEnv = Get-StackEnv
  $dbPort = if ($stackEnv['AICS_DB_PORT']) { $stackEnv['AICS_DB_PORT'] } else { '5432' }
  $redisPort = if ($stackEnv['AICS_REDIS_PORT']) { $stackEnv['AICS_REDIS_PORT'] } else { '6379' }
  $qPort = if ($stackEnv['AICS_QDRANT_PORT']) { $stackEnv['AICS_QDRANT_PORT'] } else { '6333' }
  $env:AICS_TEST_DB_URL = "postgresql+asyncpg://aics:$($stackEnv['AICS_DB_PASSWORD'])@127.0.0.1:$dbPort/aics_test"
  $env:AICS_TEST_REDIS_URL = "redis://aics:$($stackEnv['AICS_REDIS_PASSWORD'])@127.0.0.1:$redisPort/7"
  $env:AICS_TEST_QDRANT_URL = "http://127.0.0.1:$qPort"
  $env:AICS_TEST_QDRANT_API_KEY = $stackEnv['AICS_QDRANT_API_KEY']
  Push-Location $backend
  try {
    # $Service 是第二个位置参数：`dev-stack.cmd test -- <路径1> <路径2>` 会把路径 1
    # 吞进 $Service（见待解决问题清单 ISSUE-57），所以先把它并回路径列表再转发。
    $paths = @()
    if ($Service) { $paths += $Service }
    $paths += $Rest
    & uv run --frozen pytest -m integration -q @paths
    if ($LASTEXITCODE -ne 0) { Write-Warning "集成测试有失败项（退出码 $LASTEXITCODE）" }
  } finally { Pop-Location }
}

switch ($Action) {
  'init' { Invoke-Init }
  'up' {
    Invoke-Init
    Invoke-Compose @('up', '-d')
    Wait-Healthy
  }
  'down' { Invoke-Compose @('down') }
  'status' { Invoke-Compose @('ps') }
  'check' { Invoke-Check }
  'logs' {
    if ($Service) { Invoke-Compose @('logs', '--tail', '80', $Service) } else { Invoke-Compose @('logs', '--tail', '40') }
  }
  'reset' {
    if (-not $Force) { throw 'reset 会删除 docker/volumes/ 下的全部数据，确认请加 -Force' }
    Invoke-Compose @('down')
    Get-ChildItem (Join-Path $here 'volumes') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      Get-ChildItem $_.FullName -Force | Remove-Item -Recurse -Force
    }
    Write-Host 'volumes/ 已清空，下次 up 会重新初始化数据库与 ACL'
  }
  'test' { Invoke-Test }
}
