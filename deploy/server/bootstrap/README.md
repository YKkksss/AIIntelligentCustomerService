# 服务器初始化脚本（192.168.110.86）

对应文档：[服务器准备与共享服务](../../../docs/07-deployment/01-服务器准备与共享服务.md)、[Qdrant 部署](../../../docs/07-deployment/02-Qdrant部署.md)。
所有密码通过环境变量或交互输入提供，**不写入任何文件**。

> **2026-10-15 已在服务器完成**：PG 16.15（`db-postgres-1`）库 `aics`、`aics_test` 与角色 `aics`；RabbitMQ 4.3.5 vhost/用户 `aics`；Redis 8.2.9 ACL 用户 `aics`（写在 `/etc/shared-infra/redis.conf`，见下）。Qdrant 不再单独部署，共用 KnowledgeFlow 的 v1.18.2 实例。以下命令用于重建环境或核对。

```bash
# 在服务器上（把 deploy/server 同步到 ~/aics/server）
cd ~/aics/server/bootstrap
./bootstrap.sh check                                   # 只读：内存、磁盘、端口
./bootstrap.sh dirs                                    # /data/aics 目录
PG_ADMIN_USER=<PG_ADMIN_USER> ./bootstrap.sh postgres  # 库 aics、aics_test，角色 aics（交互输入密码）
RABBITMQ_CONTAINER=<容器名> ./bootstrap.sh rabbitmq    # vhost aics + 用户 aics
./bootstrap.sh redis-acl                               # 打印应写入 /etc/shared-infra/redis.conf 的 user 行（由负责人添加并重启 Redis）
```

## Redis 约定（无需建库）
- 库号 5：缓存、限流、幂等（`AICS_REDIS_URL=.../5`）；6：Celery 结果（`AICS_CELERY_RESULT_BACKEND=.../6`）；7：集成测试。
- 所有 key 带 `aics:` 前缀；代码中禁止 `FLUSHDB` / `FLUSHALL` / `KEYS`（用 `SCAN` + 前缀）。
- 实际 ACL（`/etc/shared-infra/redis.conf`）：
  ```
  user aics on >'<AICS_REDIS_PASSWORD>' ~aics:* ~celery-task-meta-* ~chord-unlock-* &aics:* &celery-task-meta-* +@all -@admin -@dangerous
  ```
  因此 `KEYS`、`FLUSHDB`、`FLUSHALL`、`INFO`、`CONFIG` 等命令不可用；AICS 代码只用普通读写、`SCAN`、`PING`、pub/sub。

## 可重复执行
- `postgres.sql` 用 `\gexec` 判断是否存在，已存在时只同步密码与连接上限。
- `rabbitmq` 子命令已存在 vhost / 用户时跳过创建，并更新密码与权限。
