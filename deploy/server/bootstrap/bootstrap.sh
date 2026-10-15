#!/usr/bin/env bash
# AICS 服务器初始化（192.168.110.86）。只创建属于 AICS 的库、用户、vhost，不修改共享服务配置。
# 说明见 docs/07-deployment/01-服务器准备与共享服务.md。
#
# 用法：
#   ./bootstrap.sh dirs                 # 创建 /data/aics 目录
#   ./bootstrap.sh postgres             # 需要 PG_ADMIN_USER、AICS_DB_PASSWORD
#   ./bootstrap.sh rabbitmq             # 需要 RABBITMQ_CONTAINER、AICS_MQ_PASSWORD
#   ./bootstrap.sh redis-acl            # 打印 /etc/shared-infra/redis.conf 中的 ACL 行；设置 REDIS_CONTAINER、AICS_REDIS_PASSWORD 时顺带验证
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
  sudo mkdir -p /data/aics/{storage,backups/pg,backups/qdrant,backups/storage,logs}
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
  # 共享 Redis 的 ACL 由配置文件管理（/etc/shared-infra/redis.conf），重启后仍生效；
  # 这里只打印应写入的 user 行（密码用占位符），由服务器负责人添加后重启 Redis。
  cat <<'ACL'
# /etc/shared-infra/redis.conf 中 AICS 的 ACL 行（<AICS_REDIS_PASSWORD> 替换为真实密码）
user aics on >'<AICS_REDIS_PASSWORD>' ~aics:* ~celery-task-meta-* ~chord-unlock-* &aics:* &celery-task-meta-* +@all -@admin -@dangerous
ACL
  echo "URL：redis://aics:<AICS_REDIS_PASSWORD>@192.168.110.86:6379/5（Celery 结果库号 6，测试库号 7）"
  if [[ -n "${REDIS_CONTAINER:-}" && -n "${AICS_REDIS_PASSWORD:-}" ]]; then
    echo "-- 验证 aics 用户：PING 应为 PONG，INFO 应被拒绝（NOPERM）"
    docker exec -i -e REDISCLI_AUTH="$AICS_REDIS_PASSWORD" "$REDIS_CONTAINER" redis-cli --user aics -n 5 PING
    docker exec -i -e REDISCLI_AUTH="$AICS_REDIS_PASSWORD" "$REDIS_CONTAINER" redis-cli --user aics -n 5 INFO server | head -1 || true
  fi
}

cmd_check() {
  free -h; df -h /data 2>/dev/null || df -h /
  docker ps --format '{{.Names}}\t{{.Ports}}'
  ss -ltn | grep -E ':8080\b' || echo "8080 端口空闲"
  curl -s -o /dev/null -w 'qdrant(KnowledgeFlow) readyz: %{http_code}\n' http://127.0.0.1:6333/readyz || true
}

case "${1:-}" in
  dirs) cmd_dirs ;;
  postgres) cmd_postgres ;;
  rabbitmq) cmd_rabbitmq ;;
  redis-acl) cmd_redis_acl ;;
  check) cmd_check ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
