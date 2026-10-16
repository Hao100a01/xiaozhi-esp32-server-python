from typing import Dict, Optional

from core.handle.textHandler.abortMessageHandler import AbortTextMessageHandler
from core.handle.textHandler.helloMessageHandler import HelloTextMessageHandler
from core.handle.textHandler.iotMessageHandler import IotTextMessageHandler
from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
from core.handle.textHandler.mcpMessageHandler import McpTextMessageHandler
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textHandler.serverMessageHandler import ServerTextMessageHandler
from core.handle.textHandler.pingMessageHandler import PingMessageHandler

TAG = __name__


class TextMessageHandlerRegistry:
    """Registry (bảng đăng ký) bộ xử lý message"""

    def __init__(self):
        self._handlers: Dict[str, TextMessageHandler] = {}
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        """Đăng ký các bộ xử lý message mặc định"""
        handlers = [
            HelloTextMessageHandler(),
            AbortTextMessageHandler(),
            ListenTextMessageHandler(),
            IotTextMessageHandler(),
            McpTextMessageHandler(),
            ServerTextMessageHandler(),
            PingMessageHandler(),
        ]

        for handler in handlers:
            self.register_handler(handler)

    def register_handler(self, handler: TextMessageHandler) -> None:
        """Đăng ký bộ xử lý message"""
        self._handlers[handler.message_type.value] = handler

    def get_handler(self, message_type: str) -> Optional[TextMessageHandler]:
        """Lấy bộ xử lý message"""
        return self._handlers.get(message_type)

    def get_supported_types(self) -> list:
        """Lấy các loại message được hỗ trợ"""
        return list(self._handlers.keys())
