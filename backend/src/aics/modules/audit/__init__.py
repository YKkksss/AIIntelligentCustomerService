"""审计（M4.1 ③）：`PgAuditSink` 实现 `core.audit.AuditSink`，业务在同一条事务里落库。

模块独立性合同禁止 `modules.auth` 直接写 `audit_logs`，所以写入端走端口、由 `main` 把
`PgAuditSink` 注入 `AuthRuntime`；查询端（`GET /api/v1/admin/audit`）由本模块自己的
`admin_router` 提供。审计表只追加：没有更新与删除接口，保留期由 M6.1 的 `purge_expired` 负责。
"""
