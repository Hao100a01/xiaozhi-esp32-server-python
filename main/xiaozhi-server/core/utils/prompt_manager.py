"""
Module quản lý system prompt
Chịu trách nhiệm quản lý và cập nhật system prompt, gồm khởi tạo nhanh và chức năng tăng cường bất đồng bộ
"""

import os
import asyncio
import threading
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from config.logger import setup_logging
from jinja2 import Template

TAG = __name__

WEEKDAY_MAP = {
    "Monday": "Thứ Hai",
    "Tuesday": "Thứ Ba",
    "Wednesday": "Thứ Tư",
    "Thursday": "Thứ Năm",
    "Friday": "Thứ Sáu",
    "Saturday": "Thứ Bảy",
    "Sunday": "Chủ Nhật",
}

EMOJI_List = [
    "😶",
    "🙂",
    "😆",
    "😂",
    "😔",
    "😠",
    "😭",
    "😍",
    "😳",
    "😲",
    "😱",
    "🤔",
    "😉",
    "😎",
    "😌",
    "🤤",
    "😘",
    "😏",
    "😴",
    "😜",
    "🙄",
]


class PromptManager:
    """Bộ quản lý system prompt, chịu trách nhiệm quản lý và cập nhật system prompt"""

    def __init__(self, config: Dict[str, Any], logger=None):
        self.config = config
        self.logger = logger or setup_logging()
        self.base_prompt_template = None
        self.last_update_time = 0

        # Import bộ quản lý cache toàn cục
        from core.utils.cache.manager import cache_manager, CacheType

        self.cache_manager = cache_manager
        self.CacheType = CacheType

        # Khởi tạo nguồn ngữ cảnh
        from core.utils.context_provider import ContextDataProvider

        self.context_provider = ContextDataProvider(config, self.logger)
        self.context_data = {}

        self._load_base_template()

    def _load_base_template(self):
        """Tải template prompt cơ sở"""
        try:
            template_path = self.config.get("prompt_template", None)
            if not template_path:
                template_path = "agent-base-prompt.txt"
            cache_key = f"prompt_template:{template_path}"

            # Lấy từ cache trước
            cached_template = self.cache_manager.get(self.CacheType.CONFIG, cache_key)
            if cached_template is not None:
                self.base_prompt_template = cached_template
                self.logger.bind(tag=TAG).debug("Tải template prompt cơ sở từ cache")
                return

            # Cache miss, đọc từ file
            if os.path.exists(template_path):
                with open(template_path, "r", encoding="utf-8") as f:
                    template_content = f.read()

                # Lưu vào cache (loại CONFIG mặc định không tự hết hạn, cần làm hết hiệu lực thủ công)
                self.cache_manager.set(
                    self.CacheType.CONFIG, cache_key, template_content
                )
                self.base_prompt_template = template_content
                self.logger.bind(tag=TAG).debug("Tải template prompt cơ sở thành công và đã cache")
            else:
                self.logger.bind(tag=TAG).warning(f"Không tìm thấy file {template_path}")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Tải template prompt thất bại: {e}")

    def get_quick_prompt(self, user_prompt: str, device_id: str = None) -> str:
        """Lấy nhanh system prompt (dùng cấu hình người dùng)"""
        device_cache_key = f"device_prompt:{device_id}"
        cached_device_prompt = self.cache_manager.get(
            self.CacheType.DEVICE_PROMPT, device_cache_key
        )
        if cached_device_prompt is not None:
            self.logger.bind(tag=TAG).debug(f"Dùng prompt đã cache của thiết bị {device_id}")
            return cached_device_prompt
        else:
            self.logger.bind(tag=TAG).debug(
                f"Thiết bị {device_id} không có prompt cache, dùng prompt được truyền vào"
            )

        # Dùng prompt được truyền vào và cache (nếu có device ID)
        if device_id:
            device_cache_key = f"device_prompt:{device_id}"
            self.cache_manager.set(self.CacheType.DEVICE_PROMPT, device_cache_key, user_prompt)
            self.logger.bind(tag=TAG).debug(f"Prompt của thiết bị {device_id} đã được cache")

        self.logger.bind(tag=TAG).info(f"Dùng prompt nhanh: {user_prompt[:50]}...")
        return user_prompt

    def _get_current_time_info(self) -> tuple:
        """Lấy thông tin thời gian hiện tại"""
        from .current_time import (
            get_current_date,
            get_current_weekday,
            get_current_lunar_date,
        )

        today_date = get_current_date()
        today_weekday = get_current_weekday()
        lunar_date = get_current_lunar_date() + "\n"

        return today_date, today_weekday, lunar_date

    def _get_location_info(self, client_ip: str) -> str:
        """Lấy thông tin vị trí"""
        try:
            # Lấy từ cache trước
            cached_location = self.cache_manager.get(self.CacheType.LOCATION, client_ip)
            if cached_location is not None:
                return cached_location

            # Cache miss, gọi API lấy
            from core.utils.util import get_ip_info

            ip_info = get_ip_info(client_ip, self.logger)
            city = ip_info.get("city", "Không rõ vị trí")
            location = f"{city}"

            # Lưu vào cache
            self.cache_manager.set(self.CacheType.LOCATION, client_ip, location)
            return location
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Lấy thông tin vị trí thất bại: {e}")
            return "Không rõ vị trí"

    def _get_weather_info(self, conn: "ConnectionHandler", location: str) -> str:
        """Lấy thông tin thời tiết"""
        try:
            # Lấy từ cache trước
            cached_weather = self.cache_manager.get(self.CacheType.WEATHER, location)
            if cached_weather is not None:
                return cached_weather

            # Cache miss, gọi hàm async get_weather
            # Windows ProactorEventLoop không hỗ trợ run_coroutine_threadsafe().result()
            # Vì vậy dùng call_soon_threadsafe nộp task + threading.Event chờ kết quả
            # Lưu ý: Event.wait() chỉ chặn thread pool hiện tại, không chặn event loop chính
            from plugins_func.functions.get_weather import get_weather
            from plugins_func.register import ActionResponse

            result_holder = []
            exception_holder = []

            async def _call():
                try:
                    result_holder.append(
                        await get_weather(conn, location=location, lang="zh_CN")
                    )
                except Exception as e:
                    exception_holder.append(e)
                finally:
                    event.set()

            event = threading.Event()
            conn.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_call()))
            if not event.wait(timeout=10):
                raise TimeoutError("Lấy thông tin thời tiết quá thời gian")
            if exception_holder:
                raise exception_holder[0]
            result = result_holder[0]
            if isinstance(result, ActionResponse):
                weather_report = result.result
                self.cache_manager.set(self.CacheType.WEATHER, location, weather_report)
                return weather_report
            return "Không lấy được thông tin thời tiết"

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Không lấy được thông tin thời tiết: {e}")
            return "Không lấy được thông tin thời tiết"

    def update_context_info(self, conn, client_ip: str):
        """Cập nhật đồng bộ thông tin ngữ cảnh"""
        try:
            local_address = ""
            if (
                client_ip
                and self.base_prompt_template
                and (
                    "local_address" in self.base_prompt_template
                    or "weather_info" in self.base_prompt_template
                )
            ):
                # Lấy thông tin vị trí (dùng cache toàn cục)
                local_address = self._get_location_info(client_ip)

            if (
                self.base_prompt_template
                and "weather_info" in self.base_prompt_template
                and local_address
            ):
                # Lấy thông tin thời tiết (dùng cache toàn cục)
                self._get_weather_info(conn, local_address)

            # Lấy dữ liệu ngữ cảnh đã cấu hình
            if hasattr(conn, "device_id") and conn.device_id:
                if (
                    self.base_prompt_template
                    and "dynamic_context" in self.base_prompt_template
                ):
                    self.context_data = self.context_provider.fetch_all(conn.device_id)
                else:
                    self.context_data = ""

            self.logger.bind(tag=TAG).debug(f"Cập nhật thông tin ngữ cảnh hoàn tất")

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Cập nhật thông tin ngữ cảnh thất bại: {e}")

    def build_enhanced_prompt(
        self, user_prompt: str, device_id: str, client_ip: str = None, *args, **kwargs
    ) -> str:
        """Dựng system prompt tăng cường"""
        if not self.base_prompt_template:
            return user_prompt

        try:
            # Lấy thông tin thời gian mới nhất (không cache)
            today_date, today_weekday, lunar_date = self._get_current_time_info()

            # Lấy thông tin ngữ cảnh đã cache
            local_address = ""
            weather_info = ""

            if client_ip:
                # Lấy thông tin vị trí (từ cache toàn cục)
                local_address = (
                    self.cache_manager.get(self.CacheType.LOCATION, client_ip) or ""
                )

                # Lấy thông tin thời tiết (từ cache toàn cục)
                if local_address:
                    weather_info = (
                        self.cache_manager.get(self.CacheType.WEATHER, local_address)
                        or ""
                    )

            # Lấy ngôn ngữ TTS đã chọn, giá trị mặc định là tiếng Việt
            language = (
                self.config.get("TTS", {})
                .get(self.config.get("selected_module", {}).get("TTS", ""), {})
                .get("language")
                or "Tiếng Việt"
            )
            self.logger.bind(tag=TAG).debug(f"Lấy được ngôn ngữ đã chọn: {language}")

            # Thay biến template
            template = Template(self.base_prompt_template)
            enhanced_prompt = template.render(
                base_prompt=user_prompt,
                current_time="{{current_time}}",
                today_date=today_date,
                today_weekday=today_weekday,
                lunar_date=lunar_date,
                local_address=local_address,
                weather_info=weather_info,
                emojiList=EMOJI_List,
                device_id=device_id,
                client_ip=client_ip,
                dynamic_context=self.context_data,
                language=language,
                *args,
                **kwargs,
            )
            device_cache_key = f"device_prompt:{device_id}"
            self.cache_manager.set(
                self.CacheType.DEVICE_PROMPT, device_cache_key, enhanced_prompt
            )
            self.logger.bind(tag=TAG).info(
                f"Dựng prompt tăng cường thành công, độ dài: {len(enhanced_prompt)}"
            )
            return enhanced_prompt

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Dựng prompt tăng cường thất bại: {e}")
            return user_prompt
