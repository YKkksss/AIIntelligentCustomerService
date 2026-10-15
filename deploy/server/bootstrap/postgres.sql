-- AICS 在共享 PostgreSQL（容器 db-postgres-1）上的库与用户。可重复执行。
-- 用法（服务器上，由 bootstrap.sh 调用；也可手动）：
--   docker exec -i db-postgres-1 psql -U <PG_ADMIN_USER> -d postgres \
--     -v ON_ERROR_STOP=1 -v aics_password='<AICS_DB_PASSWORD>' -f - < postgres.sql
-- 只创建 aics / aics_test 两个库与 aics 角色，不改动其他库与实例配置。

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE aics LOGIN PASSWORD %L CONNECTION LIMIT 40', :'aics_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aics')
\gexec

-- 已存在时同步密码与连接上限（便于轮换密码）
SELECT format('ALTER ROLE aics LOGIN PASSWORD %L CONNECTION LIMIT 40', :'aics_password')
\gexec

SELECT 'CREATE DATABASE aics OWNER aics ENCODING ''UTF8'' TEMPLATE template0'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aics')
\gexec

-- 测试库（集成测试用）
SELECT 'CREATE DATABASE aics_test OWNER aics ENCODING ''UTF8'' TEMPLATE template0'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aics_test')
\gexec

REVOKE ALL ON DATABASE aics FROM PUBLIC;
REVOKE ALL ON DATABASE aics_test FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE aics, aics_test TO aics;

\c aics
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO aics;

\c aics_test
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO aics;

\echo 'AICS: PostgreSQL 库 aics / aics_test 与角色 aics 已就绪'
