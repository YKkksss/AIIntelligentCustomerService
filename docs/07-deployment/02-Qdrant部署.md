# Qdrant 部署

文件：`deploy/server/docker-compose.yml`、`deploy/server/.env.example`。

## 1. 同步与启动
```powershell
# 开发机
scp deploy/server/docker-compose.yml knowledgeflow-server:~/aics/
```
```bash
# 服务器
cd ~/aics
printf 'QDRANT_API_KEY=%s\n' "$(openssl rand -hex 32)" > .env && chmod 600 .env
cat .env      # 把这个值填入开发机 .env 的 AICS_QDRANT_API_KEY
docker compose pull && docker compose up -d
docker compose ps
```
部署前到 Qdrant 官方 releases 确认最新稳定版，修改 compose 中固定的版本号（不使用 `latest`）。

## 2. 验证
```bash
curl -s -H "api-key: <QDRANT_API_KEY>" http://192.168.110.86:6333/collections
docker stats --no-stream aics-qdrant      # 内存应远低于 1GB 上限
```
集合由应用启动时自动创建（见 Qdrant 设计），无需手工建。

## 3. 资源
- 内存上限 1GB、CPU 2 核。按 Qdrant 设计中的量化配置，约可容纳 200 万切块。接近上限（`docker stats` > 80%）时先评估再调高。
- 数据：`/data/aics/qdrant/storage`；快照：`/data/aics/qdrant/snapshots`。

## 4. 升级
`docker compose pull && docker compose up -d`；升级前先做快照（见备份）。跨大版本升级先阅读官方升级说明。
