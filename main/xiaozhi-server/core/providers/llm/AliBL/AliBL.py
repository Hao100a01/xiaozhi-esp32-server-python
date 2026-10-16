from config.logger import setup_logging
from http import HTTPStatus
import dashscope
from dashscope import Application
from core.providers.llm.base import LLMProviderBase
from core.utils.util import check_model_key
import time

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.api_key = config["api_key"]
        self.app_id = config["app_id"]
        self.base_url = config.get("base_url")
        self.is_No_prompt = config.get("is_no_prompt")
        self.memory_id = config.get("ali_memory_id")
        self.streaming_chunk_size = config.get("streaming_chunk_size", 3)  # Số ký tự trả về mỗi lần streaming
        check_model_key("AliBLLLM", self.api_key)

    def response(self, session_id, dialogue):
        # Xử lý dialogue
        if self.is_No_prompt:
            dialogue.pop(0)
            logger.bind(tag=TAG).debug(
                f"【Dịch vụ API Alibaba Bailian】dialogue sau khi xử lý: {dialogue}"
            )

        # Tạo tham số lệnh gọi
        call_params = {
            "api_key": self.api_key,
            "app_id": self.app_id,
            "session_id": session_id,
            "messages": dialogue,
            # Bật streaming gốc của SDK
            "stream": True,
        }
        if self.memory_id != False:
            # Memory của Bailian cần tham số prompt
            prompt = dialogue[-1].get("content")
            call_params["memory_id"] = self.memory_id
            call_params["prompt"] = prompt
            logger.bind(tag=TAG).debug(
                f"【Dịch vụ API Alibaba Bailian】prompt sau khi xử lý: {prompt}"
            )

        # Tuỳ chọn đặt địa chỉ API cơ sở tuỳ chỉnh (bỏ qua nếu cấu hình URL chế độ tương thích)
        if self.base_url and ("/api/" in self.base_url):
            dashscope.base_http_api_url = self.base_url

        responses = Application.call(**call_params)

        # Xử lý streaming (SDK trả về đối tượng có thể lặp khi stream=True; ngược lại trả về đối tượng phản hồi một lần)
        logger.bind(tag=TAG).debug(
            f"【Dịch vụ API Alibaba Bailian】Tham số đã tạo: {dict(call_params, api_key='***')}"
        )

        last_text = ""
        try:
            for resp in responses:
                if resp.status_code != HTTPStatus.OK:
                    logger.bind(tag=TAG).error(
                        f"code={resp.status_code}, message={resp.message}, vui lòng tham khảo tài liệu: https://help.aliyun.com/zh/model-studio/developer-reference/error-code"
                    )
                    continue
                current_text = getattr(getattr(resp, "output", None), "text", None)
                if current_text is None:
                    continue
                # Streaming của SDK là ghi đè tăng dần, tính đầu ra chênh lệch
                if len(current_text) >= len(last_text):
                    delta = current_text[len(last_text):]
                else:
                    # Tránh thụt lùi ngẫu nhiên
                    delta = current_text
                if delta:
                    yield delta
                last_text = current_text
        except TypeError:
            # Rơi về không streaming (trả về một lần)
            if responses.status_code != HTTPStatus.OK:
                logger.bind(tag=TAG).error(
                    f"code={responses.status_code}, message={responses.message}, vui lòng tham khảo tài liệu: https://help.aliyun.com/zh/model-studio/developer-reference/error-code"
                )
                yield "【Dịch vụ API Alibaba Bailian phản hồi bất thường】"
            else:
                full_text = getattr(getattr(responses, "output", None), "text", "")
                logger.bind(tag=TAG).info(
                    f"【Dịch vụ API Alibaba Bailian】Độ dài phản hồi đầy đủ: {len(full_text)}"
                )
                for i in range(0, len(full_text), self.streaming_chunk_size):
                    chunk = full_text[i:i + self.streaming_chunk_size]
                    if chunk:
                        yield chunk

    def response_with_functions(self, session_id, dialogue, functions=None):
        # Alibaba Bailian hiện chưa hỗ trợ function call gốc. Để giữ tương thích, ở đây rơi về đầu ra streaming văn bản thuần.
        # Tầng trên sẽ tiêu thụ theo dạng (content, tool_calls), ở đây luôn trả về (token, None)
        logger.bind(tag=TAG).warning(
            "Alibaba Bailian chưa triển khai function call gốc, đã rơi về đầu ra streaming văn bản thuần"
        )
        for token in self.response(session_id, dialogue):
            yield token, None
