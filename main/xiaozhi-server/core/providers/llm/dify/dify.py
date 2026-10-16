import json
from config.logger import setup_logging
import requests
from core.providers.llm.base import LLMProviderBase
from core.providers.llm.system_prompt import get_system_prompt_for_function
from core.utils.util import check_model_key

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.api_key = config["api_key"]
        self.mode = config.get("mode", "chat-messages")
        self.base_url = config.get("base_url", "https://api.dify.ai/v1").rstrip("/")
        self.session_conversation_map = {}  # Lưu ánh xạ session_id và conversation_id
        model_key_msg = check_model_key("DifyLLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)

    def response(self, session_id, dialogue, **kwargs):
        # Lấy thông điệp người dùng cuối cùng
        last_msg = next(m for m in reversed(dialogue) if m["role"] == "user")
        conversation_id = self.session_conversation_map.get(session_id)

        # Khởi tạo yêu cầu streaming
        if self.mode == "chat-messages":
            request_json = {
                "query": last_msg["content"],
                "response_mode": "streaming",
                "user": session_id,
                "inputs": {},
                "conversation_id": conversation_id,
            }
        elif self.mode == "workflows/run":
            request_json = {
                "inputs": {"query": last_msg["content"]},
                "response_mode": "streaming",
                "user": session_id,
            }
        elif self.mode == "completion-messages":
            request_json = {
                "inputs": {"query": last_msg["content"]},
                "response_mode": "streaming",
                "user": session_id,
            }

        with requests.post(
            f"{self.base_url}/{self.mode}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=request_json,
            stream=True,
        ) as r:
            if self.mode == "chat-messages":
                for line in r.iter_lines():
                    if line.startswith(b"data: "):
                        event = json.loads(line[6:])
                        # Nếu không tìm thấy conversation_id, lấy conversation_id lần này
                        if not conversation_id:
                            conversation_id = event.get("conversation_id")
                            self.session_conversation_map[session_id] = (
                                conversation_id  # Cập nhật ánh xạ
                            )
                        # Lọc sự kiện message_replace, sự kiện này sẽ đẩy toàn lượng một lần
                        if event.get("event") != "message_replace" and event.get(
                            "answer"
                        ):
                            yield event["answer"]
            elif self.mode == "workflows/run":
                for line in r.iter_lines():
                    if line.startswith(b"data: "):
                        event = json.loads(line[6:])
                        if event.get("event") == "workflow_finished":
                            if event["data"]["status"] == "succeeded":
                                yield event["data"]["outputs"]["answer"]
                            else:
                                yield "【Dịch vụ phản hồi bất thường】"
            elif self.mode == "completion-messages":
                for line in r.iter_lines():
                    if line.startswith(b"data: "):
                        event = json.loads(line[6:])
                        # Lọc sự kiện message_replace, sự kiện này sẽ đẩy toàn lượng một lần
                        if event.get("event") != "message_replace" and event.get(
                            "answer"
                        ):
                            yield event["answer"]

    def response_with_functions(self, session_id, dialogue, functions=None):
        if len(dialogue) == 2 and functions is not None and len(functions) > 0:
            # Lần đầu gọi llm, lấy thông điệp người dùng cuối cùng, đính kèm prompt tool
            last_msg = dialogue[-1]["content"]
            function_str = json.dumps(functions, ensure_ascii=False)
            modify_msg = get_system_prompt_for_function(function_str) + last_msg
            dialogue[-1]["content"] = modify_msg

        # Nếu cái cuối cùng là role="tool", đính kèm vào user
        if len(dialogue) > 1 and dialogue[-1]["role"] == "tool":
            assistant_msg = "\ntool call result: " + dialogue[-1]["content"] + "\n\n"
            while len(dialogue) > 1:
                if dialogue[-1]["role"] == "user":
                    dialogue[-1]["content"] = assistant_msg + dialogue[-1]["content"]
                    break
                dialogue.pop()

        for token in self.response(session_id, dialogue):
            yield token, None
