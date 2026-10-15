#!/usr/bin/env bash
# AICS 服务器初始化（192.168.110.86）。只创建属于 AICS 的库、用户、vhost，不修改共享服务配置。
# 说明见 docs/07-deployment/01-服务器准备与共享服务.md。
#
# 用法：
#   ./bootstrap.sh dirs                 # 创建 /data/aics 目录
#   ./bootstrap.sh postgres             # 需要 PG_ADMIN_USER、AICS_DB_PASSWORD
#   ./bootstrap.sh rabbitmq             # 需要 RABBITMQ_CONTAINER、AICS_MQ_PASSWORD
#   ./bootstrap.sh redis-acl            # 可选：Redis 启用 ACL 时；需要 REDIS_CONTAINER、REDIS_ADMIN_PASSWORD、AICS_REDIS_PASSWORD
#   ./bootstrap.sh check                # 只读检查端口、内存、磁盘
# 密码从环境变量读取，未设置时交互输入（不回显）；不要把密码写进本文件或命令历史。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PG_CONTAINER="${PG_CONTAINER:-db-postgres-1}"

need() {  # need VAR "提示"
  local var="$1" prompt="$2"
  if [[ -z "${!var:-}" ]]; then
    read -r -s -p "$prompt: " "$var"; echo
    export "${var?}"
  fi
  [[ -n "${!var}" ]] || { echo "缺少 $var" >&2; exit 1; }
}

cmd_dirs() {
  sudo mkdir -p /data/aics/{qdrant/storage,qdrant/snapshots,storage,backups,logs}
  sudo chown -R "$USER:$USER" /data/aics
  mkdir -p ~/aics
  echo "目录已创建：/data/aics、~/aics"
}

cmd_postgres() {
  : "${PG_ADMIN_USER:?请设置 PG_ADMIN_USER（共享实例管理员，向负责人获取）}"
  need AICS_DB_PASSWORD "AICS 数据库密码"
  docker exec -i -e PGPASSWORD="${PG_ADMIN_PASSWORD:-}" "$PG_CONTAINER" \
    psql -U "$PG_ADMIN_USER" -d postgres -v ON_ERROR_STOP=1 \
    -v aics_password="$AICS_DB_PASSWORD" -f - < "$HERE/postgres.sql"
  echo "验证（开发机）：psql \"postgresql://aics:<AICS_DB_PASSWORD>@192.168.110.86:5432/aics\" -c 'select 1'"
  echo "若连接被 pg_hba.conf 拒绝，请负责人添加：host aics,aics_test aics 192.168.110.0/24 scram-sha-256"
}

cmd_rabbitmq() {
  : "${RABBITMQ_CONTAINER:?请设置 RABBITMQ_CONTAINER（docker ps 查看 rabbitmq 容器名）}"
  need AICS_MQ_PASSWORD "AICS RabbitMQ 密码"
  local r=(docker exec -i "$RABBITMQ_CONTAINER" rabbitmqctl)
  if "${r[@]}" list_vhosts --silent | grep -qx aics; then echo "vhost aics 已存在"; else "${r[@]}" add_vhost aics; fi
  if "${r[@]}" list_users --silent | awk '{print $1}' | grep -qx aics; then
    # 已存在时更新密码（从标准输入读取，避免出现在进程列表）
    printf '%s' "$AICS_MQ_PASSWORD" | docker exec -i "$RABBITMQ_CONTAINER" rabbitmqctl change_password aics
  else
    printf '%s' "$AICS_MQ_PASSWORD" | docker exec -i "$RABBITMQ_CONTAINER" rabbitmqctl add_user aics
  fi
  "${r[@]}" set_permissions -p aics aics ".*" ".*" ".*"
  "${r[@]}" set_user_tags aics management
  echo "验证：http://192.168.110.86:15672 用 aics 登录，只能看到 vhost aics"
}

cmd_redis_acl() {
  : "${REDIS_CONTAINER:?请设置 REDIS_CONTAINER}"
  need REDIS_ADMIN_PASSWORD "Redis 管理密码"
  need AICS_REDIS_PASSWORD "AICS Redis 密码"
  # 只放行 aics: 前缀与 Celery 结果 key；库号 5（缓存/限流/幂等）、6（Celery 结果）、7（测试）
  docker exec -i -e REDISCLI_AUTH="$REDIS_ADMIN_PASSWORD" "$REDIS_CONTAINER" redis-cli \
    ACL SETUSER aics on ">$AICS_REDIS_PASSWORD" '~aics:*' '~celery-task-meta-*' '&*' \
    +@all -@dangerous +select +info
  echo "Redis ACL 用户 aics 已设置；URL：redis://aics:<AICS_REDIS_PASSWORD>@192.168.110.86:6379/5"
  echo "注意：若 Redis 使用配置文件持久化 ACL，需负责人确认 ACL SAVE 或 aclfile 设置"
}

cmd_check() {
  free -h; df -h /data 2>/dev/null || df -h /
  docker ps --format '{{.Names}}\t{{.Ports}}'
  ss -ltn | grep -E ':(6333|6334|8080)\b' || echo "6333/6334/8080 端口空闲"
}

case "${1:-}" in
  dirs) cmd_dirs ;;
  postgres) cmd_postgres ;;
  rabbitmq) cmd_rabbitmq ;;
  redis-acl) cmd_redis_acl ;;
  check) cmd_check ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
