# ADR-0003 任务队列选用 Celery + RabbitMQ
- 状态：已接受
- 背景：需要可靠的后台任务（入库耗时数分钟、评测）；服务器已有 RabbitMQ 4.3。
- 选项：ARQ（仅 Redis、轻量但生态小、无 RabbitMQ）；Dramatiq（支持 RabbitMQ，但定时/监控生态弱）；Celery（成熟、Flower 监控、beat 定时、`acks_late` + 重试）。
- 决策：Celery 5，broker = RabbitMQ vhost `aics`，result backend = Redis db 6。队列：`ingest`、`eval`、`maintenance`。`worker_prefetch_multiplier=1`、`task_acks_late=True`。
- 修订（2026-10-15，见 ADR-0008）：增加队列 `context`（会话压缩 `compact_conversation`、消息向量化 `embed_message`、出站 Webhook 投递 `deliver_webhook`，见 ADR-0011），由独立 worker 进程消费，避免被长时间入库任务阻塞。完整队列清单：`ingest`（入库、替换、重建、重新分词）、`context`、`eval`、`maintenance`（清理、对账、空闲会话扫描 `idle_sweep`、每日统计、保留期清理）。
- 后果：Celery 任务是同步函数，内部用 `asyncio.run` 调用异步 service；Windows 开发需 `--pool=solo`（或 threads）。
