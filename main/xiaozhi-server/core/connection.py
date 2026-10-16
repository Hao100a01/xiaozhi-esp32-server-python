import os
import sys
import copy
import json
import re
import uuid
import time
import queue
import asyncio
import threading
import traceback
import subprocess
import websockets
import opuslib_next
import numpy as np

from core.utils.util import (
    extract_json_from_string,
    check_vad_update,
    check_asr_update,
    filter_sensitive_info,
)
from typing import Dict, Any
from collections import deque
from core.utils.modules_initialize import (
    initialize_modules,
    initialize_tts,
    initialize_asr,
)
from core.handle.reportHandle import report, enqueue_tool_report
from core.providers.tts.default import DefaultTTS
from concurrent.futures import ThreadPoolExecutor
from core.utils.dialogue import Message, Dialogue
from core.providers.asr.dto.dto import InterfaceType
from core.handle.textHandle import handleTextMessage
from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from plugins_func.loadplugins import auto_import_modules
from plugins_func.register import Action, ActionResponse, all_function_registry, module_func_map
from core.auth import AuthenticationError
from config.config_loader import get_private_config_from_api
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType
from config.logger import setup_logging, build_module_string, create_connection_logger
from config.manage_api_client import DeviceNotFoundException, DeviceBindException, generate_and_save_chat_title
from core.utils.prompt_manager import PromptManager
from core.utils.voiceprint_provider import VoiceprintProvider
from core.utils.util import get_system_error_response
from core.utils import textUtils


TAG = __name__

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass

# Định nghĩa tool ảo direct_answer
# Không phải tool thật mà là cơ chế định tuyến: biến lựa chọn nhị phân "có gọi tool không" thành lựa chọn đa "gọi tool nào", tránh model nhỏ kích hoạt nhầm tool thật
DIRECT_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "direct_answer",
        "description": "Khi yêu cầu của người dùng không khớp với bất kỳ tool nào khác, dùng tuỳ chọn này để trả lời trực tiếp. Viết nội dung trả lời vào param response.",
        "parameters": {
            "type": "object",
            "properties": {
                "response": {
                    "type": "string",
                    "description": "Nội dung đầy đủ bạn trả lời người dùng",
                },
            },
            "required": ["response"],
        },
    },
}


