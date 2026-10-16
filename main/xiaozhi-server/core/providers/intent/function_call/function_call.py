from ..base import IntentProviderBase
from typing import List, Dict
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class IntentProvider(IntentProviderBase):
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """
        Triển khai nhận dạng ý định mặc định, luôn trả về tiếp tục trò chuyện
        Args:
            dialogue_history: Danh sách lịch sử hội thoại
            text: Bản ghi hội thoại lần này
        Returns:
            Luôn trả về "tiếp tục trò chuyện"
        """
        logger.bind(tag=TAG).debug(
            "Using functionCallProvider, always returning continue chat"
        )
        return '{"function_call": {"name": "continue_chat"}}'
