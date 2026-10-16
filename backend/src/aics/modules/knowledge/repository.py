"""knowledge 模块使用的 repository：实现位于 `aics.ingest.repository`（入库流水线与本模块共用）。"""

from aics.ingest.repository import (
    ChunkParentRepository,
    ChunkQuestionRepository,
    ChunkRepository,
    DocumentRepository,
    JobRepository,
)

__all__ = [
    "ChunkParentRepository",
    "ChunkQuestionRepository",
    "ChunkRepository",
    "DocumentRepository",
    "JobRepository",
]
