import requests
from requests.exceptions import RequestException
from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.agent_id = config.get("agent_id")  # Tương ứng agent_id
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url", config.get("url"))  # Mặc định dùng base_url
        self.api_url = f"{self.base_url}/api/conversation/process"  # Nối URL API đầy đủ

    def response(self, session_id, dialogue, **kwargs):
        # Trợ lý giọng nói Home Assistant có sẵn ý định, không cần dùng ý định có sẵn của xiaozhi AI, chỉ cần chuyển lời người dùng nói cho Home Assistant

        # Trích xuất content của role 'user' cuối cùng
        input_text = None
        if isinstance(dialogue, list):  # Đảm bảo dialogue là một list
            # Duyệt ngược, tìm thông điệp có role 'user' cuối cùng
            for message in reversed(dialogue):
                if message.get("role") == "user":  # Tìm thông điệp có role 'user'
                    input_text = message.get("content", "")
                    break  # Tìm thấy thì thoát vòng lặp ngay

        # Tạo dữ liệu yêu cầu
        payload = {
            "text": input_text,
            "agent_id": self.agent_id,
            "conversation_id": session_id,  # Dùng session_id làm conversation_id
        }
        # Thiết lập header yêu cầu
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Khởi tạo yêu cầu POST
        with requests.post(self.api_url, json=payload, headers=headers) as response:
            # Kiểm tra yêu cầu có thành công không
            response.raise_for_status()

            # Phân tích dữ liệu trả về
            data = response.json()
        speech = (
            data.get("response", {})
            .get("speech", {})
            .get("plain", {})
            .get("speech", "")
        )

        # Trả về nội dung đã tạo
        if speech:
            yield speech
        else:
            logger.bind(tag=TAG).warning("Dữ liệu API trả về không có nội dung speech")

    def response_with_functions(self, session_id, dialogue, functions=None):
        logger.bind(tag=TAG).error(
            f"homeassistant không hỗ trợ (function call), nên dùng nhận dạng ý định khác"
        )
