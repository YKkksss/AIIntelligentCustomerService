"""ORM 模型；Alembic 通过导入本包拿到完整 metadata。"""

from aics.db.models.apikey import ApiKey
from aics.db.models.app import App, UserAppRole
from aics.db.models.conversation import Conversation, Message, SystemSetting
from aics.db.models.event import ConversationEvent, WebhookDelivery
from aics.db.models.fact import ConversationFact, ConversationFactHistory
from aics.db.models.feedback import Feedback
from aics.db.models.job import Job
from aics.db.models.knowledge import Chunk, ChunkParent, ChunkQuestion, Document
from aics.db.models.user import Role, User

__all__ = [
    "ApiKey",
    "App",
    "Chunk",
    "ChunkParent",
    "ChunkQuestion",
    "Conversation",
    "ConversationEvent",
    "ConversationFact",
    "ConversationFactHistory",
    "Document",
    "Feedback",
    "Job",
    "Message",
    "Role",
    "SystemSetting",
    "User",
    "UserAppRole",
    "WebhookDelivery",
]
