# 服务器初始化脚本（192.168.110.86）

对应文档：[服务器准备与共享服务](../../../docs/07-deployment/01-服务器准备与共享服务.md)、[Qdrant 部署](../../../docs/07-deployment/02-Qdrant部署.md)。
所有密码通过环境变量或交互输入提供，**不写入任何文件**。

```bash
# 在服务器上（把 deploy/server 同步到 ~/aics/server）
cd ~/aics/server/bootstrap
./bootstrap.sh check                                   # 只读：内存、磁盘、端口
./bootstrap.sh dirs                                    # /data/aics 目录
PG_ADMIN_USER=<PG_ADMIN_USER> ./bootstrap.sh postgres  # 库 aics、aics_test，角色 aics（交互输入密码）
RABBITMQ_CONTAINER=<容器名> ./bootstrap.sh rabbitmq    # vhost aics + 用户 aics
REDIS_CONTAINER=<容器名> ./bootstrap.sh redis-acl      # 可选：Redis 启用 ACL 时
cd .. && cp .env.example .env && vi .env               # QDRANT_API_KEY
docker compose -p aics up -d                           # Qdrant
```

## Redis 约定（无需建库）
- 库号 5：缓存、限流、幂等（`AICS_REDIS_URL=.../5`）；6：Celery 结果（`AICS_CELERY_RESULT_BACKEND=.../6`）；7：集成测试。
- 所有 key 带 `aics:` 前缀；代码中禁止 `FLUSHDB` / `FLUSHALL` / `KEYS`（用 `SCAN` + 前缀）。
- 未启用 ACL 时使用共享密码，依靠前缀与库号隔离；启用 ACL 时用 `redis-acl` 子命令创建受限用户。

## 可重复执行
- `postgres.sql` 用 `\gexec` 判断是否存在，已存在时只同步密码与连接上限。
- `rabbitmq` 子命令已存在 vhost / 用户时跳过创建，并更新密码与权限。
