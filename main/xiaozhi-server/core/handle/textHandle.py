from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry
from core.handle.textMessageProcessor import TextMessageProcessor

TAG = __name__

# Registry (bảng đăng ký) các bộ xử lý toàn cục
message_registry = TextMessageHandlerRegistry()

# Tạo instance bộ xử lý message toàn cục
message_processor = TextMessageProcessor(message_registry)


async def handleTextMessage(conn: "ConnectionHandler", message):
    """Xử lý message text"""
    await message_processor.process_message(conn, message)
