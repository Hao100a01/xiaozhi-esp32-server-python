from config.logger import setup_logging
from openai import OpenAI
import json
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.base_url = config.get("base_url", "http://localhost:11434")
        # Initialize OpenAI client with Ollama base URL
        # Nếu chưa có v1, thêm v1
        if not self.base_url.endswith("/v1"):
            self.base_url = f"{self.base_url}/v1"

        self.client = OpenAI(
            base_url=self.base_url,
            api_key="ollama",  # Ollama doesn't need an API key but OpenAI client requires one
        )

        # Kiểm tra có phải mô hình qwen3 không
        self.is_qwen3 = self.model_name and self.model_name.lower().startswith("qwen3")

    def response(self, session_id, dialogue, **kwargs):
        # Nếu là mô hình qwen3, thêm lệnh /no_think vào thông điệp cuối cùng của người dùng
        if self.is_qwen3:
            # Sao chép danh sách hội thoại, tránh sửa hội thoại gốc
            dialogue_copy = dialogue.copy()

            # Tìm thông điệp người dùng cuối cùng
            for i in range(len(dialogue_copy) - 1, -1, -1):
                if dialogue_copy[i]["role"] == "user":
                    # Thêm lệnh /no_think vào trước thông điệp người dùng
                    dialogue_copy[i]["content"] = (
                        "/no_think " + dialogue_copy[i]["content"]
                    )
                    logger.bind(tag=TAG).debug(f"Thêm lệnh /no_think cho mô hình qwen3")
                    break

            # Dùng hội thoại đã sửa
            dialogue = dialogue_copy

        responses = self.client.chat.completions.create(
            model=self.model_name, messages=dialogue, stream=True
        )
        is_active = True
        # Dùng để xử lý thẻ xuyên chunk
        buffer = ""

        try:
            for chunk in responses:
                try:
                    delta = (
                        chunk.choices[0].delta
                        if getattr(chunk, "choices", None)
                        else None
                    )
                    content = delta.content if hasattr(delta, "content") else ""

                    if content:
                        # Thêm nội dung vào bộ đệm
                        buffer += content

                        # Xử lý thẻ trong bộ đệm
                        while "<think>" in buffer and "</think>" in buffer:
                            # Tìm thẻ <think></think> đầy đủ và xoá
                            pre = buffer.split("<think>", 1)[0]
                            post = buffer.split("</think>", 1)[1]
                            buffer = pre + post

                        # Xử lý trường hợp chỉ có thẻ mở
                        if "<think>" in buffer:
                            is_active = False
                            buffer = buffer.split("<think>", 1)[0]

                        # Xử lý trường hợp chỉ có thẻ đóng
                        if "</think>" in buffer:
                            is_active = True
                            buffer = buffer.split("</think>", 1)[1]

                        # Nếu hiện đang hoạt động và bộ đệm có nội dung, thì xuất ra
                        if is_active and buffer:
                            yield buffer
                            buffer = ""  # Xoá bộ đệm

                except Exception as e:
                    logger.bind(tag=TAG).error(f"Error processing chunk: {e}")
        finally:
            responses.close()

    def response_with_functions(self, session_id, dialogue, functions=None):
        # Nếu là mô hình qwen3, thêm lệnh /no_think vào thông điệp cuối cùng của người dùng
        if self.is_qwen3:
            # Sao chép danh sách hội thoại, tránh sửa hội thoại gốc
            dialogue_copy = dialogue.copy()

            # Tìm thông điệp người dùng cuối cùng
            for i in range(len(dialogue_copy) - 1, -1, -1):
                if dialogue_copy[i]["role"] == "user":
                    # Thêm lệnh /no_think vào trước thông điệp người dùng
                    dialogue_copy[i]["content"] = (
                        "/no_think " + dialogue_copy[i]["content"]
                    )
                    logger.bind(tag=TAG).debug(f"Thêm lệnh /no_think cho mô hình qwen3")
                    break

            # Dùng hội thoại đã sửa
            dialogue = dialogue_copy

        stream = self.client.chat.completions.create(
            model=self.model_name,
            messages=dialogue,
            stream=True,
            tools=functions,
        )

        is_active = True
        buffer = ""

        try:
            for chunk in stream:
                try:
                    delta = (
                        chunk.choices[0].delta
                        if getattr(chunk, "choices", None)
                        else None
                    )
                    content = delta.content if hasattr(delta, "content") else None
                    tool_calls = (
                        delta.tool_calls if hasattr(delta, "tool_calls") else None
                    )

                    # Nếu là lệnh gọi công cụ, truyền trực tiếp
                    if tool_calls:
                        yield None, tool_calls
                        continue

                    # Xử lý nội dung văn bản
                    if content:
                        # Thêm nội dung vào bộ đệm
                        buffer += content

                        # Xử lý thẻ trong bộ đệm
                        while "<think>" in buffer and "</think>" in buffer:
                            # Tìm thẻ <think></think> đầy đủ và xoá
                            pre = buffer.split("<think>", 1)[0]
                            post = buffer.split("</think>", 1)[1]
                            buffer = pre + post

                        # Xử lý trường hợp chỉ có thẻ mở
                        if "<think>" in buffer:
                            is_active = False
                            buffer = buffer.split("<think>", 1)[0]

                        # Xử lý trường hợp chỉ có thẻ đóng
                        if "</think>" in buffer:
                            is_active = True
                            buffer = buffer.split("</think>", 1)[1]

                        # Nếu hiện đang hoạt động và bộ đệm có nội dung, thì xuất ra
                        if is_active and buffer:
                            yield buffer, None
                            buffer = ""  # Xoá bộ đệm
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Error processing function chunk: {e}")
                    continue
        finally:
            stream.close()
