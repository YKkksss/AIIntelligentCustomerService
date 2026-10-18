# 本机开发依赖服务（Docker）

AICS 开发所需的中间件全在本机 Docker 上跑：**PostgreSQL 16 / Redis 8.2 / RabbitMQ 4 / Qdrant v1.18.2**。
库名、角色、vhost、Redis ACL、库号与集合前缀都与服务器共享中间件保持一致（见
[服务器准备与共享服务](../docs/07-deployment/01-服务器准备与共享服务.md)），所以集成测试与真实链路同构。
本项目所有 docker 相关内容都在这个目录下：compose、初始化脚本、挂载目录、管理脚本。

## 目录结构

```
docker/
├─ compose.yaml            # 四个依赖服务（端口只绑 127.0.0.1）
├─ .env.example            # 变量模板；.env 不入库
├─ dev-stack.ps1           # init / up / down / status / check / logs / test / reset
├─ init/postgres/          # 首次初始化：建 aics 角色与 aics / aics_test 库
└─ volumes/                # 数据挂载目录（已忽略，不入库）
   ├─ postgres/  redis/  rabbitmq/  qdrant/
```

## 快速开始

```powershell
cd docker
.\dev-stack.ps1 init        # 生成 .env（随机密码）；或手工 cp .env.example .env
.\dev-stack.ps1 up          # 启动并等四个服务健康
.\dev-stack.ps1 check       # 逐个探活，并打印应用 / 测试要用的环境变量
```

停止用 `.\dev-stack.ps1 down`（保留数据），清空数据用 `.\dev-stack.ps1 reset -Force`，
看日志用 `.\dev-stack.ps1 logs [-Service postgres]`。

## 服务一览

| 服务 | 容器 | 镜像 | 本机地址 | 账号 | 数据目录 |
|---|---|---|---|---|---|
| PostgreSQL | `aics-dev-postgres` | `postgres:16.15-alpine` | `127.0.0.1:5432` | `aics` / `docker/.env` 里的 `AICS_DB_PASSWORD`；库 `aics`、`aics_test` | `volumes/postgres` |
| Redis | `aics-dev-redis` | `redis:8.2.9-alpine` | `127.0.0.1:6379` | 用户 `aics` / `AICS_REDIS_PASSWORD`；库号 5 应用、6 Celery 结果、7 集成测试 | `volumes/redis` |
| RabbitMQ | `aics-dev-rabbitmq` | `rabbitmq:4.3.5-management-alpine` | `127.0.0.1:5672`、管理台 `127.0.0.1:15672` | 用户 `aics` / `AICS_MQ_PASSWORD`；vhost `aics` | `volumes/rabbitmq` |
| Qdrant | `aics-dev-qdrant` | `qdrant/qdrant:v1.18.2` | `127.0.0.1:6333`、gRPC `6334` | API Key = `AICS_QDRANT_API_KEY` | `volumes/qdrant` |

Redis 的 ACL 与服务器同款：`default` 用户关闭，只允许 `aics:*`、`celery-task-meta-*`、`chord-unlock-*`
的键与频道，并且 `-@admin -@dangerous`（`KEYS`/`FLUSHDB`/`INFO` 之类会被拒）。
Qdrant 只允许 `aics_` 前缀的集合（`AICS_QDRANT_COLLECTION_PREFIX` 默认 `aics_`）。

## 接到项目上

1. **应用配置**：仓库根目录的 `.env`（不入库）指向本机服务即可，`dev-stack.ps1 check` 会直接列出这几行：

   ```
   AICS_DB_URL=postgresql+asyncpg://aics:<AICS_DB_PASSWORD>@127.0.0.1:5432/aics
   AICS_REDIS_URL=redis://aics:<AICS_REDIS_PASSWORD>@127.0.0.1:6379/5
   AICS_CELERY_BROKER_URL=amqp://aics:<AICS_MQ_PASSWORD>@127.0.0.1:5672/aics
   AICS_CELERY_RESULT_BACKEND=redis://aics:<AICS_REDIS_PASSWORD>@127.0.0.1:6379/6
   AICS_QDRANT_URL=http://127.0.0.1:6333
   AICS_QDRANT_API_KEY=<AICS_QDRANT_API_KEY>
   ```

2. **起应用**：`.\scripts\dev.ps1 [-Beat]`（api + worker + beat + 前端各开一个窗口），
   或单独 `cd backend; uv run aics-api`。
3. **跑集成测试**（需要真实 PG / Redis / Qdrant）：

   ```powershell
   .\docker\dev-stack.ps1 test            # 自动注入 AICS_TEST_* 后跑 pytest -m integration
   .\docker\dev-stack.ps1 test -- -k apikeys
   ```

   它等价于先设置：

   ```
   AICS_TEST_DB_URL=postgresql+asyncpg://aics:<AICS_DB_PASSWORD>@127.0.0.1:5432/aics_test
   AICS_TEST_REDIS_URL=redis://aics:<AICS_REDIS_PASSWORD>@127.0.0.1:6379/7
   AICS_TEST_QDRANT_URL=http://127.0.0.1:6333
   AICS_TEST_QDRANT_API_KEY=<AICS_QDRANT_API_KEY>
   ```

   集成测试会用 `alembic upgrade head` 把 `aics_test` 迁移到最新，并用 `aics_test_` 前缀的
   Qdrant 集合，测完自行清理；默认的 `pytest` 仍然是单元测试。

## 说明

- 端口只绑 `127.0.0.1`，不对局域网开放；密码在 `docker/.env`，已忽略入库。
- 数据在 `docker/volumes/`（已忽略）：想从零重来就 `reset -Force`，再 `up`。
- 本机是**独占**实例，服务器上 AICS 与 KnowledgeFlow 共用中间件；这也是两者的唯一差别。
  因此本机不需要 `AICS_TRUSTED_PROXIES`（没有反向代理）。
- 首次 `up` 之后如果改了 `docker/.env` 里的密码：Postgres 与 Redis 需要 `reset -Force` 重建
  （初始化脚本与 ACL 只在数据目录为空时执行），RabbitMQ 需要删掉 `volumes/rabbitmq` 再起。
- 真实模型（DeepSeek / 百炼）仍然走外网，需要 `AICS_LLM_API_KEY`、`AICS_EMBED_API_KEY`、
  `AICS_RERANK_API_KEY`；集成测试用假模型，不需要真 Key。
- 两个脚本的**编码约定**（改了会坏，见待解决问题清单 ISSUE-51）：`dev-stack.cmd` 必须保持
  **纯 ASCII**——cmd.exe 用 OEM 代码页（中文 Windows 是 GBK）解析 `.cmd`，UTF-8 中文注释会被
  拆错并当成命令执行；`dev-stack.ps1` 带 **UTF-8 BOM**——本机通常没有 `pwsh`，实际由
  Windows PowerShell 5.1 执行，无 BOM 时它按 ANSI 读脚本、中文提示会乱码。
- 本机没有 `pwsh` 时 `.cmd` 自动回退到 `powershell.exe`（5.1），行为一致，只是启动略慢。