class ConnectionHandler:
    def __init__(
            self,
            config: Dict[str, Any],
            _vad,
            _asr,
            _llm,
            _memory,
            _intent,
            server=None,
    ):
        self.common_config = config
        self.config = copy.deepcopy(config)
        self.session_id = str(uuid.uuid4())
        self.logger = setup_logging()
        self.server = server  # Lưu tham chiếu instance server

        self.need_bind = False  # Có cần liên kết thiết bị không
        self.bind_completed_event = asyncio.Event()
        self.bind_code = None  # Mã xác minh liên kết thiết bị
        self.last_bind_prompt_time = 0  # Timestamp lần phát nhắc liên kết trước (giây)
        self.bind_prompt_interval = 60  # Khoảng cách phát nhắc liên kết (giây)

        self.read_config_from_api = self.config.get("read_config_from_api", False)

        self.websocket: websockets.ServerConnection | None = None
        self.headers = None
        self.device_id = None
        self.client_ip = None
        self.prompt = None
        self.welcome_msg = None
        self.max_output_size = 0
        self.chat_history_conf = 0
        self.audio_format = "opus"
        self.sample_rate = 24000  # Tần số lấy mẫu mặc định, cập nhật động từ message hello của client

        # Liên quan trạng thái client
        self.client_abort = False
        self.client_is_speaking = False
        self.client_listen_mode = "auto"
        self.client_aec = False  # Có bật AEC phía server không

        # Liên quan task thread
        self.loop = None  # Lấy event loop đang chạy trong handle_connection
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=5)

        # Thêm thread pool báo cáo
        self.report_queue = queue.Queue()
        self.report_thread = None
        # Sau này có thể sửa chỗ này để điều chỉnh báo cáo asr và tts, hiện mặc định đều bật
        self.report_asr_enable = self.read_config_from_api
        self.report_tts_enable = self.read_config_from_api

        # Các thành phần phụ thuộc
        self.vad = None
        self.asr = None
        self.tts = None
        self._asr = _asr
        self._vad = _vad
        self.llm = _llm
        self.memory = _memory
        self.intent = _intent

        # Quản lý nhận diện voiceprint riêng cho mỗi kết nối
        self.voiceprint_provider = None

        # Biến liên quan VAD
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_window = deque(maxlen=5)
        self.first_activity_time = 0.0  # Ghi thời điểm hoạt động đầu tiên (mili giây)
        self.last_activity_time = 0.0  # Timestamp hoạt động thống nhất (mili giây)
        self.vad_last_voice_time = 0.0  # Ghi thời điểm người dùng nói lần cuối (mili giây)
        self.client_voice_stop = False
        self.last_is_voice = False

        # Biến liên quan ASR
        # Vì khi deploy thực tế có thể dùng ASR local công cộng, không được để lộ biến cho ASR công cộng
        # Nên các biến liên quan ASR phải định nghĩa ở đây, là biến private của connection
        self.asr_audio = []  # Lưu danh sách frame PCM, dùng chung cho VAD và ASR
        self.asr_audio_queue = queue.Queue()
        self.current_speaker = None  # Lưu người nói hiện tại
        self.introduced_speakers = set()  # Người nói đã "giới thiệu lần đầu", chỉ mang tên ở lượt đầu
        self.system_introduced_speakers = set()  # Người nói đã inject danh tính vào system, danh tính system chỉ xuất hiện ở lượt đầu

        # Biến liên quan LLM
        self.dialogue = Dialogue()

        # Biến liên quan TTS
        self.sentence_id = None
        # Xử lý khi response TTS không trả về text
        self.tts_MessageText = ""

        # Biến liên quan IOT
        self.iot_descriptors = {}
        self.func_handler = None

        self.cmd_exit = self.config["exit_commands"]

        # Có đóng kết nối sau khi chat xong không
        self.close_after_chat = False
        self.load_function_plugin = False
        self.intent_type = "nointent"

        self.timeout_seconds = (
                int(self.config.get("close_connection_no_voice_time", 120)) + 60
        )  # Cộng thêm 60 giây trên lần đóng thứ nhất, thực hiện lần đóng thứ hai
        self.timeout_task = None

        # {"mcp":true} nghĩa là bật chức năng MCP
        self.features = None

        # Đánh dấu kết nối có từ MQTT không
        self.conn_from_mqtt_gateway = False

        # Khởi tạo bộ quản lý prompt
        self.prompt_manager = PromptManager(self.config, self.logger)

        # Khởi tạo trạng thái cuộc gọi
        self.calling = False
        # Đánh dấu hiện tại có phải chế độ nghe cuộc gọi đến
        self.incoming_call = None

    async def handle_connection(self, ws: websockets.ServerConnection):
        try:
            # Lấy event loop đang chạy (phải trong ngữ cảnh bất đồng bộ)
            self.loop = asyncio.get_running_loop()

            # Lấy và xác thực headers
            self.headers = dict(ws.request.headers)
            real_ip = self.headers.get("x-real-ip") or self.headers.get(
                "x-forwarded-for"
            )
            if real_ip:
                self.client_ip = real_ip.split(",")[0].strip()
            else:
                self.client_ip = ws.remote_address[0]
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {self.headers}"
            )

            self.device_id = self.headers.get("device-id", None)

            # Xác thực đạt, tiếp tục xử lý
            self.websocket = ws

            # Kiểm tra có phải kết nối từ MQTT
            request_path = ws.request.path
            self.conn_from_mqtt_gateway = request_path.endswith("?from=mqtt_gateway")
            if self.conn_from_mqtt_gateway:
                self.logger.bind(tag=TAG).info("Kết nối từ: MQTT gateway")

            # Khởi tạo timestamp hoạt động
            self.first_activity_time = time.time() * 1000
            self.last_activity_time = time.time() * 1000

            # Khởi động task kiểm tra timeout
            self.timeout_task = asyncio.create_task(self._check_timeout())

            # Khởi động task dọn cache AEC
            self._aec_cache_cleanup_task = asyncio.create_task(self._check_aec_cache_expiry())

            self.welcome_msg = self.config["xiaozhi"]
            self.welcome_msg["session_id"] = self.session_id

            # Đọc tần số lấy mẫu từ cấu hình
            self.sample_rate = self.welcome_msg["audio_params"]["sample_rate"]
            self.logger.bind(tag=TAG).info(f"Cấu hình tần số lấy mẫu audio đầu ra là: {self.sample_rate}")

            # Khởi tạo cấu hình và thành phần ở nền (hoàn toàn không chặn vòng lặp chính)
            asyncio.create_task(self._background_initialize())

            try:
                async for message in self.websocket:
                    await self._route_message(message)
            except websockets.exceptions.ConnectionClosed:
                self.logger.bind(tag=TAG).info("Client đã ngắt kết nối")

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            try:
                await self._save_and_close(ws)
            except Exception as final_error:
                self.logger.bind(tag=TAG).error(f"Lỗi khi dọn dẹp cuối cùng: {final_error}")
                # Đảm bảo dù lưu memory thất bại vẫn đóng kết nối
                try:
                    await self.close(ws)
                except Exception as close_error:
                    self.logger.bind(tag=TAG).error(
                        f"Lỗi khi buộc đóng kết nối: {close_error}"
                    )

    async def _save_and_close(self, ws):
        """Lưu memory và đóng kết nối"""
        try:
            # Thread bảo vệ 1: sinh tiêu đề độc lập (không phụ thuộc model memory)
            # Chỉ kích hoạt sinh tiêu đề khi phía server bật báo cáo chat_history,
            # nếu không phía server không tra ngược được agent/chat_history tương ứng theo session_id,
            # sẽ ném lỗi "智能体未找到"
            if self.session_id and self.chat_history_conf != 0:
                def generate_title_task():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(
                            generate_and_save_chat_title(self.session_id)
                        )
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(f"Sinh tiêu đề thất bại: {e}")
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass

                threading.Thread(target=generate_title_task, daemon=True).start()

            # Thread bảo vệ 2: đi quy trình cũ lưu memory (chỉ memory, không gồm tiêu đề)
            if self.memory:
                # Dùng thread pool lưu memory bất đồng bộ
                def save_memory_task():
                    try:
                        # Tạo event loop mới (tránh xung đột với vòng lặp chính)
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(
                            self.memory.save_memory(
                                self.dialogue.dialogue, self.session_id
                            )
                        )
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(f"Lưu memory thất bại: {e}")
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass

                # Khởi động thread lưu memory, không chờ hoàn tất
                threading.Thread(target=save_memory_task, daemon=True).start()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Lưu memory thất bại: {e}")
        finally:
            # Đóng kết nối ngay, không chờ lưu memory xong
            try:
                await self.close(ws)
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"Đóng kết nối sau khi lưu memory thất bại: {close_error}"
                )

    async def _discard_message_with_bind_prompt(self):
        """Bỏ message và kiểm tra có cần phát nhắc liên kết"""
        current_time = time.time()
        # Kiểm tra có cần phát nhắc liên kết
        if current_time - self.last_bind_prompt_time >= self.bind_prompt_interval:
            self.last_bind_prompt_time = current_time
            # Tái dùng logic nhắc liên kết hiện có
            from core.handle.receiveAudioHandle import check_bind_device

            asyncio.create_task(check_bind_device(self))

    async def _route_message(self, message):
        """Định tuyến message"""
        # Kiểm tra đã lấy được trạng thái liên kết thật chưa
        if not self.bind_completed_event.is_set():
            # Chưa lấy được trạng thái thật, chờ đến khi lấy được hoặc timeout
            try:
                await asyncio.wait_for(self.bind_completed_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                # Timeout vẫn chưa lấy được trạng thái thật, bỏ message
                await self._discard_message_with_bind_prompt()
                return

        # Đã lấy được trạng thái thật, kiểm tra có cần liên kết
        if self.need_bind:
            # Cần liên kết, bỏ message
            await self._discard_message_with_bind_prompt()
            return

        # Không cần liên kết, tiếp tục xử lý message

        if isinstance(message, str):
            await handleTextMessage(self, message)
        elif isinstance(message, bytes):
            if self.vad is None or self.asr is None:
                return

            # Xử lý gói audio từ MQTT gateway
            if self.conn_from_mqtt_gateway and len(message) >= 16:
                handled = await self._process_mqtt_audio_message(message)
                if handled:
                    return

            # Giải mã PCM ngay ở lối vào, tránh VAD và ASR giải mã trùng lặp
            pcm_frame = self._decode_opus_packet(message)
            if pcm_frame:
                self.asr_audio_queue.put(pcm_frame)

    async def _process_mqtt_audio_message(self, message):
        """
        Xử lý message audio từ MQTT gateway, phân tích header 16 byte và trích xuất dữ liệu audio, xử lý AEC trước khi vào hàng đợi

        Args:
            message: Message audio gồm header

        Returns:
            bool: Có xử lý message thành công không
        """
        try:
            # Phân tích timestamp
            timestamp = int.from_bytes(message[8:12], "big")

            audio_data = message[16:]
            # Giải mã PCM ngay ở lối vào
            pcm_frame = self._decode_opus_packet(audio_data)
            if not pcm_frame:
                return True

            # Xử lý AEC: nếu timestamp>0 và đã bật AEC
            if timestamp > 0 and self.client_aec:
                pcm_frame = self._apply_aec(timestamp, pcm_frame)

            self.asr_audio_queue.put(pcm_frame)
            return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Phân tích gói audio WebSocket thất bại: {e}")

        # Xử lý thất bại, trả về False nghĩa là cần tiếp tục xử lý
        return False

    def _apply_aec(self, timestamp: int, pcm_frame: bytes) -> bytes:
        """Áp dụng xử lý AEC - thuật toán tổng hợp: ước lượng trễ tương quan chéo + lọc Wiener + trừ phổ"""
        try:
            if not pcm_frame or len(pcm_frame) == 0:
                return pcm_frame

            if not hasattr(self, "aec_audio_cache") or not self.aec_audio_cache:
                return pcm_frame

            mic_audio = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
            mic_rms = np.sqrt(np.mean(mic_audio ** 2))

            if mic_rms < 100:
                return pcm_frame

            sorted_timestamps = sorted(self.aec_audio_cache.keys())
            if len(sorted_timestamps) < 2:
                return pcm_frame

            # ========== Khớp frame tham chiếu (khớp phổ công suất log) ==========
            n = len(mic_audio)

            # Tìm timestamp gần nhất làm điểm bắt đầu
            closest_idx = min(range(len(sorted_timestamps)), key=lambda i: abs(sorted_timestamps[i] - timestamp))

            # Tính trước phổ công suất log của mic_audio (dùng chung trong vòng lặp, tránh FFT trùng lặp)
            mic_window = np.hanning(n)
            mic_fft = np.fft.rfft(mic_audio * mic_window)
            mic_psd = np.abs(mic_fft) ** 2
            mic_log_psd = 10 * np.log10(mic_psd + 1e-8)
            mic_P_xx = np.dot(mic_log_psd, mic_log_psd)

            # Dùng khớp phổ công suất log tìm frame tốt nhất: tìm 2 frame trước và sau
            best_corr = -1
            best_ref_idx = closest_idx
            best_ref_rms = 0.0

            for offset in range(-2, 3):  # T-2, T-1, T, T+1, T+2
                test_idx = closest_idx + offset
                if test_idx < 0 or test_idx >= len(sorted_timestamps):
                    continue
                test_ts = sorted_timestamps[test_idx]
                test_ref = np.frombuffer(self.aec_audio_cache[test_ts], dtype=np.int16).astype(np.float32)
                test_ref_rms = np.sqrt(np.mean(test_ref ** 2))
                if test_ref_rms < 50:
                    continue

                # Tương quan phổ công suất log
                test_window = np.hanning(len(test_ref))
                test_fft = np.fft.rfft(test_ref * test_window)
                test_psd = np.abs(test_fft) ** 2
                test_log_psd = 10 * np.log10(test_psd + 1e-8)
                P_xy = np.dot(mic_log_psd, test_log_psd)
                P_yy = np.dot(test_log_psd, test_log_psd)
                corr = abs(P_xy) / (np.sqrt(mic_P_xx) * np.sqrt(P_yy) + 1e-8)

                if corr > best_corr:
                    best_corr = corr
                    best_ref_idx = test_idx
                    best_ref_rms = test_ref_rms

            best_ts = sorted_timestamps[best_ref_idx]
            best_ref = np.frombuffer(self.aec_audio_cache[best_ts], dtype=np.int16).astype(np.float32)
            ref_rms = best_ref_rms

            if ref_rms < 50:
                return pcm_frame

            # Căn chỉnh tín hiệu tham chiếu (cắt trực tiếp cùng độ dài)
            aligned_ref = best_ref[:n]
            if len(aligned_ref) < n:
                aligned_ref = np.pad(aligned_ref, (0, n - len(aligned_ref)))

            # ========== Xử lý AEC miền tần (trừ phổ) ==========
            # Tín hiệu miền thời gian bị méo pha sau khi qua đường âm học, khiến tương quan miền thời gian thấp và P_xy âm dương bất định
            # Phổ biên độ miền tần không bị ảnh hưởng bởi pha, tương quan phổ công suất log ổn định ở 0.97+
            # Công thức: result_mag = max(|mic_fft| - |ref_fft| * scale * coef, 0)

            mic_mag = np.abs(mic_fft)
            mic_phase = np.angle(mic_fft)
            ref_fft = np.fft.rfft(aligned_ref * np.hanning(n))
            ref_mag = np.abs(ref_fft)

            # Tính tỷ lệ echo scale ở miền tần
            scale = np.sum(mic_mag * ref_mag) / (np.dot(ref_mag, ref_mag) + 1e-8)

            # Hệ số thích ứng: điều chỉnh động theo scale và coh
            # scale lớn (echo mạnh) -> coef lớn; coh cao (khớp chuẩn) -> coef lớn
            raw_coef = 1.0 + scale * 3 + (best_corr - 0.97) * 30
            coef = max(0.5, min(3.0, raw_coef))

            # Trừ phổ (trừ quá + chỉnh lưu nửa sóng)
            echo_mag = ref_mag * scale * coef
            result_mag = np.maximum(mic_mag - echo_mag * 1.5, mic_mag * 0.1)

            # Giữ pha để tái tạo tín hiệu
            result_fft = result_mag * np.exp(1j * mic_phase)
            output = np.fft.irfft(result_fft, n)

            # Khi độ tin cậy cao là echo thuần, nén thêm để đảm bảo VAD không phát hiện
            if best_corr >= 0.97 and ref_rms > 500:
                output = output * 0.3

            # Hậu xử lý: giới hạn biên độ
            output = np.clip(output, -32768, 32767)

            # Chuyển thành bytes
            result = output.astype(np.int16).tobytes()

            return result

        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"[AEC] Xử lý thất bại: {e}")
            return pcm_frame

    def _decode_opus_packet(self, opus_packet: bytes) -> bytes:
        """
        Giải mã gói Opus thành dữ liệu PCM

        Args:
            opus_packet: Dữ liệu audio mã hoá Opus

        Returns:
            bytes: Dữ liệu PCM sau giải mã, thất bại trả về None
        """
        try:
            if not opus_packet or len(opus_packet) == 0:
                return None

            self._init_connection_state(self)
            pcm_frame = self._connection_opus_decoder.decode(opus_packet, 960)
            return pcm_frame
        except Exception as e:
            self.logger.bind(tag=TAG).debug(f"Giải mã Opus thất bại: {e}")
            return None

    def _init_connection_state(self, conn):
        """Khởi tạo bộ giải mã Opus riêng cho kết nối"""
        if not hasattr(conn, "_connection_opus_decoder"):
            conn._connection_opus_decoder = opuslib_next.Decoder(16000, 1)

    async def handle_restart(self, message):
        """Xử lý yêu cầu khởi động lại server"""
        try:

            self.logger.bind(tag=TAG).info("Nhận được lệnh khởi động lại server, chuẩn bị thực thi...")

            # Gửi phản hồi xác nhận
            await self.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "success",
                        "message": "Server đang khởi động lại...",
                        "content": {"action": "restart"},
                    }
                )
            )

            # Thực thi thao tác khởi động lại bất đồng bộ
            def restart_server():
                """Phương thức thực thi khởi động lại thực tế"""
                time.sleep(1)
                self.logger.bind(tag=TAG).info("Thực thi khởi động lại server...")
                subprocess.Popen(
                    [sys.executable, "app.py"],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    start_new_session=True,
                )
                os._exit(0)

            # Dùng thread thực thi khởi động lại để tránh chặn event loop
            threading.Thread(target=restart_server, daemon=True).start()

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Khởi động lại thất bại: {str(e)}")
            await self.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "error",
                        "message": f"Restart failed: {str(e)}",
                        "content": {"action": "restart"},
                    }
                )
            )

    def _initialize_components(self):
        try:
            if self.tts is None:
                self.tts = self._initialize_tts()
            # Mở kênh tổng hợp giọng nói
            asyncio.run_coroutine_threadsafe(
                self.tts.open_audio_channels(self), self.loop
            )
            if self.need_bind:
                self.bind_completed_event.set()
                return
            self.selected_module_str = build_module_string(
                self.config.get("selected_module", {})
            )
            self.logger = create_connection_logger(self.selected_module_str)

            """Khởi tạo thành phần"""
            if self.config.get("prompt") is not None:
                user_prompt = self.config["prompt"]
                # Dùng prompt nhanh để khởi tạo
                prompt = self.prompt_manager.get_quick_prompt(user_prompt)
                self.change_system_prompt(prompt)
                self.logger.bind(tag=TAG).info(
                    f"Khởi tạo nhanh thành phần: prompt thành công {prompt[:50]}..."
                )

            """Khởi tạo thành phần local"""
            if self.vad is None:
                self.vad = self._vad
            if self.asr is None:
                self.asr = self._initialize_asr()

            # Khởi tạo nhận diện voiceprint
            self._initialize_voiceprint()
            # Mở kênh nhận diện giọng nói
            asyncio.run_coroutine_threadsafe(
                self.asr.open_audio_channels(self), self.loop
            )

            """Tải memory"""
            self._initialize_memory()
            """Tải nhận diện ý định"""
            self._initialize_intent()
            """Khởi tạo thread báo cáo"""
            self._init_report_threads()
            """Cập nhật system prompt"""
            self._init_prompt_enhancement()
            """Inject ví dụ few-shot gọi tool (chỉ chế độ function_call)"""
            self._inject_tool_call_fewshot()

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Khởi tạo instance thành phần thất bại: {e}")

    def _init_prompt_enhancement(self):

        # Cập nhật thông tin ngữ cảnh
        self.prompt_manager.update_context_info(self, self.client_ip)
        enhanced_prompt = self.prompt_manager.build_enhanced_prompt(
            self.config["prompt"],
            self.device_id,
            self.client_ip,
            emoji_enabled=(self.features or {}).get("emoji", True),
        )
        if enhanced_prompt:
            self.change_system_prompt(enhanced_prompt)
            self.logger.bind(tag=TAG).debug("System prompt đã được cập nhật tăng cường")

    def _inject_tool_call_fewshot(self):
        """Inject ví dụ few-shot gọi tool vào lịch sử hội thoại.
        Cấu trúc: mẫu dương (ví dụ gọi tool) đặt trước system động, có thể trúng prefix cache;
        mẫu âm (ví dụ trả lời trực tiếp) đặt sau system động, sát ngay message người dùng thật,
        đảm bảo thứ model thấy cuối cùng trước khi xử lý message người dùng là mẫu hành vi "không gọi tool".
        """
        if self.intent_type != "function_call":
            return
        if not hasattr(self, "func_handler") or self.func_handler is None:
            return

        tools = self.func_handler.get_functions()
        if not tools:
            return

        tool_names = {t.get("function", {}).get("name") for t in tools}

        # === Ví dụ few-shot (is_temporary) ===
        # Minh họa cách dùng direct_answer mang param response, một lần gọi là trả lời xong

        # Ví dụ 1: direct_answer (nội dung trả lời viết trong param response, không cần đệ quy)
        da_tc_id = "fewshot_da_001"
        self.dialogue.put(Message(role="user", content="Kể cho tôi nghe một câu chuyện đi", is_temporary=True))
        self.dialogue.put(Message(
            role="assistant",
            tool_calls=[{
                "id": da_tc_id,
                "function": {"arguments":  '{"response": "Được chứ, bạn muốn nghe thể loại nào? Cổ tích, phiêu lưu hay hài hước? Chọn một đi, tôi kể cho nghe~"}', "name": "direct_answer"},
                "type": "function", "index": 0,
            }],
            is_temporary=True,
        ))
        self.dialogue.put(Message(
            role="tool", tool_call_id=da_tc_id,
            content="Đã trả lời trực tiếp", is_temporary=True,
        ))

        # Ví dụ 2: gọi tool thật (handle_exit_intent)
        if "handle_exit_intent" in tool_names:
            tc_id = "fewshot_exit_001"
            self.dialogue.put(Message(role="user", content="Tạm biệt", is_temporary=True))
            self.dialogue.put(Message(
                role="assistant",
                tool_calls=[{
                    "id": tc_id,
                    "function": {"arguments":  '{"say_goodbye": "Tạm biệt, hẹn gặp lại nhé~"}', "name": "handle_exit_intent"},
                    "type": "function", "index": 0,
                }],
                is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="tool", tool_call_id=tc_id,
                content="Đã xử lý ý định thoát", is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="assistant", content="Tạm biệt, hẹn gặp lại nhé~", is_temporary=True,
            ))

        self.logger.bind(tag=TAG).debug("Đã inject ví dụ few-shot gọi tool")

    def _init_report_threads(self):
        """Khởi tạo thread báo cáo ASR và TTS"""
        if not self.read_config_from_api or self.need_bind:
            return
        if self.chat_history_conf == 0:
            return
        if self.report_thread is None or not self.report_thread.is_alive():
            self.report_thread = threading.Thread(
                target=self._report_worker, daemon=True
            )
            self.report_thread.start()
            self.logger.bind(tag=TAG).info("Thread báo cáo TTS đã khởi động")

    def _initialize_tts(self):
        """Khởi tạo TTS"""
        tts = None
        if not self.need_bind:
            tts = initialize_tts(self.config)

        if tts is None:
            tts = DefaultTTS(self.config, delete_audio_file=True)

        return tts

    def _initialize_asr(self):
        """Khởi tạo ASR"""
        if (
                self._asr is not None
                and hasattr(self._asr, "interface_type")
                and self._asr.interface_type == InterfaceType.LOCAL
        ):
            # Nếu ASR công cộng là dịch vụ local thì trả về trực tiếp
            # Vì một instance ASR local có thể dùng chung cho nhiều kết nối
            asr = self._asr
        else:
            # Nếu ASR công cộng là dịch vụ remote thì khởi tạo instance mới
            # Vì ASR remote liên quan tới kết nối websocket và thread nhận, cần mỗi kết nối một instance
            asr = initialize_asr(self.config)

        return asr

    def _initialize_voiceprint(self):
        """Khởi tạo nhận diện voiceprint cho kết nối hiện tại"""
        try:
            voiceprint_config = self.config.get("voiceprint", {})
            if voiceprint_config:
                voiceprint_provider = VoiceprintProvider(voiceprint_config)
                if voiceprint_provider is not None and voiceprint_provider.enabled:
                    self.voiceprint_provider = voiceprint_provider
                    self.logger.bind(tag=TAG).info("Chức năng nhận diện voiceprint đã được bật động khi kết nối")
                else:
                    self.logger.bind(tag=TAG).warning("Chức năng nhận diện voiceprint đã bật nhưng cấu hình chưa đầy đủ")
            else:
                self.logger.bind(tag=TAG).info("Chức năng nhận diện voiceprint chưa bật")
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"Khởi tạo nhận diện voiceprint thất bại: {str(e)}")

    async def _background_initialize(self):
        """Khởi tạo cấu hình và thành phần ở nền (hoàn toàn không chặn vòng lặp chính)"""
        try:
            # Lấy cấu hình khác biệt bất đồng bộ
            await self._initialize_private_config_async()
            # Khởi tạo thành phần trong thread pool
            self.executor.submit(self._initialize_components)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Khởi tạo nền thất bại: {e}")

    async def _initialize_private_config_async(self):
        """Lấy cấu hình khác biệt từ interface bất đồng bộ (bản bất đồng bộ, không chặn vòng lặp chính)"""
        if not self.read_config_from_api:
            self.need_bind = False
            self.bind_completed_event.set()
            return
        try:
            begin_time = time.time()
            private_config = await get_private_config_from_api(
                self.config,
                self.headers.get("device-id"),
                self.headers.get("client-id", self.headers.get("device-id")),
            )
            private_config["delete_audio"] = bool(self.config.get("delete_audio", True))
            private_config["tts_timeout"] = self.config.get("tts_timeout", 15)
            self.logger.bind(tag=TAG).info(
                f"{time.time() - begin_time} giây, lấy cấu hình khác biệt bất đồng bộ thành công: {json.dumps(filter_sensitive_info(private_config), ensure_ascii=False)}"
            )
            self.need_bind = False
            self.bind_completed_event.set()
        except DeviceNotFoundException as e:
            self.need_bind = True
            private_config = {}
        except DeviceBindException as e:
            self.need_bind = True
            self.bind_code = e.bind_code
            private_config = {}
        except Exception as e:
            self.need_bind = True
            self.logger.bind(tag=TAG).error(f"Lấy cấu hình khác biệt bất đồng bộ thất bại: {e}")
            private_config = {}

        init_llm, init_tts, init_memory, init_intent = (
            False,
            False,
            False,
            False,
        )

        init_vad = check_vad_update(self.common_config, private_config)
        init_asr = check_asr_update(self.common_config, private_config)

        if init_vad:
            self.config["VAD"] = private_config["VAD"]
            self.config["selected_module"]["VAD"] = private_config["selected_module"][
                "VAD"
            ]
        if init_asr:
            self.config["ASR"] = private_config["ASR"]
            self.config["selected_module"]["ASR"] = private_config["selected_module"][
                "ASR"
            ]
        if private_config.get("TTS", None) is not None:
            init_tts = True
            self.config["TTS"] = private_config["TTS"]
            self.config["selected_module"]["TTS"] = private_config["selected_module"][
                "TTS"
            ]
        if private_config.get("LLM", None) is not None:
            init_llm = True
            self.config["LLM"] = private_config["LLM"]
            self.config["selected_module"]["LLM"] = private_config["selected_module"][
                "LLM"
            ]
        if private_config.get("VLLM", None) is not None:
            self.config["VLLM"] = private_config["VLLM"]
            self.config["selected_module"]["VLLM"] = private_config["selected_module"][
                "VLLM"
            ]
        if private_config.get("Memory", None) is not None:
            init_memory = True
            self.config["Memory"] = private_config["Memory"]
            self.config["selected_module"]["Memory"] = private_config[
                "selected_module"
            ]["Memory"]
        if private_config.get("Intent", None) is not None:
            init_intent = True
            self.config["Intent"] = private_config["Intent"]
            model_intent = private_config.get("selected_module", {}).get("Intent", {})
            self.config["selected_module"]["Intent"] = model_intent
            # Tải cấu hình plugin
            if model_intent != "Intent_nointent":
                plugin_from_server = private_config.get("plugins", {})
                for plugin, config_str in plugin_from_server.items():
                    plugin_from_server[plugin] = json.loads(config_str)
                # Sao chép cấu hình plugin cấp module sang từng tên hàm cụ thể,
                # tiện cho tra cứu per-function mô tả, news_sources v.v. về sau
                for module_name, func_names in module_func_map.items():
                    if module_name in plugin_from_server:
                        module_config = plugin_from_server[module_name]
                        for func_name in func_names:
                            if func_name not in plugin_from_server:
                                plugin_from_server[func_name] = module_config
                self.config["plugins"] = plugin_from_server
                # Mở rộng tên plugin cấp module thành tên hàm cụ thể
                expanded_functions = []
                for plugin_key in plugin_from_server.keys():
                    if plugin_key in all_function_registry:
                        expanded_functions.append(plugin_key)
                    elif plugin_key in module_func_map:
                        expanded_functions.extend(module_func_map[plugin_key])
                    else:
                        expanded_functions.append(plugin_key)
                self.config["Intent"][self.config["selected_module"]["Intent"]][
                    "functions"
                ] = expanded_functions
        if private_config.get("prompt", None) is not None:
            self.config["prompt"] = private_config["prompt"]
        # Lấy thông tin voiceprint
        if private_config.get("voiceprint", None) is not None:
            self.config["voiceprint"] = private_config["voiceprint"]
        if private_config.get("summaryMemory", None) is not None:
            self.config["summaryMemory"] = private_config["summaryMemory"]
        if private_config.get("device_max_output_size", None) is not None:
            self.max_output_size = int(private_config["device_max_output_size"])
        if private_config.get("chat_history_conf", None) is not None:
            self.chat_history_conf = int(private_config["chat_history_conf"])
        if private_config.get("mcp_endpoint", None) is not None:
            self.config["mcp_endpoint"] = private_config["mcp_endpoint"]
        if private_config.get("context_providers", None) is not None:
            self.config["context_providers"] = private_config["context_providers"]

        # Inject từ thay thế vào cấu hình module TTS
        if private_config.get("correct_words", None) is not None:
            select_tts_module = self.config["selected_module"]["TTS"]
            self.config["TTS"][select_tts_module]["correct_words"] = private_config[
                "correct_words"
            ]

        # Dùng run_in_executor thực thi initialize_modules trong thread pool, tránh chặn vòng lặp chính
        try:
            modules = await self.loop.run_in_executor(
                None,  # Dùng thread pool mặc định
                initialize_modules,
                self.logger,
                private_config,
                init_vad,
                init_asr,
                init_llm,
                init_tts,
                init_memory,
                init_intent,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Khởi tạo thành phần thất bại: {e}")
            modules = {}
        if modules.get("tts", None) is not None:
            self.tts = modules["tts"]
        if modules.get("vad", None) is not None:
            self.vad = modules["vad"]
        if modules.get("asr", None) is not None:
            self.asr = modules["asr"]
        if modules.get("llm", None) is not None:
            self.llm = modules["llm"]
        if modules.get("intent", None) is not None:
            self.intent = modules["intent"]
        if modules.get("memory", None) is not None:
            self.memory = modules["memory"]

    def _initialize_memory(self):
        if self.memory is None:
            return
        """Khởi tạo module memory"""
        self.memory.init_memory(
            role_id=self.device_id,
            llm=self.llm,
            summary_memory=self.config.get("summaryMemory", None),
            save_to_file=not self.read_config_from_api,
        )

        # Lấy cấu hình tóm tắt memory
        memory_config = self.config["Memory"]
        memory_type = self.config["Memory"][self.config["selected_module"]["Memory"]][
            "type"
        ]
        # Nếu dùng nomen hoặc mem_report_only thì trả về trực tiếp
        if memory_type == "nomem" or memory_type == "mem_report_only":
            return
        # Dùng chế độ mem_local_short
        elif memory_type == "mem_local_short":
            memory_llm_name = memory_config[self.config["selected_module"]["Memory"]][
                "llm"
            ]
            if memory_llm_name and memory_llm_name in self.config["LLM"]:
                # Nếu đã cấu hình LLM chuyên dụng thì tạo instance LLM riêng
                from core.utils import llm as llm_utils

                memory_llm_config = self.config["LLM"][memory_llm_name]
                memory_llm_type = memory_llm_config.get("type", memory_llm_name)
                memory_llm = llm_utils.create_instance(
                    memory_llm_type, memory_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"Đã tạo LLM chuyên dụng cho tóm tắt memory: {memory_llm_name}, loại: {memory_llm_type}"
                )
                self.memory.set_llm(memory_llm)
            else:
                # Ngược lại dùng LLM chính
                self.memory.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("Dùng LLM chính làm model nhận diện ý định")

    def _initialize_intent(self):
        if self.intent is None:
            return
        self.intent_type = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ]["type"]
        if self.intent_type == "function_call" or self.intent_type == "intent_llm":
            self.load_function_plugin = True
        """Khởi tạo module nhận diện ý định"""
        # Lấy cấu hình nhận diện ý định
        intent_config = self.config["Intent"]
        intent_type = self.config["Intent"][self.config["selected_module"]["Intent"]][
            "type"
        ]

        # Nếu dùng nointent thì trả về trực tiếp
        if intent_type == "nointent":
            return
        # Dùng chế độ intent_llm
        elif intent_type == "intent_llm":
            intent_llm_name = intent_config[self.config["selected_module"]["Intent"]][
                "llm"
            ]

            if intent_llm_name and intent_llm_name in self.config["LLM"]:
                # Nếu đã cấu hình LLM chuyên dụng thì tạo instance LLM riêng
                from core.utils import llm as llm_utils

                intent_llm_config = self.config["LLM"][intent_llm_name]
                intent_llm_type = intent_llm_config.get("type", intent_llm_name)
                intent_llm = llm_utils.create_instance(
                    intent_llm_type, intent_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"Đã tạo LLM chuyên dụng cho nhận diện ý định: {intent_llm_name}, loại: {intent_llm_type}"
                )
                self.intent.set_llm(intent_llm)
            else:
                # Ngược lại dùng LLM chính
                self.intent.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("Dùng LLM chính làm model nhận diện ý định")

        """Tải bộ xử lý tool thống nhất"""
        self.func_handler = UnifiedToolHandler(self)

        # Khởi tạo bất đồng bộ bộ xử lý tool
        if hasattr(self, "loop") and self.loop:
            asyncio.run_coroutine_threadsafe(self.func_handler._initialize(), self.loop)

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        # Cập nhật system prompt vào ngữ cảnh
        self.dialogue.update_system_message(self.prompt)

    def chat(self, query, depth=0):
        # Lưu sentence_id của task hiện tại vào biến cục bộ, tránh bị task mới ghi đè
        current_sentence_id = None

        if query is not None:
            self.logger.bind(tag=TAG).info(f"Model lớn nhận được message người dùng: {query}")

        # Tạo ID phiên mới và gửi request FIRST ở tầng ngoài cùng
        if depth == 0:
            current_sentence_id = str(uuid.uuid4().hex)
            self.sentence_id = current_sentence_id  # Cập nhật thuộc tính dùng chung
            self.dialogue.put(Message(role="user", content=query))
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )
        else:
            # Khi gọi đệ quy, dùng sentence_id hiện tại
            current_sentence_id = self.sentence_id

        # Đặt độ sâu đệ quy tối đa, tránh vòng lặp vô hạn, có thể điều chỉnh theo nhu cầu
        MAX_DEPTH = 5
        force_final_answer = False  # Đánh dấu có buộc trả lời cuối cùng không

        if depth >= MAX_DEPTH:
            self.logger.bind(tag=TAG).debug(
                f"Đã đạt độ sâu gọi tool tối đa {MAX_DEPTH}, sẽ buộc trả lời dựa trên thông tin hiện có"
            )
            force_final_answer = True
            # Thêm chỉ thị hệ thống, yêu cầu LLM trả lời dựa trên thông tin hiện có
            self.dialogue.put(
                Message(
                    role="user",
                    content="[System prompt] Đã đạt giới hạn số lần gọi tool tối đa, hãy dựa vào mọi thông tin đã có để đưa ra câu trả lời cuối cùng. Đừng thử gọi thêm tool nào nữa.",
                )
            )

        # Define intent functions
        functions = None
        # Khi đạt độ sâu tối đa, tắt gọi tool, buộc LLM trả lời trực tiếp
        if (
                self.intent_type == "function_call"
                and hasattr(self, "func_handler")
                and not force_final_answer
        ):
            functions = list(self.func_handler.get_functions())
            # Chỉ inject tool ảo direct_answer ở lần gọi tầng đầu
            # Gọi đệ quy (depth>0) không inject, tránh model gọi lại direct_answer khi sinh câu trả lời text gây vòng lặp
            if functions is not None and depth == 0:
                functions.append(DIRECT_ANSWER_TOOL)

        response_message = []

        try:
            # Dùng hội thoại có memory
            memory_str = None
            # Chỉ truy vấn memory khi query không rỗng (tức người dùng hỏi)
            if self.memory is not None and query:
                future = asyncio.run_coroutine_threadsafe(
                    self.memory.query_memory(query), self.loop
                )
                memory_str = future.result()

            # Chỉ inject danh tính vào system khi người nói này xuất hiện lần đầu, sau đó dựa vào lượt đầu của lịch sử hội thoại,
            # tránh tên lặp lại mỗi lượt trong system khiến model gọi tên liên tục
            speaker_for_system = None
            cs = (self.current_speaker or "").strip()
            if cs and cs != "未知说话人" and cs not in self.system_introduced_speakers:
                self.system_introduced_speakers.add(cs)
                speaker_for_system = cs

            if self.intent_type == "function_call" and functions is not None:
                # Dùng interface streaming hỗ trợ functions
                llm_responses = self.llm.response_with_functions(
                    self.session_id,
                    self.dialogue.get_llm_dialogue_with_memory(
                        memory_str, self.config.get("voiceprint", {}), speaker_for_system
                    ),
                    functions=functions,
                )
            else:
                llm_responses = self.llm.response(
                    self.session_id,
                    self.dialogue.get_llm_dialogue_with_memory(
                        memory_str, self.config.get("voiceprint", {}), speaker_for_system
                    ),
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM xử lý lỗi {query}: {e}")
            return None

        # Xử lý response streaming
        tool_call_flag = False
        # Hỗ trợ nhiều lệnh gọi tool song song - dùng list lưu trữ
        tool_calls_list = []  # Định dạng: [{"id": "", "name": "", "arguments": ""}]
        content_arguments = ""
        emotion_flag = True
        try:
            for response in llm_responses:
                if self.client_abort:
                    break
                if self.intent_type == "function_call" and functions is not None:
                    content, tools_call = response
                    if "content" in response:
                        content = response["content"]
                        tools_call = None
                    if content is not None and len(content) > 0:
                        content_arguments += content

                    if not tool_call_flag and content_arguments.startswith("<tool_call>"):
                        # print("content_arguments", content_arguments)
                        tool_call_flag = True

                    if tools_call is not None and len(tools_call) > 0:
                        tool_call_flag = True
                        self._merge_tool_calls(tool_calls_list, tools_call)

                    # Trích xuất streaming param response của direct_answer, gửi TTS realtime
                    # Dùng bộ đệm an toàn, tránh ký hiệu đóng JSON lọt vào TTS
                    _DA_STREAM_BUFFER = 5
                    for tc in tool_calls_list:
                        if tc["name"] == "direct_answer" and tc.get("arguments"):
                            da_text = self._extract_direct_answer_response(tc["arguments"])
                            sent_len = tc.get("_da_sent", 0)
                            if da_text and len(da_text) > sent_len:
                                safe_end = max(sent_len, len(da_text) - _DA_STREAM_BUFFER)
                                if safe_end > sent_len:
                                    new_part = da_text[sent_len:safe_end]
                                    # Dọn rác đóng JSON có thể lọt trong delta
                                    new_part = self._clean_response_garbage(new_part)
                                    if new_part:
                                        tc["_da_sent"] = safe_end
                                        self.tts.tts_text_queue.put(
                                            TTSMessageDTO(
                                                sentence_id=current_sentence_id,
                                                sentence_type=SentenceType.MIDDLE,
                                                content_type=ContentType.TEXT,
                                                content_detail=new_part,
                                            )
                                        )
                else:
                    content = response

                # Lấy emoji cảm xúc trong câu trả lời LLM, mỗi lượt hội thoại chỉ lấy một lần ở đầu
                if emotion_flag and content is not None and content.strip():
                    if (self.features or {}).get("emoji", True):
                        asyncio.run_coroutine_threadsafe(
                            textUtils.get_emotion(self, content),
                            self.loop,
                        )
                    emotion_flag = False

                if content is not None and len(content) > 0:
                    if not tool_call_flag:
                        response_message.append(content)
                        self.tts.tts_text_queue.put(
                            TTSMessageDTO(
                                sentence_id=current_sentence_id,
                                sentence_type=SentenceType.MIDDLE,
                                content_type=ContentType.TEXT,
                                content_detail=content,
                            )
                        )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM stream processing error: {e}")
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=get_system_error_response(self.config),
                )
            )
            if depth == 0:
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
            return
        # Xử lý function call
        if tool_call_flag:
            bHasError = False
            # Xử lý định dạng gọi tool dựa trên text
            if len(tool_calls_list) == 0 and content_arguments:
                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        tool_calls_list.append(
                            {
                                "id": str(uuid.uuid4().hex),
                                "name": content_arguments_json["name"],
                                "arguments": json.dumps(
                                    content_arguments_json["arguments"],
                                    ensure_ascii=False,
                                ),
                            }
                        )
                    except Exception as e:
                        bHasError = True
                        response_message.append(a)
                else:
                    bHasError = True
                    response_message.append(content_arguments)
                if bHasError:
                    self.logger.bind(tag=TAG).error(
                        f"function call error: {content_arguments}"
                    )

            if not bHasError and len(tool_calls_list) > 0:
                # Xử lý tool ảo direct_answer
                direct_answer_calls = [tc for tc in tool_calls_list if tc["name"] == "direct_answer"]
                real_tool_calls = [tc for tc in tool_calls_list if tc["name"] != "direct_answer"]

                if direct_answer_calls:
                    self.logger.bind(tag=TAG).debug(
                        f"Model chọn direct_answer, streaming đã phát, ghi vào lịch sử hội thoại"
                    )
                    for tc in direct_answer_calls:
                        da_response = self._extract_direct_answer_response(tc.get("arguments", "{}"))
                        if da_response:
                            # Đẩy phần chưa gửi trong bộ đệm streaming
                            sent_len = tc.get("_da_sent", 0)
                            remaining = da_response[sent_len:]
                            if remaining:
                                remaining = self._clean_response_garbage(remaining)
                                if remaining:
                                    self.tts.tts_text_queue.put(
                                        TTSMessageDTO(
                                            sentence_id=current_sentence_id,
                                            sentence_type=SentenceType.MIDDLE,
                                            content_type=ContentType.TEXT,
                                            content_detail=remaining,
                                        )
                                    )
                            # Ghi vào lịch sử hội thoại
                            da_response = self._clean_response_garbage(da_response)
                            self.tts.store_tts_text(current_sentence_id, da_response)
                            self.dialogue.put(Message(role="assistant", content=da_response))

                    if not real_tool_calls:
                        if depth == 0:
                            self.tts.tts_text_queue.put(
                                TTSMessageDTO(
                                    sentence_id=current_sentence_id,
                                    sentence_type=SentenceType.LAST,
                                    content_type=ContentType.ACTION,
                                )
                            )
                        return

                    tool_calls_list = real_tool_calls

            if not bHasError and len(tool_calls_list) > 0:
                self.logger.bind(tag=TAG).debug(
                    f"Phát hiện {len(tool_calls_list)} lệnh gọi tool"
                )

                # Text LLM đã phát ở giai đoạn streaming
                streamed_text = ""
                if len(response_message) > 0:
                    streamed_text = "".join(response_message)
                    self.tts.store_tts_text(current_sentence_id, streamed_text)
                    self.dialogue.put(Message(role="assistant", content=streamed_text))
                response_message.clear()

                # Thu thập Future của mọi lệnh gọi tool
                futures_with_data = []
                for tool_call_data in tool_calls_list:
                    self.logger.bind(tag=TAG).debug(
                        f"function_name={tool_call_data['name']}, function_id={tool_call_data['id']}, function_arguments={tool_call_data['arguments']}"
                    )

                    # Dùng phương thức chung báo cáo gọi tool
                    tool_input = json.loads(tool_call_data.get("arguments") or "{}")
                    enqueue_tool_report(self, tool_call_data['name'], tool_input)

                    future = asyncio.run_coroutine_threadsafe(
                        self.func_handler.handle_llm_function_call(
                            self, tool_call_data
                        ),
                        self.loop,
                    )
                    futures_with_data.append((future, tool_call_data, tool_input))

                # Thời gian timeout gọi tool, có thể cấu hình, mặc định 30 giây
                tool_call_timeout = int(self.config.get("tool_call_timeout", 30))
                # Chờ coroutine kết thúc (thời gian chờ thực tế là cái chậm nhất)
                tool_results = []

                for future, tool_call_data, tool_input in futures_with_data:
                    try:
                        result = future.result(timeout=tool_call_timeout)
                        tool_results.append((result, tool_call_data))
                        # Dùng phương thức chung báo cáo kết quả gọi tool
                        enqueue_tool_report(self, tool_call_data['name'], tool_input, str(result.result) if result.result else None, report_tool_call=False)

                    except Exception as e:
                        self.logger.bind(tag=TAG).error(
                            f"Gọi tool timeout hoặc bất thường: {tool_call_data['name']}, lỗi: {e}"
                        )
                        # Khi timeout trả về phản hồi lỗi, tránh cả quy trình kẹt cứng
                        tool_results.append((
                            ActionResponse(action=Action.ERROR, result="Ôi, mạng gặp chút vấn đề, bạn thử lại sau nhé!"),
                            tool_call_data
                        ))
                        # Báo cáo lỗi gọi tool
                        enqueue_tool_report(self, tool_call_data['name'], tool_input, str(e), report_tool_call=False)

                # Xử lý thống nhất kết quả gọi tool
                if tool_results:
                    self._handle_function_result(tool_results, depth=depth, streamed_text=streamed_text)

        # Lưu nội dung hội thoại
        if len(response_message) > 0:
            text_buff = "".join(response_message)
            self.tts.store_tts_text(current_sentence_id, text_buff)
            self.dialogue.put(Message(role="assistant", content=text_buff))

        if depth == 0:
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )
            # Dùng lambda tính trễ, chỉ thực thi get_llm_dialogue() ở cấp DEBUG
            self.logger.bind(tag=TAG).debug(
                lambda: json.dumps(
                    self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False
                )
            )

        return True

    def _handle_function_result(self, tool_results, depth, streamed_text=""):
        need_llm_tools = []
        record_tools = []

        for result, tool_call_data in tool_results:
            if result.action in [
                Action.RESPONSE,
                Action.NOTFOUND,
                Action.ERROR,
            ]:
                text = result.response if result.response else result.result
                if streamed_text and text in streamed_text:
                    self.logger.bind(tag=TAG).debug(
                        f"Skipping duplicate TTS for tool {tool_call_data['name']}, already streamed"
                    )
                else:
                    self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
                    self.tts.store_tts_text(self.sentence_id, text)
                self.dialogue.put(Message(role="assistant", content=text))
            elif result.action == Action.REQLLM:
                need_llm_tools.append((result, tool_call_data))
            elif result.action == Action.RECORD:
                record_tools.append((result, tool_call_data))
            else:
                pass

        # Action.RECORD: ghi chuỗi gọi tool đầy đủ (assistant(tool_calls) → tool(result) → assistant(response))
        # Model học mẫu gọi tool từ lịch sử, không gọi thêm LLM
        if record_tools:
            # Dựng message assistant (gồm tool_calls), ghi lại "model đã gọi những tool nào"
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(record_tools)
            ]
            self.dialogue.put(Message(role="assistant", tool_calls=all_tool_calls))

            # Ghi kết quả thực thi của từng tool, ghi lại "tool đã trả về gì"
            for result, tool_call_data in record_tools:
                text = result.result or ""
                self.dialogue.put(
                    Message(
                        role="tool",
                        tool_call_id=(
                            str(uuid.uuid4())
                            if tool_call_data["id"] is None
                            else tool_call_data["id"]
                        ),
                        content=text,
                    )
                )

            # Dùng text cố định làm câu trả lời cuối, bổ sung đủ ba đoạn chuẩn, đảm bảo message tiếp theo là user chứ không nối tool
            response_parts = []
            for result, _ in record_tools:
                resp = result.response or result.result
                if resp:
                    response_parts.append(resp)
            if response_parts:
                self.dialogue.put(Message(role="assistant", content="，".join(response_parts)))

        if need_llm_tools:
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(need_llm_tools)
            ]
            self.dialogue.put(Message(role="assistant", tool_calls=all_tool_calls))

            for result, tool_call_data in need_llm_tools:
                text = result.result
                if text is not None and len(text) > 0:
                    self.dialogue.put(
                        Message(
                            role="tool",
                            tool_call_id=(
                                str(uuid.uuid4())
                                if tool_call_data["id"] is None
                                else tool_call_data["id"]
                            ),
                            content=text,
                        )
                    )

            self.chat(None, depth=depth + 1)

    def _report_worker(self):
        """Thread công việc báo cáo lịch sử chat"""
        while not self.stop_event.is_set():
            try:
                # Lấy dữ liệu từ hàng đợi, đặt timeout để kiểm tra định kỳ sự kiện dừng
                item = self.report_queue.get(timeout=1)
                if item is None:  # Phát hiện đối tượng poison pill
                    break
                try:
                    # Kiểm tra trạng thái thread pool
                    if self.executor is None:
                        continue
                    # Nộp task vào thread pool
                    self.executor.submit(self._process_report, *item)
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"Thread báo cáo lịch sử chat bất thường: {e}")
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"Thread công việc báo cáo lịch sử chat bất thường: {e}")

        self.logger.bind(tag=TAG).info("Thread báo cáo lịch sử chat đã thoát")

    def _process_report(self, type, text, audio_data, report_time):
        """Xử lý task báo cáo"""
        try:
            # Thực thi báo cáo bất đồng bộ (chạy trong event loop)
            asyncio.run(report(self, type, text, audio_data, report_time))
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Xử lý báo cáo bất thường: {e}")
        finally:
            # Đánh dấu task hoàn tất
            self.report_queue.task_done()

    def clearSpeakStatus(self):
        self.client_is_speaking = False
        self.logger.bind(tag=TAG).debug(f"Xoá trạng thái nói của phía server")

    async def close(self, ws=None):
        """Phương thức dọn tài nguyên"""
        try:
            # Dọn tài nguyên kết nối VAD
            if (
                    hasattr(self, "vad")
                    and self.vad
                    and hasattr(self.vad, "release_conn_resources")
            ):
                self.vad.release_conn_resources(self)

            # Dọn bộ giải mã opus
            if hasattr(self, "_connection_opus_decoder"):
                try:
                    delattr(self, "_connection_opus_decoder")
                except Exception:
                    pass

            # Dọn bộ đệm audio
            if hasattr(self, "audio_buffer"):
                self.audio_buffer.clear()

            # Huỷ task timeout
            if self.timeout_task and not self.timeout_task.done():
                self.timeout_task.cancel()
                try:
                    await self.timeout_task
                except asyncio.CancelledError:
                    pass
                self.timeout_task = None

            # Huỷ task dọn cache AEC
            if hasattr(self, "_aec_cache_cleanup_task") and self._aec_cache_cleanup_task and not self._aec_cache_cleanup_task.done():
                self._aec_cache_cleanup_task.cancel()
                try:
                    await self._aec_cache_cleanup_task
                except asyncio.CancelledError:
                    pass
                self._aec_cache_cleanup_task = None

            # Dọn cache AEC
            if hasattr(self, "aec_audio_cache"):
                self.aec_audio_cache.clear()
                self.aec_audio_cache_time.clear()

            # Dọn tài nguyên bộ xử lý tool
            if hasattr(self, "func_handler") and self.func_handler:
                try:
                    await self.func_handler.cleanup()
                except Exception as cleanup_error:
                    self.logger.bind(tag=TAG).error(
                        f"Lỗi khi dọn tài nguyên bộ xử lý tool: {cleanup_error}"
                    )

            # Kích hoạt sự kiện dừng
            if self.stop_event:
                self.stop_event.set()

            # Xoá sạch hàng đợi task
            self.clear_queues()

            # Đóng kết nối WebSocket
            try:
                if ws:
                    # Kiểm tra an toàn trạng thái WebSocket rồi đóng
                    try:
                        if hasattr(ws, "closed") and not ws.closed:
                            await ws.close()
                        elif hasattr(ws, "state") and ws.state.name != "CLOSED":
                            await ws.close()
                        else:
                            # Nếu không có thuộc tính closed thì thử đóng trực tiếp
                            await ws.close()
                    except Exception:
                        # Nếu đóng thất bại thì bỏ qua lỗi
                        pass
                elif self.websocket:
                    try:
                        if (
                                hasattr(self.websocket, "closed")
                                and not self.websocket.closed
                        ):
                            await self.websocket.close()
                        elif (
                                hasattr(self.websocket, "state")
                                and self.websocket.state.name != "CLOSED"
                        ):
                            await self.websocket.close()
                        else:
                            # Nếu không có thuộc tính closed thì thử đóng trực tiếp
                            await self.websocket.close()
                    except Exception:
                        # Nếu đóng thất bại thì bỏ qua lỗi
                        pass
            except Exception as ws_error:
                self.logger.bind(tag=TAG).error(f"Lỗi khi đóng kết nối WebSocket: {ws_error}")

            if self.tts:
                await self.tts.close()
            if self.asr:
                await self.asr.close()

            # Cuối cùng đóng thread pool (tránh chặn)
            if self.executor:
                try:
                    self.executor.shutdown(wait=False)
                except Exception as executor_error:
                    self.logger.bind(tag=TAG).error(
                        f"Lỗi khi đóng thread pool: {executor_error}"
                    )
                self.executor = None
            self.logger.bind(tag=TAG).info("Tài nguyên kết nối đã giải phóng")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Lỗi khi đóng kết nối: {e}")
        finally:
            # Đảm bảo sự kiện dừng được đặt
            if self.stop_event:
                self.stop_event.set()

    def clear_queues(self):
        """Xoá sạch mọi hàng đợi task"""
        if self.tts:
            self.logger.bind(tag=TAG).debug(
                f"Bắt đầu dọn: kích thước hàng đợi TTS={self.tts.tts_text_queue.qsize()}, kích thước hàng đợi audio={self.tts.tts_audio_queue.qsize()}"
            )

            # Xoá hàng đợi bằng cách không chặn
            for q in [
                self.tts.tts_text_queue,
                self.tts.tts_audio_queue,
                self.report_queue,
            ]:
                if not q:
                    continue
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

            # Đặt lại bộ điều tiết luồng audio (huỷ task nền và xoá hàng đợi)
            if hasattr(self, "audio_rate_controller") and self.audio_rate_controller:
                self.audio_rate_controller.reset()
                self.logger.bind(tag=TAG).debug("Đã đặt lại bộ điều tiết luồng audio")

            self.logger.bind(tag=TAG).debug(
                f"Dọn xong: kích thước hàng đợi TTS={self.tts.tts_text_queue.qsize()}, kích thước hàng đợi audio={self.tts.tts_audio_queue.qsize()}"
            )

    def reset_audio_states(self):
        """
        Đặt lại mọi trạng thái liên quan audio (VAD + ASR)
        """
        # Reset VAD states
        self.client_audio_buffer.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window.clear()
        self.last_is_voice = False
        self.vad_last_voice_time = 0.0

        # Clear ASR buffers
        self.asr_audio.clear()

        self.logger.bind(tag=TAG).debug("All audio states reset.")

    def chat_and_close(self, text):
        """Chat with the user and then close the connection"""
        try:
            # Use the existing chat method
            self.chat(text)

            # After chat is complete, close the connection
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    async def _check_timeout(self):
        """Kiểm tra timeout kết nối"""
        try:
            while not self.stop_event.is_set():
                last_activity_time = self.last_activity_time
                if self.need_bind:
                    last_activity_time = self.first_activity_time

                # Kiểm tra có timeout không (chỉ khi timestamp đã khởi tạo)
                if last_activity_time > 0.0:
                    current_time = time.time() * 1000
                    if current_time - last_activity_time > self.timeout_seconds * 1000:
                        if not self.stop_event.is_set():
                            self.logger.bind(tag=TAG).info("Kết nối timeout, chuẩn bị đóng")
                            # Đặt sự kiện dừng, tránh xử lý trùng
                            self.stop_event.set()
                            # Dùng try-except bọc thao tác đóng, đảm bảo không bị chặn vì ngoại lệ
                            try:
                                await self.close(self.websocket)
                            except Exception as close_error:
                                self.logger.bind(tag=TAG).error(
                                    f"Lỗi khi đóng kết nối do timeout: {close_error}"
                                )
                        break
                # Kiểm tra mỗi 10 giây, tránh quá thường xuyên
                await asyncio.sleep(10)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Task kiểm tra timeout lỗi: {e}")
        finally:
            self.logger.bind(tag=TAG).info("Task kiểm tra timeout đã thoát")

    async def _check_aec_cache_expiry(self):
        """Dọn định kỳ cache AEC hết hạn"""
        try:
            while not self.stop_event.is_set():
                if hasattr(self, "aec_audio_cache") and self.aec_audio_cache:
                    current_time = time.time()
                    expired_keys = [
                        ts for ts, cache_time in list(self.aec_audio_cache_time.items())
                        if current_time - cache_time > 120  # Hết hạn sau 2 phút
                    ]
                    for ts in expired_keys:
                        self.aec_audio_cache.pop(ts, None)
                        self.aec_audio_cache_time.pop(ts, None)
                    if expired_keys:
                        self.logger.bind(tag=TAG).debug(f"[AEC] Dọn cache hết hạn {len(expired_keys)} mục")
                # Kiểm tra mỗi 30 giây
                await asyncio.sleep(30)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Task dọn cache AEC lỗi: {e}")

    @staticmethod
    def _extract_direct_answer_response(arguments_str):
        """Trích xuất giá trị response từ param của direct_answer.
        Ưu tiên dùng json.loads phân tích chuẩn, giai đoạn streaming fallback sang trích xuất chuỗi.
        """
        if not arguments_str:
            return ""
        # Ưu tiên thử phân tích JSON chuẩn (dùng cho JSON đầy đủ và đúng định dạng)
        try:
            data = json.loads(arguments_str)
            if isinstance(data, dict) and "response" in data:
                return data["response"]
        except (json.JSONDecodeError, TypeError):
            pass
        # Fallback: JSON giai đoạn streaming có thể chưa đầy đủ, dùng trích xuất chuỗi
        marker = '"response": "'
        idx = arguments_str.find(marker)
        if idx < 0:
            marker = '"response":"'
            idx = arguments_str.find(marker)
        if idx < 0:
            return ""
        start = idx + len(marker)
        raw = arguments_str[start:]
        # Bỏ ký hiệu đóng JSON ở cuối (nếu đã đầy đủ)
        if raw.endswith('"}'):
            raw = raw[:-2]
        elif raw.endswith('"'):
            raw = raw[:-1]
        # Xử lý escape JSON
        raw = raw.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
        return raw

    @staticmethod
    def _clean_response_garbage(text):
        """Dọn ký hiệu đóng JSON có thể lọt trong response.
        Model đôi khi sinh ký tự đóng JSON trong nội dung response (như ）"}} 或 '})，
        Chúng không phải một phần nội dung câu chuyện, cần loại bỏ.
        """
        if not text:
            return text
        # Dọn rác đóng JSON đứng riêng một dòng（如 ）"}}  '}}  "}}  }}  } ）
        _garbage_chars = frozenset('")\'}）')
        lines = text.split('\n')
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if stripped and len(stripped) <= 8 and all(c in _garbage_chars for c in stripped):
                continue
            cleaned.append(line)
        result = '\n'.join(cleaned)
        # Dọn ký hiệu đóng JSON còn sót ở cuối
        result = re.sub(r'["\'}\]]+$', '', result.rstrip()).rstrip()
        return result

    def _merge_tool_calls(self, tool_calls_list, tools_call):
        """Hợp nhất danh sách gọi tool

        Args:
            tool_calls_list: Danh sách gọi tool đã thu thập
            tools_call: Lệnh gọi tool mới
        """
        for tool_call in tools_call:
            tool_index = getattr(tool_call, "index", None)
            if tool_index is None:
                if tool_call.function.name:
                    # Có function_name nghĩa là lệnh gọi tool mới
                    tool_index = len(tool_calls_list)
                else:
                    tool_index = len(tool_calls_list) - 1 if tool_calls_list else 0

            # Đảm bảo list có đủ vị trí
            if tool_index >= len(tool_calls_list):
                tool_calls_list.append({"id": "", "name": "", "arguments": ""})

            # Cập nhật thông tin gọi tool
            if tool_call.id:
                tool_calls_list[tool_index]["id"] = tool_call.id
            if tool_call.function.name:
                tool_calls_list[tool_index]["name"] = tool_call.function.name
            if tool_call.function.arguments:
                tool_calls_list[tool_index]["arguments"] += tool_call.function.arguments
