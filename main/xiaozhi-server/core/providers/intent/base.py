from abc import ABC, abstractmethod
from typing import List, Dict
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class IntentProviderBase(ABC):
    def __init__(self, config):
        self.config = config

    def set_llm(self, llm):
        self.llm = llm
        # Lấy tên mô hình và thông tin kiểu
        model_name = getattr(llm, "model_name", str(llm.__class__.__name__))
        # Ghi log chi tiết hơn
        logger.bind(tag=TAG).info(f"Nhận dạng ý định thiết lập LLM: {model_name}")

    @abstractmethod
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """
        Phát hiện ý định trong câu cuối cùng của người dùng
        Args:
            dialogue_history: Danh sách lịch sử hội thoại, mỗi bản ghi chứa role và content
        Returns:
            Trả về ý định đã nhận dạng, định dạng:
            - "Tiếp tục trò chuyện"
            - "Kết thúc trò chuyện"
            - "Phát nhạc tên_bài_hát" hoặc "Phát nhạc ngẫu nhiên"
            - "Tra cứu thời tiết tên_địa_điểm" hoặc "Tra cứu thời tiết [vị trí hiện tại]"
        """
        pass
