# 0011 智能客服头像（DiceBear 前端生成，2026-10-22）

## 背景

管理后台里每个「智能客服」（apps 表的一行）此前只有一个渐变方块 + 首字母，列表卡片与单应用控制台长得几乎一样，切换时不容易一眼区分。用户提出给每个客服一个可换的头像，并给了一份基于 DiceBear 的方案（`avatar_style` / `avatar_seed` / `avatar_url` 三列 + `reka-ui` 渲染 + `@dicebear/*` 生成 + 上传裁剪与表情包作为可选项）。

## 借鉴（收瞄）

- **DiceBear**（`@dicebear/core` + `@dicebear/collection`，MIT）：纯前端按 `style` + `seed` 生成 SVG，同参数永远同一张脸，不需要存图片。
- **reka-ui** 的 `AvatarRoot / AvatarImage / AvatarFallback`：加载失败时回落到首字母。
- **JSONB 配置列**：本项目 `apps` 已有 `rag_config / handoff_config / session_config / webhook_config / chunk_config` 五个 JSONB，PATCH 走「只提交改动键 + `expected_config_version` 乐观校验」，头像作为配置的一部分最自然。

## 不借鉴（理由）

- **三列拆开**（`avatar_style` / `avatar_seed` / `avatar_url`）：本项目配置一律按 JSONB 存，拆三列会让「配置版本号 + 审计 diff + 复制应用」三处逻辑多出特例；JSONB 一个 `avatar` 对象整体进位更简单，将来加字段（缩略图 key、背景色）不用再迁移。
- **内联 SVG 组件**：DiceBear 的 SVG 带 `<mask id="viewboxMask">`，一页渲染多个头像时 id 会互相覆盖，裁成头像框会出错 → 统一走 `toDataUri()` 输出 `<img>`，各自独立。
- **上传裁剪（`vue-advanced-cropper` + 存储）与表情包**：本期不做。`avatar.url` 字段先留好，等真有「上传自己的照片」需求时再连 `backend/src/aics/storage` 做一次独立小迭代；表情属于输入侧交互，等对外挂件（v1.2 规划）再说。
- **CC BY 风格**（`personas` / `adventurer`）：内部使用没问题，但一旦对外就要考虑署名，不值得给未来留债 → 白名单只放 MIT 的五个。

## 我们的补充

- **存储**：迁移 `0017` 给 `apps` 加 `avatar JSONB NOT NULL DEFAULT '{}'`；`{}` 表示「用默认」，读取时按应用 id 派生稳定 seed（`avatar_of()`），历史行无需回填。
- **校验**：`modules/apps/avatar.py` 负责白名单与长度（`style ∈ AVATAR_STYLES`、`seed ≤ 64`、`url` 只认 `http(s)`），422 的 `details.errors` 定位到 `["avatar","style"]`；`PATCH` 提交部分键时与现值合并，所以「只换 seed」不会把风格打回默认。
- **版本与审计**：头像变化走 `update_app` 的统一路径 —— `config_version + 1`、审计 `app.update` 的 `diff` 里带前后值（`AUDIT_PATCH_FIELDS` 已加入 `avatar`）。
- **前端**：`utils/avatar.ts`（白名单、`normalizeAvatar`、`randomSeed`、按风格懒加载 chunk 的 `avatarDataUri`）+ `components/AssistantAvatar.vue`（异步生成、失败回落首字母、缓存同参数结果）；用在列表卡片、控制台头部、配置页「头像」卡（风格下拉 + 「换一个」）。
- **打包**：DiceBear 通过动态 `import()` 进入独立 chunk（`lib-*.js`，gzip ≈278 kB），首屏不加载；五套风格同包，只在第一次画头像时下载一次。

## 最终决定

采用 DiceBear 前端生成 + `apps.avatar` JSONB：默认 `notionists`，另给 `lorelei / openPeeps / thumbs / pixelArt`；换头像 = 换 seed；不存图片、不做上传、不做表情包。`url` 字段预留并优先于生成结果。

## 同步到的文档

- `docs/03-database/01-数据库设计.md`：§3.3 `apps` 增 `avatar` 列，§1.1 迁移到 `0017`。
- `docs/04-api/03-管理接口参考.md`：应用详情 / `PATCH` 的 `avatar` 字段与 422 口径。
- `docs/05-frontend/01-管理后台设计.md`：配置页「头像」卡与列表卡片头像。
- `docs/00-handoff/02-实现现状与代码地图.md`：迁移行、apps 行与前端文件。
- `CHANGELOG.md`：新增 / 测试两节。

## 后续

- 「上传自己的图」（头像 2.0）：接存储 + 裁剪，`avatar.url` 已留好。
- 对外挂件（v1.2）：挂件头图直接复用同一套 `avatar`，不需要再改数据模型。
