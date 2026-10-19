export interface paths {
    "/healthz": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Healthz */
        get: operations["healthz_healthz_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/readyz": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Readyz */
        get: operations["readyz_readyz_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/chat": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 对话（阻塞返回，或 SSE 流式） */
        post: operations["chat_api_v1_apps__app_id__chat_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/conversations/{conversation_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * 会话事件流（SSE，支持 Last-Event-ID 补发）
         * @description 会话事件流（SSE）：补发 `Last-Event-ID` 之后的事件，然后按频道通知推送。
         *
         *     会话不存在（含跨应用）在开流之前就 404，响应是普通 JSON 错误体；流一旦开始，错误只能
         *     体现为连接结束（客户端带 `Last-Event-ID` 重连补发）。
         */
        get: operations["stream_events_api_v1_apps__app_id__conversations__conversation_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/conversations/{conversation_id}/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 会话消息（游标分页，旧→新） */
        get: operations["list_messages_api_v1_apps__app_id__conversations__conversation_id__messages_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/documents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 文档列表（游标分页） */
        get: operations["list_documents_api_v1_apps__app_id__documents_get"];
        put?: never;
        /**
         * 上传文档（multipart，超过上限立即 413）
         * @description 上传文档（FR-KB-01）：落盘 + 建 job + 投递入队；同应用内容重复返回 200 + `duplicate`。
         */
        post: operations["upload_document_api_v1_apps__app_id__documents_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/documents/{document_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 文档详情（含处理状态与错误） */
        get: operations["get_document_api_v1_apps__app_id__documents__document_id__get"];
        put?: never;
        post?: never;
        /**
         * 删除文档（逻辑删除，向量与切块一并清理）
         * @description 删除文档（FR-KB-09）：204；立即不可检索，异步清理（`aics.cleanup_document`）。
         */
        delete: operations["delete_document_api_v1_apps__app_id__documents__document_id__delete"];
        options?: never;
        head?: never;
        /**
         * 更新文档元数据（分类、标签等）
         * @description 改标题 / 元数据（FR-KB-01）：立即生效，无需重建。
         */
        patch: operations["patch_document_api_v1_apps__app_id__documents__document_id__patch"];
        trace?: never;
    };
    "/api/v1/apps/{app_id}/documents/{document_id}/chunks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 文档切块列表（含父块与补充问题） */
        get: operations["list_chunks_api_v1_apps__app_id__documents__document_id__chunks_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/documents/url": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 从 URL 导入网页（阻断私网地址）
         * @description URL 导入（FR-KB-02）：抓取在 service 里做（`core.netguard` 校验 + 15 s 超时）。
         */
        post: operations["import_document_url_api_v1_apps__app_id__documents_url_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/documents/{document_id}/file": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /**
         * 替换文档文件（新版本，旧向量作废）
         * @description 替换文档（FR-KB-08）：文档 ID 不变；内容相同 → 200 `unchanged`；与别的文档重复 → 409。
         */
        put: operations["replace_document_file_api_v1_apps__app_id__documents__document_id__file_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/documents/{document_id}/reindex": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 重新索引（切块配置变更后使用）
         * @description 重建文档（FR-KB-06）：202 + job；构建期间旧版本继续可检索。
         */
        post: operations["reindex_document_api_v1_apps__app_id__documents__document_id__reindex_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * 查询会话的转人工状态
         * @description 转人工状态（§3.3）：已关闭的会话也能读，`handoff_status` 保留最后一个值。
         */
        get: operations["get_handoff_api_v1_apps__app_id__conversations__conversation_id__handoff_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/accept": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 坐席接入（并发只有一个成功）
         * @description 抢单接入（`pending → accepted`）；被别人抢先 → 409 `HANDOFF_STATE_CONFLICT`。
         */
        post: operations["accept_handoff_api_v1_apps__app_id__conversations__conversation_id__handoff_accept_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 坐席发送人工消息
         * @description 坐席发言：写 `role=human_agent` 消息，返回消息对象（201）。
         */
        post: operations["post_agent_message_api_v1_apps__app_id__conversations__conversation_id__handoff_messages_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/release": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 坐席交还（AI 恢复接管）
         * @description 交还 AI（`pending|accepted → released`）：下一轮起 AI 恢复应答。
         */
        post: operations["release_handoff_api_v1_apps__app_id__conversations__conversation_id__handoff_release_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/handoffs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * 转人工队列（按状态分页，id 升序游标）
         * @description 坐席平台补偿拉取：`pending` / `accepted` 的会话，`id` 升序游标分页。
         */
        get: operations["list_handoffs_api_v1_apps__app_id__handoffs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/apps/{app_id}/messages/{message_id}/feedback": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 给 assistant 回答点赞或点踩（同一消息覆盖写）
         * @description 点赞 / 点踩一条 assistant 回答；同一消息重复提交按覆盖写（`id` 不变）。
         */
        post: operations["put_feedback_api_v1_apps__app_id__messages__message_id__feedback_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/auth/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 登录（邮箱 + 密码，返回 access / refresh 与可见应用） */
        post: operations["login_api_v1_admin_auth_login_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/auth/refresh": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 用 refresh token 换新的一对 token（旧的立即失效） */
        post: operations["refresh_api_v1_admin_auth_refresh_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/auth/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 登出（撤销 refresh token；access 到期自然失效） */
        post: operations["logout_api_v1_admin_auth_logout_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/auth/me": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 当前登录用户与可见应用（含权限名） */
        get: operations["me_api_v1_admin_auth_me_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/auth/password": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 修改自己的密码（成功后旧 token 全部失效，返回新的一对） */
        post: operations["change_password_api_v1_admin_auth_password_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/users": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 用户列表（可按邮箱 / 姓名模糊搜索） */
        get: operations["list_users_api_v1_admin_users_get"];
        put?: never;
        /** 新建后台用户（首次登录需改密） */
        post: operations["create_user_api_v1_admin_users_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/users/{user_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** 删除用户（软删除，立即失效其 token） */
        delete: operations["delete_user_api_v1_admin_users__user_id__delete"];
        options?: never;
        head?: never;
        /** 修改用户（姓名 / 状态 / 超管 / 重置密码；改密或禁用会踢掉旧 token） */
        patch: operations["patch_user_api_v1_admin_users__user_id__patch"];
        trace?: never;
    };
    "/api/v1/admin/users/{user_id}/reset-password": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 重置用户密码（返回一次性临时密码，用户首登须改密） */
        post: operations["reset_password_api_v1_admin_users__user_id__reset_password_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/apps/{app_id}/members": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 应用成员（用户 + 角色） */
        get: operations["list_members_api_v1_admin_apps__app_id__members_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/apps/{app_id}/members/{user_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** 新增或改派应用成员角色（撤销后立即生效） */
        put: operations["put_member_api_v1_admin_apps__app_id__members__user_id__put"];
        post?: never;
        /** 移除应用成员（立即失效其权限缓存） */
        delete: operations["delete_member_api_v1_admin_apps__app_id__members__user_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/audit": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 审计日志（id 降序游标分页） */
        get: operations["list_audit_api_v1_admin_audit_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/admin/audit/actions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 审计里出现过的动作名（筛选用） */
        get: operations["list_actions_api_v1_admin_audit_actions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/models": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * 模型列表（OpenAI 兼容）
         * @description 只返回当前 Key 能用的模型：它所属的应用 id（不暴露模型注册表）。
         */
        get: operations["list_models_v1_models_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/chat/completions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 对话补全（OpenAI 兼容，支持 stream）
         * @description 阻塞与流式共用一条路径：`stream=true` 走 SSE，每个 delta 一个 `chat.completion.chunk`。
         */
        post: operations["chat_completions_v1_chat_completions_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * AcceptBody
         * @description `POST …/handoff/accept`：抢单接入（`pending → accepted`）。
         */
        AcceptBody: {
            /** Agent Id */
            agent_id: string;
            /** Agent Name */
            agent_name?: string | null;
        };
        /**
         * AgentMessageBody
         * @description `POST …/handoff/messages`：坐席发言（仅接入的坐席本人）。
         */
        AgentMessageBody: {
            /** Agent Id */
            agent_id: string;
            /** Content */
            content: string;
        };
        /** Body_replace_document_file_api_v1_apps__app_id__documents__document_id__file_put */
        Body_replace_document_file_api_v1_apps__app_id__documents__document_id__file_put: {
            /** File */
            file: string;
        };
        /** Body_upload_document_api_v1_apps__app_id__documents_post */
        Body_upload_document_api_v1_apps__app_id__documents_post: {
            /** File */
            file: string;
            /** Title */
            title?: string | null;
            /** Metadata */
            metadata?: string | null;
        };
        /** ChangePasswordBody */
        ChangePasswordBody: {
            /** Current Password */
            current_password: string;
            /** New Password */
            new_password: string;
        };
        /**
         * ChatCompletionRequest
         * @description `POST /v1/chat/completions` 的请求体（不支持的字段忽略，开放接口参考 §3）。
         */
        ChatCompletionRequest: {
            /** Model */
            model: string;
            /** Messages */
            messages?: components["schemas"]["ChatMessage"][];
            /**
             * Stream
             * @default false
             */
            stream: boolean;
            /** User */
            user?: string | null;
            /** Temperature */
            temperature?: number | null;
            /** Max Tokens */
            max_tokens?: number | null;
        };
        /** ChatMessage */
        ChatMessage: {
            /** Role */
            role: string;
            /** Content */
            content?: string | components["schemas"]["ContentPart"][] | null;
        };
        /**
         * ChatRequest
         * @description `POST /api/v1/apps/{app_id}/chat` body. Unknown fields are ignored (forward compatible).
         */
        ChatRequest: {
            /** Question */
            question: string;
            /** Conversation Id */
            conversation_id?: string | null;
            /** User Id */
            user_id?: string | null;
            /**
             * Stream
             * @default false
             */
            stream: boolean;
            /** Metadata */
            metadata?: {
                [key: string]: unknown;
            };
        };
        /** ContentPart */
        ContentPart: {
            /** Type */
            type?: string | null;
            /** Text */
            text?: string | null;
        };
        /** DocumentPatch */
        DocumentPatch: {
            /** Title */
            title?: string | null;
            /** Metadata */
            metadata?: {
                [key: string]: unknown;
            } | null;
        };
        /**
         * ErrorEnvelope
         * @description 原生接口的统一错误体（接口规范 §4）。
         */
        ErrorEnvelope: {
            error: components["schemas"]["ErrorItem"];
        };
        /**
         * ErrorItem
         * @description 错误体的内层对象。
         */
        ErrorItem: {
            /**
             * Code
             * @description 稳定的业务错误码，接入方按它分支
             * @example VALIDATION_ERROR
             */
            code: string;
            /**
             * Message
             * @description 给人看的中文说明
             * @example 参数校验失败
             */
            message: string;
            /**
             * Details
             * @description 结构化补充信息（如 `errors`、`message_id`、`required_scope`）
             */
            details?: {
                [key: string]: unknown;
            };
            /**
             * Request Id
             * @description 与响应头 `X-Request-ID` 一致
             */
            request_id?: string | null;
        };
        /**
         * FeedbackBody
         * @description `POST …/messages/{message_id}/feedback`：点赞 `1` / 点踩 `-1`，评论可选。
         *
         *     `rating` 用 `Literal[1, -1]` 而不是 `int` + 范围校验：越界在 pydantic 层就是 422
         *     `VALIDATION_ERROR`，与数据库的 CHECK 双向对齐、错误体也不用自己拼。
         */
        FeedbackBody: {
            /**
             * Rating
             * @enum {integer}
             */
            rating: 1 | -1;
            /** Comment */
            comment?: string | null;
        };
        /** LoginBody */
        LoginBody: {
            /** Email */
            email: string;
            /** Password */
            password: string;
        };
        /** LogoutBody */
        LogoutBody: {
            /** Refresh Token */
            refresh_token?: string | null;
        };
        /** MemberBody */
        MemberBody: {
            /**
             * Role Id
             * @enum {string}
             */
            role_id: "app_admin" | "viewer";
        };
        /** RefreshBody */
        RefreshBody: {
            /** Refresh Token */
            refresh_token: string;
        };
        /**
         * ReleaseBody
         * @description `POST …/handoff/release`：交还 AI（`pending|accepted → released`）。
         */
        ReleaseBody: {
            /** Agent Id */
            agent_id: string;
            /** Note */
            note?: string | null;
        };
        /** UrlImportBody */
        UrlImportBody: {
            /** Url */
            url: string;
            /** Title */
            title?: string | null;
        };
        /** UserCreateBody */
        UserCreateBody: {
            /** Email */
            email: string;
            /** Display Name */
            display_name: string;
            /** Password */
            password: string;
            /**
             * Is Super Admin
             * @default false
             */
            is_super_admin: boolean;
        };
        /** UserPatchBody */
        UserPatchBody: {
            /** Display Name */
            display_name?: string | null;
            /** Status */
            status?: ("active" | "disabled") | null;
            /** Is Super Admin */
            is_super_admin?: boolean | null;
            /** Password */
            password?: string | null;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    healthz_healthz_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: string;
                    };
                };
            };
        };
    };
    readyz_readyz_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    chat_api_v1_apps__app_id__chat_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChatRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    stream_events_api_v1_apps__app_id__conversations__conversation_id__events_get: {
        parameters: {
            query?: never;
            header?: {
                "Last-Event-ID"?: string | null;
            };
            path: {
                app_id: string;
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_messages_api_v1_apps__app_id__conversations__conversation_id__messages_get: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path: {
                app_id: string;
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_documents_api_v1_apps__app_id__documents_get: {
        parameters: {
            query?: {
                status?: ("queued" | "parsing" | "indexing" | "done" | "failed" | "deleting") | null;
                q?: string | null;
                page?: number;
                page_size?: number;
            };
            header?: never;
            path: {
                app_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    upload_document_api_v1_apps__app_id__documents_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": components["schemas"]["Body_upload_document_api_v1_apps__app_id__documents_post"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 上传文件超过 `AICS_UPLOAD_MAX_MB` */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    get_document_api_v1_apps__app_id__documents__document_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                document_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    delete_document_api_v1_apps__app_id__documents__document_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                document_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    patch_document_api_v1_apps__app_id__documents__document_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                document_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DocumentPatch"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_chunks_api_v1_apps__app_id__documents__document_id__chunks_get: {
        parameters: {
            query?: {
                page?: number;
                page_size?: number;
            };
            header?: never;
            path: {
                app_id: string;
                document_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    import_document_url_api_v1_apps__app_id__documents_url_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UrlImportBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    replace_document_file_api_v1_apps__app_id__documents__document_id__file_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                document_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": components["schemas"]["Body_replace_document_file_api_v1_apps__app_id__documents__document_id__file_put"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 上传文件超过 `AICS_UPLOAD_MAX_MB` */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    reindex_document_api_v1_apps__app_id__documents__document_id__reindex_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                document_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    get_handoff_api_v1_apps__app_id__conversations__conversation_id__handoff_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    accept_handoff_api_v1_apps__app_id__conversations__conversation_id__handoff_accept_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AcceptBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    post_agent_message_api_v1_apps__app_id__conversations__conversation_id__handoff_messages_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentMessageBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    release_handoff_api_v1_apps__app_id__conversations__conversation_id__handoff_release_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReleaseBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_handoffs_api_v1_apps__app_id__handoffs_get: {
        parameters: {
            query?: {
                status?: "pending" | "accepted";
                cursor?: string | null;
                page_size?: number;
            };
            header?: never;
            path: {
                app_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    put_feedback_api_v1_apps__app_id__messages__message_id__feedback_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                message_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FeedbackBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    login_api_v1_admin_auth_login_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LoginBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 错误 */
            423: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    refresh_api_v1_admin_auth_refresh_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RefreshBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 超过速率或配额限制，带 `Retry-After` */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    logout_api_v1_admin_auth_logout_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LogoutBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    me_api_v1_admin_auth_me_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    change_password_api_v1_admin_auth_password_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChangePasswordBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_users_api_v1_admin_users_get: {
        parameters: {
            query?: {
                q?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    create_user_api_v1_admin_users_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UserCreateBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    delete_user_api_v1_admin_users__user_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                user_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    patch_user_api_v1_admin_users__user_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                user_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UserPatchBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    reset_password_api_v1_admin_users__user_id__reset_password_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                user_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_members_api_v1_admin_apps__app_id__members_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    put_member_api_v1_admin_apps__app_id__members__user_id__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                user_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MemberBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    delete_member_api_v1_admin_apps__app_id__members__user_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                app_id: string;
                user_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_audit_api_v1_admin_audit_get: {
        parameters: {
            query?: {
                app_id?: string | null;
                action?: string | null;
                actor_id?: string | null;
                cursor?: string | null;
                page_size?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_actions_api_v1_admin_audit_actions_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（Key 不存在、已吊销、已过期） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 没有权限（scope 不足、应用停用、IP 不在白名单） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 对象不存在（含「不属于本应用」的情况） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
            /** @description 请求体或参数校验失败，`details.errors` 列出字段 */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorEnvelope"];
                };
            };
        };
    };
    list_models_v1_models_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 缺少或无效凭证（`type=authentication_error`） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description scope 不足或应用停用（`type=permission_error`） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description `model` 与 Key 所属应用不一致（`code=model_not_found`） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    chat_completions_v1_chat_completions_post: {
        parameters: {
            query?: never;
            header?: {
                "X-AICS-Conversation-Id"?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChatCompletionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description 请求不合法（OpenAI 风格错误体，`type=invalid_request_error`） */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效凭证（`type=authentication_error`） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description scope 不足或应用停用（`type=permission_error`） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description `model` 与 Key 所属应用不一致（`code=model_not_found`） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 请求体校验失败（`type=invalid_request_error`） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 超过速率或配额限制（`type=rate_limit_error`） */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
}
