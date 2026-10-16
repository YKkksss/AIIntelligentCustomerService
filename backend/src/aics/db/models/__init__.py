"""ORM 模型；Alembic 通过导入本包拿到完整 metadata。"""

from aics.db.models.app import App, UserAppRole
from aics.db.models.conversation import Conversation, Message, SystemSetting
from aics.db.models.fact import ConversationFact, ConversationFactHistory
from aics.db.models.job import Job
from aics.db.models.knowledge import Chunk, ChunkParent, ChunkQuestion, Document
from aics.db.models.user import Role, User

__all__ = [
    "App",
    "Chunk",
    "ChunkParent",
    "ChunkQuestion",
    "Conversation",
    "ConversationFact",
    "ConversationFactHistory",
    "Document",
    "Job",
    "Message",
    "Role",
    "SystemSetting",
    "User",
    "UserAppRole",
]
