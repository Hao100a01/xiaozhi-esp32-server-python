from abc import abstractmethod, ABC
from typing import Dict, Any

from core.handle.textMessageType import TextMessageType

TAG = __name__


class TextMessageHandler(ABC):
    """Lớp cơ sở trừu tượng của bộ xử lý message"""

    @abstractmethod
    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        """Phương thức trừu tượng xử lý message"""
        pass

    @property
    @abstractmethod
    def message_type(self) -> TextMessageType:
        """Trả về loại message được xử lý"""
        pass
