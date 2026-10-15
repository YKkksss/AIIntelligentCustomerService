# ADR-0009 RunMode 与提示词版本化
- 状态：已接受（2026-10-15）
- 背景：评测要求压缩同步执行以保证可复现，调试需要输出完整 trace，在线请求两者都不需要；若用散落的布尔参数控制，三条路径容易分叉。提示词散落在代码中时，无法回答"这条回答 / 这次评测用的是哪一版提示词"。
- 决策：
  1. rag 图入口参数 `run_mode: RunMode`（`online` / `debug` / `eval`），只控制三件事：压缩同步或异步、trace 输出位置、会话渠道（`api|openai` / `debug` / `eval`）与是否计入统计；**不改变**检索、判断、生成逻辑。
  2. 所有提示词为 `*.md` 文件（`rag/prompts/`、`memory/prompts/`、`modules/eval/prompts/`），YAML front-matter 含 `name`、`version`；正文变化而版本未变时 CI 失败。
  3. `messages.prompt_version` 记录本轮用到的提示词版本串；`eval_runs.prompt_versions` 记录完整 `{name: version}`；评测对比视图展示提示词差异。
- 后果：多一个 front-matter 加载器与 CI 检查；提示词变更可追溯到消息与评测。详见[RAG 流程设计 §2、§5](../03-RAG流程设计.md)、[评测指南](../../06-development/07-评测指南.md)。
