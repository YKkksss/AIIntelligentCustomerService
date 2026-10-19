"""后台认证与 RBAC（M4.1 ②起；`admin_router` 只服务 `/api/v1/admin`，与开放接口的 Key 认证两套）。

- `passwords`：argon2id 哈希与校验。
- `tokens`：JWT 签发 / 校验（HS256，access 30 分钟、refresh 7 天轮换）。
- `permissions`：24 个点分权限名与三种角色的矩阵（`roles.permissions` 的代码副本）。
- `service`：登录、刷新、登出、改密、锁定与审计。
- `authorizer`：`core.security.Authorizer` 的数据库实现（`require_perm` 依赖它）。
- `repository`：只读全局表（users / roles / user_app_roles / apps）。
"""
