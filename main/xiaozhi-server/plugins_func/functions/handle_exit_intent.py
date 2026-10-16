from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

handle_exit_intent_function_desc = {
    "type": "function",
    "function": {
        "name": "handle_exit_intent",
        "description": "Khi người dùng muốn kết thúc cuộc trò chuyện hoặc cần thoát hệ thống thì gọi",
        "parameters": {
            "type": "object",
            "properties": {
                "say_goodbye": {
                    "type": "string",
                    "description": "Lời chào tạm biệt thân thiện để kết thúc cuộc trò chuyện với người dùng",
                }
            },
            "required": ["say_goodbye"],
        },
    },
}


@register_function(
    "handle_exit_intent", handle_exit_intent_function_desc, ToolType.SYSTEM_CTL
)
def handle_exit_intent(conn: "ConnectionHandler", say_goodbye: str | None = None):
    # Xử lý ý định thoát
    try:
        if say_goodbye is None:
            say_goodbye = "Tạm biệt, chúc bạn một ngày vui vẻ!"
        if not conn.close_after_chat:
            conn.close_after_chat = True
        logger.bind(tag=TAG).info(f"Đã xử lý ý định thoát: {say_goodbye}")
        return ActionResponse(
            action=Action.RESPONSE, result="Đã xử lý ý định thoát", response=say_goodbye
        )
    except Exception as e:
        logger.bind(tag=TAG).error(f"Lỗi xử lý ý định thoát: {e}")
        return ActionResponse(
            action=Action.NONE, result="Xử lý ý định thoát thất bại", response=""
        )
