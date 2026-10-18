#!/bin/sh
# 官方 postgres 镜像首次初始化数据目录时执行：创建 AICS 的库与角色。
# 密码从 compose 传入的 AICS_DB_PASSWORD 读取，不落仓库。
set -e

if [ -z "${AICS_DB_PASSWORD:-}" ]; then
  echo "AICS_DB_PASSWORD 未设置：请在 docker/.env 中填写 AICS_DB_PASSWORD" >&2
  exit 1
fi

echo "AICS: 创建库 aics / aics_test 与角色 aics"
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname postgres \
  -v aics_password="$AICS_DB_PASSWORD" \
  -f /docker-entrypoint-initdb.d/aics.sql
