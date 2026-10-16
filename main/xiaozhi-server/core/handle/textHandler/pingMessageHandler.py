import json
import time
from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

TAG = __name__


class PingMessageHandler(TextMessageHandler):
    """Bộ xử lý message Ping, dùng để duy trì kết nối WebSocket"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.PING

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        """
        Xử lý message PING, gửi phản hồi PONG
        Định dạng message: {"type": "ping"}
        Args:
            conn: Đối tượng kết nối WebSocket
            msg_json: Dữ liệu JSON của message PING
        """
        # Kiểm tra đã bật tính năng heartbeat WebSocket chưa
        enable_websocket_ping = conn.config.get("enable_websocket_ping", False)
        if not enable_websocket_ping:
            conn.logger.debug(f"Tính năng heartbeat WebSocket chưa bật, bỏ qua message PING")
            return

        try:
            conn.logger.debug(f"Nhận được message PING, gửi phản hồi PONG")
            conn.last_activity_time = time.time() * 1000
            # Dựng message phản hồi PONG
            pong_message = {
                "type": "pong",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }

            # Gửi phản hồi PONG
            await conn.websocket.send(json.dumps(pong_message))

        except Exception as e:
            conn.logger.error(f"Xảy ra lỗi khi xử lý message PING: {e}")
