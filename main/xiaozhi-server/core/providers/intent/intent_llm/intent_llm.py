import asyncio
from typing import List, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import IntentProviderBase
from plugins_func.functions.play_music import initialize_music_handler
from config.logger import setup_logging
from core.utils.util import get_system_error_response
import re
import json
import hashlib
import time



TAG = __name__
logger = setup_logging()


class IntentProvider(IntentProviderBase):
    def __init__(self, config):
        super().__init__(config)
        self.llm = None
        self.promot = ""
        # Nhập trình quản lý bộ đệm toàn cục
        from core.utils.cache.manager import cache_manager, CacheType

        self.cache_manager = cache_manager
        self.CacheType = CacheType
        self.history_count = 4  # Mặc định dùng 4 bản ghi hội thoại gần nhất

    def get_intent_system_prompt(self, functions_list: str) -> str:
        """
        Tạo system prompt động dựa trên tuỳ chọn ý định đã cấu hình và các hàm khả dụng
        Args:
            functions: Danh sách hàm khả dụng, chuỗi định dạng JSON
        Returns:
            System prompt sau khi định dạng
        """

        # Tạo phần mô tả hàm
        functions_desc = "Danh sách hàm khả dụng:\n"
        for func in functions_list:
            func_info = func.get("function", {})
            name = func_info.get("name", "")
            desc = func_info.get("description", "")
            params = func_info.get("parameters", {})

            functions_desc += f"\nTên hàm: {name}\n"
            functions_desc += f"Mô tả: {desc}\n"

            if params:
                functions_desc += "Tham số:\n"
                for param_name, param_info in params.get("properties", {}).items():
                    param_desc = param_info.get("description", "")
                    param_type = param_info.get("type", "")
                    functions_desc += f"- {param_name} ({param_type}): {param_desc}\n"

            functions_desc += "---\n"

        prompt = (
            "【Yêu cầu định dạng nghiêm ngặt】Bạn chỉ được trả về định dạng JSON, tuyệt đối không được trả về bất kỳ ngôn ngữ tự nhiên nào!\n\n"
            "Bạn là trợ lý nhận dạng ý định. Hãy phân tích câu cuối cùng của người dùng, xác định ý định và gọi hàm tương ứng.\n\n"
            "【Quy tắc quan trọng】Với các loại truy vấn sau, hãy trả về trực tiếp result_for_context, không cần gọi hàm:\n"
            "- Hỏi giờ hiện tại (ví dụ: bây giờ mấy giờ, giờ hiện tại, tra cứu giờ, v.v.)\n"
            "- Hỏi ngày hôm nay (ví dụ: hôm nay ngày mấy, hôm nay thứ mấy, hôm nay là ngày gì, v.v.)\n"
            "- Hỏi ngày âm lịch hôm nay (ví dụ: hôm nay âm lịch ngày mấy, hôm nay tiết khí gì, v.v.)\n"
            "- Hỏi thành phố đang ở (ví dụ: tôi đang ở đâu, bạn biết tôi ở thành phố nào không, v.v.)"
            "Hệ thống sẽ xây dựng câu trả lời trực tiếp dựa trên thông tin ngữ cảnh.\n\n"
            "- Nếu người dùng dùng từ nghi vấn (như 'làm sao', 'tại sao', 'như thế nào') để hỏi về việc thoát (ví dụ 'sao lại thoát rồi?'), lưu ý đây không phải yêu cầu bạn thoát, hãy trả về {'function_call': {'name': 'continue_chat'}\n"
            "- Chỉ khi người dùng dùng rõ ràng các lệnh như 'thoát hệ thống', 'kết thúc hội thoại', 'tôi không muốn nói chuyện với bạn nữa' mới kích hoạt handle_exit_intent\n\n"
            f"{functions_desc}\n"
            "Các bước xử lý:\n"
            "1. Phân tích đầu vào của người dùng, xác định ý định\n"
            "2. Kiểm tra có phải truy vấn thông tin cơ bản nêu trên (giờ, ngày, v.v.) không, nếu đúng thì trả về result_for_context\n"
            "3. Chọn hàm phù hợp nhất từ danh sách hàm khả dụng\n"
            "4. Nếu tìm được hàm phù hợp, tạo định dạng function_call tương ứng\n"
            '5. Nếu không tìm được hàm phù hợp, trả về {"function_call": {"name": "continue_chat"}}\n\n'
            "Yêu cầu định dạng trả về:\n"
            "1. Phải trả về định dạng JSON thuần, không chứa bất kỳ chữ nào khác\n"
            "2. Phải chứa trường function_call\n"
            "3. function_call phải chứa trường name\n"
            "4. Nếu hàm cần tham số, phải chứa trường arguments\n\n"
            "Ví dụ:\n"
            "```\n"
            "Người dùng: Bây giờ mấy giờ?\n"
            'Trả về: {"function_call": {"name": "result_for_context"}}\n'
            "```\n"
            "```\n"
            "Người dùng: Pin hiện tại còn bao nhiêu?\n"
            'Trả về: {"function_call": {"name": "get_battery_level", "arguments": {"response_success": "Pin hiện tại là {value}%", "response_failure": "Không lấy được phần trăm pin hiện tại của Battery"}}}\n'
            "```\n"
            "```\n"
            "Người dùng: Độ sáng màn hình hiện tại là bao nhiêu?\n"
            'Trả về: {"function_call": {"name": "self_screen_get_brightness"}}\n'
            "```\n"
            "```\n"
            "Người dùng: Đặt độ sáng màn hình thành 50%\n"
            'Trả về: {"function_call": {"name": "self_screen_set_brightness", "arguments": {"brightness": 50}}}\n'
            "```\n"
            "```\n"
            "Người dùng: Tôi muốn kết thúc hội thoại\n"
            'Trả về: {"function_call": {"name": "handle_exit_intent", "arguments": {"say_goodbye": "goodbye"}}}\n'
            "```\n"
            "```\n"
            "Người dùng: Xin chào\n"
            'Trả về: {"function_call": {"name": "continue_chat"}}\n'
            "```\n\n"
            "Lưu ý:\n"
            "1. Chỉ trả về định dạng JSON, không chứa bất kỳ chữ nào khác\n"
            '2. Ưu tiên kiểm tra truy vấn của người dùng có phải thông tin cơ bản (giờ, ngày, v.v.) không, nếu đúng thì trả về {"function_call": {"name": "result_for_context"}}, không cần tham số arguments\n'
            '3. Nếu không tìm được hàm phù hợp, trả về {"function_call": {"name": "continue_chat"}}\n'
            "4. Đảm bảo định dạng JSON trả về đúng, chứa mọi trường cần thiết\n"
            "5. result_for_context không cần bất kỳ tham số nào, hệ thống sẽ tự động lấy thông tin từ ngữ cảnh\n"
            "Giải thích đặc biệt:\n"
            "- Khi một lần nhập của người dùng chứa nhiều lệnh (như 'bật đèn và tăng âm lượng')\n"
            "- Hãy trả về mảng JSON gồm nhiều function_call\n"
            "- Ví dụ: {'function_calls': [{name:'light_on'}, {name:'volume_up'}]}\n\n"
            "【Cảnh báo cuối cùng】Tuyệt đối cấm xuất bất kỳ ngôn ngữ tự nhiên, biểu tượng cảm xúc hay chữ giải thích nào! Chỉ được xuất định dạng JSON hợp lệ! Vi phạm quy tắc này sẽ gây lỗi hệ thống!"
        )
        return prompt

    async def replyResult(self, text: str, original_text: str):
        """Dùng asyncio.to_thread để tránh chặn vòng lặp sự kiện"""
        try:
            user_prompt = (
                "Hãy dựa vào nội dung trên, trả lời người dùng với giọng điệu như con người, yêu cầu ngắn gọn, hãy trả về trực tiếp kết quả. Người dùng đang nói: "
                + original_text
            )
            # Dùng to_thread để đưa lệnh gọi đồng bộ gây chặn vào thread pool thực thi, không chặn vòng lặp sự kiện
            llm_result = await asyncio.to_thread(
                self.llm.response_no_stream,
                system_prompt=text,
                user_prompt=user_prompt,
            )
            return llm_result
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in generating reply result: {e}")
            return get_system_error_response(self.config)

    async def detect_intent(
        self, conn: "ConnectionHandler", dialogue_history: List[Dict], text: str
    ) -> str:
        if not self.llm:
            raise ValueError("LLM provider not set")
        if conn.func_handler is None:
            return '{"function_call": {"name": "continue_chat"}}'

        # Ghi lại thời gian bắt đầu tổng thể
        total_start_time = time.time()

        # In thông tin mô hình đang dùng
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"Dùng mô hình nhận dạng ý định: {model_info}")

        # Tính khoá bộ đệm
        cache_key = hashlib.md5((conn.device_id + text).encode()).hexdigest()

        # Kiểm tra bộ đệm
        cached_intent = self.cache_manager.get(self.CacheType.INTENT, cache_key)
        if cached_intent is not None:
            cache_time = time.time() - total_start_time
            logger.bind(tag=TAG).debug(
                f"Dùng ý định từ bộ đệm: {cache_key} -> {cached_intent}, mất: {cache_time:.4f} giây"
            )
            return cached_intent

        if self.promot == "":
            functions = conn.func_handler.get_functions()
            if hasattr(conn, "mcp_client"):
                mcp_tools = conn.mcp_client.get_available_tools()
                if mcp_tools is not None and len(mcp_tools) > 0:
                    if functions is None:
                        functions = []
                    functions.extend(mcp_tools)

            self.promot = self.get_intent_system_prompt(functions)

        music_config = initialize_music_handler(conn)
        music_file_names = music_config["music_file_names"]
        prompt_music = f"{self.promot}\n<musicNames>{music_file_names}\n</musicNames>"

        home_assistant_cfg = conn.config["plugins"].get("home_assistant")
        if home_assistant_cfg:
            devices = home_assistant_cfg.get("devices", [])
        else:
            devices = []
        if len(devices) > 0:
            hass_prompt = "\nDưới đây là danh sách thiết bị thông minh trong nhà tôi (vị trí, tên thiết bị, entity_id), có thể điều khiển qua homeassistant\n"
            for device in devices:
                hass_prompt += device + "\n"
            prompt_music += hass_prompt

        logger.bind(tag=TAG).debug(f"User prompt: {prompt_music}")

        # Tạo prompt lịch sử hội thoại của người dùng
        msgStr = ""

        # Lấy lịch sử hội thoại gần đây
        start_idx = max(0, len(dialogue_history) - self.history_count)
        for i in range(start_idx, len(dialogue_history)):
            msgStr += f"{dialogue_history[i].role}: {dialogue_history[i].content}\n"

        msgStr += f"User: {text}\n"
        user_prompt = f"current dialogue:\n{msgStr}"

        # Ghi lại thời gian hoàn thành tiền xử lý
        preprocess_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(f"Tiền xử lý nhận dạng ý định mất: {preprocess_time:.4f} giây")

        # Dùng LLM để nhận dạng ý định
        llm_start_time = time.time()
        logger.bind(tag=TAG).debug(f"Bắt đầu gọi LLM nhận dạng ý định, mô hình: {model_info}")

        try:
            # Dùng to_thread để đưa lệnh gọi đồng bộ gây chặn vào thread pool, tránh chặn vòng lặp sự kiện
            intent = await asyncio.to_thread(
                self.llm.response_no_stream,
                system_prompt=prompt_music,
                user_prompt=user_prompt,
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in intent detection LLM call: {e}")
            return '{"function_call": {"name": "continue_chat"}}'

        # Ghi lại thời gian hoàn thành lệnh gọi LLM
        llm_time = time.time() - llm_start_time
        logger.bind(tag=TAG).debug(
            f"Nhận dạng ý định bằng mô hình lớn ngoài hoàn tất, mô hình: {model_info}, thời gian gọi: {llm_time:.4f} giây"
        )

        # Ghi lại thời gian bắt đầu hậu xử lý
        postprocess_start_time = time.time()

        # Dọn dẹp và phân tích phản hồi
        intent = intent.strip()
        # Thử trích xuất phần JSON
        match = re.search(r"\{.*\}", intent, re.DOTALL)
        if match:
            intent = match.group(0)

        # Ghi lại tổng thời gian xử lý
        total_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(
            f"【Hiệu năng nhận dạng ý định】Mô hình: {model_info}, tổng thời gian: {total_time:.4f} giây, lệnh gọi LLM: {llm_time:.4f} giây, truy vấn: '{text[:20]}...'"
        )

        # Thử phân tích thành JSON
        try:
            intent_data = json.loads(intent)
            # Nếu chứa function_call, định dạng thành dạng phù hợp để xử lý
            if "function_call" in intent_data:
                function_data = intent_data["function_call"]
                function_name = function_data.get("name")
                function_args = function_data.get("arguments", {})

                # Ghi lại function call đã nhận dạng
                logger.bind(tag=TAG).info(
                    f"llm đã nhận dạng ý định: {function_name}, tham số: {function_args}"
                )

                # Xử lý các kiểu ý định khác nhau
                if function_name == "result_for_context":
                    # Xử lý truy vấn thông tin cơ bản, xây dựng kết quả trực tiếp từ context
                    logger.bind(tag=TAG).info(
                        "Đã phát hiện ý định result_for_context, sẽ dùng thông tin ngữ cảnh để trả lời trực tiếp"
                    )

                elif function_name == "continue_chat":
                    # Xử lý hội thoại thông thường
                    # Giữ lại các thông điệp không liên quan đến công cụ
                    clean_history = [
                        msg
                        for msg in conn.dialogue.dialogue
                        if msg.role not in ["tool", "function"]
                    ]
                    conn.dialogue.dialogue = clean_history

                else:
                    # Xử lý lệnh gọi hàm
                    logger.bind(tag=TAG).info(f"Đã phát hiện ý định gọi hàm: {function_name}")

            # Xử lý và trả về bộ đệm thống nhất
            self.cache_manager.set(self.CacheType.INTENT, cache_key, intent)
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).debug(f"Hậu xử lý ý định mất: {postprocess_time:.4f} giây")
            return intent
        except json.JSONDecodeError:
            # Thời gian hậu xử lý
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).error(
                f"Không thể phân tích JSON ý định: {intent}, hậu xử lý mất: {postprocess_time:.4f} giây"
            )
            # Nếu phân tích thất bại, mặc định trả về ý định tiếp tục trò chuyện
            return '{"function_call": {"name": "continue_chat"}}'
