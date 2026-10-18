-- AICS 在本机 PostgreSQL 上的库与角色，与服务器 deploy/server/bootstrap/postgres.sql 等价。
-- 由官方 postgres 镜像的入口脚本在首次初始化数据目录时执行（见 01-aics.sh）：
--   psql -v ON_ERROR_STOP=1 -U postgres -d postgres -v aics_password=... -f aics.sql
-- 只创建 aics / aics_test 两个库与 aics 角色，不改动实例配置。

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE aics LOGIN PASSWORD %L CONNECTION LIMIT 40', :'aics_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aics')
\gexec

ALTER ROLE aics LOGIN PASSWORD :'aics_password' CONNECTION LIMIT 40;

SELECT 'CREATE DATABASE aics OWNER aics ENCODING ''UTF8'' TEMPLATE template0'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aics')
\gexec

-- 测试库（集成测试用；测试库名固定为 aics_test）
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

\echo 'AICS: 本机 PostgreSQL 的 aics / aics_test 库与 aics 角色已就绪'
