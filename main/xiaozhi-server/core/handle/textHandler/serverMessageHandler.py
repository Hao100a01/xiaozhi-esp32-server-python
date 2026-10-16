import asyncio
import json
from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.providers.tools.device_mcp import handle_mcp_message

TAG = __name__

class ServerTextMessageHandler(TextMessageHandler):
    """Bộ xử lý message MCP"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.SERVER

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        # Nếu cấu hình đọc từ API thì cần xác thực secret
        if not conn.read_config_from_api:
            return
        # Lấy secret của request POST
        post_secret = msg_json.get("content", {}).get("secret", "")
        secret = conn.config["manager-api"].get("secret", "")
        # Nếu secret không khớp thì trả về
        if post_secret != secret:
            await conn.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "error",
                        "message": "Xác thực secret máy chủ thất bại",
                    }
                )
            )
            return
        # Cập nhật cấu hình động
        if msg_json["action"] == "update_config":
            try:
                # Cập nhật cấu hình của WebSocketServer
                if not conn.server:
                    await conn.websocket.send(
                        json.dumps(
                            {
                                "type": "server",
                                "status": "error",
                                "message": "Không lấy được instance máy chủ",
                                "content": {"action": "update_config"},
                            }
                        )
                    )
                    return

                if not await conn.server.update_config():
                    await conn.websocket.send(
                        json.dumps(
                            {
                                "type": "server",
                                "status": "error",
                                "message": "Cập nhật cấu hình máy chủ thất bại",
                                "content": {"action": "update_config"},
                            }
                        )
                    )
                    return

                # Gửi phản hồi thành công
                await conn.websocket.send(
                    json.dumps(
                        {
                            "type": "server",
                            "status": "success",
                            "message": "Cập nhật cấu hình thành công",
                            "content": {"action": "update_config"},
                        }
                    )
                )
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"Cập nhật cấu hình thất bại: {str(e)}")
                await conn.websocket.send(
                    json.dumps(
                        {
                            "type": "server",
                            "status": "error",
                            "message": f"Cập nhật cấu hình thất bại: {str(e)}",
                            "content": {"action": "update_config"},
                        }
                    )
                )
        # Khởi động lại máy chủ
        elif msg_json["action"] == "restart":
            await conn.handle_restart(msg_json)