import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry

TAG = __name__


class TextMessageProcessor:
    """Lớp chính của bộ xử lý message"""

    def __init__(self, registry: TextMessageHandlerRegistry):
        self.registry = registry

    async def process_message(self, conn: "ConnectionHandler", message: str) -> None:
        """Điểm vào chính xử lý message"""
        try:
            # Phân tích message JSON
            msg_json = json.loads(message)

            # Xử lý message JSON
            if isinstance(msg_json, dict):
                message_type = msg_json.get("type")

                # Ghi log
                conn.logger.bind(tag=TAG).info(f"Nhận được message {message_type}: {message}")

                # Lấy và thực thi bộ xử lý
                handler = self.registry.get_handler(message_type)
                if handler:
                    await handler.handle(conn, msg_json)
                else:
                    conn.logger.bind(tag=TAG).error(f"Nhận được message loại không xác định: {message}")
            # Xử lý message số thuần
            elif isinstance(msg_json, int):
                conn.logger.bind(tag=TAG).info(f"Nhận được message số: {message}")
                await conn.websocket.send(message)

        except json.JSONDecodeError:
            # Message không phải JSON thì chuyển tiếp trực tiếp
            conn.logger.bind(tag=TAG).error(f"Phân tích được message lỗi: {message}")
            await conn.websocket.send(message)
