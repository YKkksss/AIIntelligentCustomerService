# deploy/dev 已废弃

本机开发的依赖服务（PostgreSQL / Redis / RabbitMQ / Qdrant）已统一收到仓库根目录的 `docker/` 下用 `docker compose` 管理（`docker/compose.yaml`、`docker/dev-stack.cmd`）。

- 用法与说明：[docker/README.md](../../docker/README.md)
- 开发环境搭建步骤：[docs/06-development/02-开发环境搭建.md](../../docs/06-development/02-开发环境搭建.md)

原来的 `deploy/dev/docker-compose.yml`（只起 Qdrant）与 `deploy/dev/.env.example` 已被 `docker/` 取代并删除（历史版本见 git）。本目录只保留这个指针；服务器端的 `deploy/server/`（bootstrap 脚本与 SQL）与 Docker 无关，继续保留。
