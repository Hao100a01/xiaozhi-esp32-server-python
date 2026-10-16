import os
import uuid
import json
import time
import queue
import asyncio
import traceback
import websockets

from typing import Callable, Any
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.utils.alibl_endpoint import build_ws_connect_options, resolve_ws_url
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "rate", 0.5, 2.0, 1.0, lambda v: round(v, 1)),
        ("ttsPitch", "pitch", 0.5, 2.0, 1.0, lambda v: round(v, 1)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        self.interface_type = InterfaceType.DUAL_STREAM
        # Cấu hình cơ bản
        self.api_key = config.get("api_key")
        if not self.api_key:
            raise ValueError("api_key is required for CosyVoice TTS")

        # Cấu hình WebSocket
        self.ws_url = resolve_ws_url(config.get("ws_url"))
        self.ws_connect_options = build_ws_connect_options(self.tts_timeout)
        self.ws = None
        self._monitor_task = None
        self.activate_session = False
        self.last_active_time = None

        # Cấu hình mô hình và giọng đọc
        self.model = config.get("model", "cosyvoice-v2")
        self.voice = config.get("voice", "longxiaochun_v2")  # Giọng đọc mặc định
        if config.get("private_voice"):
            self.voice = config.get("private_voice")

        # Cấu hình tham số âm thanh
        self.format = config.get("format", "pcm")

        volume = config.get("volume", "50")
        self.volume = int(volume) if volume else 50

        rate = config.get("rate", "1.0")
        self.rate = float(rate) if rate else 1.0

        pitch = config.get("pitch", "1.0")
        self.pitch = float(pitch) if pitch else 1.0

        # Áp dụng điều chỉnh phần trăm (nếu có), ngược lại dùng cấu hình chung
        self._apply_percentage_params(config)

        self.header = {
            "Authorization": f"Bearer {self.api_key}",
            # "user-agent": "your_platform_info", // Tuỳ chọn
            # "X-DashScope-WorkSpace": workspace, // Tuỳ chọn, ID không gian nghiệp vụ Alibaba Cloud Bailian
            "X-DashScope-DataInspection": "enable",
        }

    async def _ensure_connection(self):
        """Đảm bảo kết nối WebSocket khả dụng, hỗ trợ tái dùng kết nối trong 60 giây"""
        try:
            current_time = time.time()
            if self.ws and current_time - self.last_active_time < 60:
                # Trong một phút mới được tái dùng liên kết để hội thoại liên tục
                logger.bind(tag=TAG).debug(f"Dùng liên kết đã có...")
                return self.ws
            logger.bind(tag=TAG).debug("Bắt đầu thiết lập kết nối mới...")

            # Huỷ tác vụ lắng nghe cũ trước khi thiết lập kết nối mới
            await self._cancel_monitor_task()

            self.ws = await websockets.connect(
                self.ws_url,
                additional_headers=self.header,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=10,
                **self.ws_connect_options,
            )

            logger.bind(tag=TAG).debug("Thiết lập kết nối WebSocket thành công")
            self.last_active_time = current_time
            return self.ws
        except Exception as e:
            logger.bind(tag=TAG).error(f"Thiết lập kết nối thất bại: {str(e)}")
            self.ws = None
            self.last_active_time = None
            raise

    def tts_text_priority_thread(self):
        """Luồng xử lý văn bản TTS streaming"""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)

                if self.conn.client_abort:
                    try:
                        logger.bind(tag=TAG).info("Đã nhận thông tin ngắt, dừng luồng xử lý văn bản TTS")
                        asyncio.run_coroutine_threadsafe(
                            self.finish_session(self.conn.sentence_id),
                            loop=self.conn.loop,
                        )
                        continue
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"Huỷ phiên TTS thất bại: {str(e)}")
                        continue

                # Lọc thông điệp cũ: kiểm tra sentence_id có khớp không
                if message.sentence_id != self.conn.sentence_id:
                    continue

                logger.bind(tag=TAG).debug(
                    f"Đã nhận tác vụ TTS｜{message.sentence_type.name} ｜ {message.content_type.name} | ID phiên: {message.sentence_id}"
                )

                if message.sentence_type == SentenceType.FIRST:
                    # Đặt lại trạng thái xử lý streaming
                    self.reset_stream_state()
                    # Khởi tạo phiên
                    try:
                        if not getattr(self.conn, "sentence_id", None): 
                            self.conn.sentence_id = uuid.uuid4().hex
                            logger.bind(tag=TAG).debug(f"Tự động tạo ID phiên mới: {self.conn.sentence_id}")

                        logger.bind(tag=TAG).debug("Bắt đầu khởi động phiên TTS...")
                        future = asyncio.run_coroutine_threadsafe(
                            self.start_session(self.conn.sentence_id),
                            loop=self.conn.loop,
                        )
                        future.result(timeout=self.tts_timeout)
                        self.before_stop_play_files.clear()
                        logger.bind(tag=TAG).debug("Khởi động phiên TTS thành công")
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"Khởi động phiên TTS thất bại: {str(e)}")
                        continue

                elif ContentType.TEXT == message.content_type:
                    if message.content_detail:
                        try:
                            logger.bind(tag=TAG).debug(
                                f"Bắt đầu gửi văn bản TTS: {message.content_detail}"
                            )
                            future = asyncio.run_coroutine_threadsafe(
                                self.text_to_speak(message.content_detail, None),
                                loop=self.conn.loop,
                            )
                            future.result(timeout=self.tts_timeout)
                        except Exception as e:
                            logger.bind(tag=TAG).error(f"Gửi văn bản TTS thất bại: {str(e)}")
                            continue

                elif ContentType.FILE == message.content_type:
                    logger.bind(tag=TAG).info(
                        f"Thêm tệp âm thanh vào danh sách chờ phát: {message.content_file}"
                    )
                    if message.content_file and os.path.exists(message.content_file):
                        # Xử lý trước dữ liệu âm thanh tệp
                        self._process_audio_file_stream(message.content_file, callback=lambda audio_data: self.handle_audio_file(audio_data, message.content_detail))

                if message.sentence_type == SentenceType.LAST:
                    try:
                        logger.bind(tag=TAG).debug("Bắt đầu kết thúc phiên TTS...")
                        future = asyncio.run_coroutine_threadsafe(
                            self.finish_session(self.conn.sentence_id),
                            loop=self.conn.loop,
                        )
                        future.result()
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"Kết thúc phiên TTS thất bại: {str(e)}")
                        continue

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Xử lý văn bản TTS thất bại: {str(e)}, kiểu: {type(e).__name__}, ngăn xếp: {traceback.format_exc()}"
                )
                continue

    async def text_to_speak(self, text, _):
        """Gửi văn bản tới dịch vụ TTS để tổng hợp"""
        try:
            if self.ws is None:
                logger.bind(tag=TAG).warning("Kết nối WebSocket không tồn tại, dừng gửi văn bản")
                return

            # Lọc Markdown
            filtered_text = MarkdownCleaner.clean_markdown(text)

            if filtered_text:
                # Dùng khớp cửa sổ trượt để xử lý từ thay thế xuyên mảnh
                confirmed_texts, self._pending_prefix = self._match_stream_text(filtered_text)

                # Gửi từng đoạn văn bản đã xác định
                for txt in confirmed_texts:
                    if txt and self.ws:
                        continue_task_message = {
                            "header": {
                                "action": "continue-task",
                                "task_id": self.conn.sentence_id,
                                "streaming": "duplex",
                            },
                            "payload": {"input": {"text": txt}},
                        }
                        await self.ws.send(json.dumps(continue_task_message))
                        self.last_active_time = time.time()
            return
        except Exception as e:
            logger.bind(tag=TAG).error(f"Gửi văn bản TTS thất bại: {str(e)}")
            if self.ws:
                try:
                    await self.ws.close()
                except:
                    pass
                self.ws = None
            raise

    async def start_session(self, session_id):
        """Khởi động phiên TTS"""
        logger.bind(tag=TAG).debug(f"Bắt đầu phiên～～{session_id}")
        try:
            # Khi phiên trước đang hoạt động thì đóng kết nối trước và tạo liên kết mới
            if self.activate_session:
                await self.close()

            # Đặt cờ kích hoạt phiên
            self.activate_session = True

            # Đảm bảo kết nối khả dụng
            await self._ensure_connection()

            # Khởi động tác vụ lắng nghe
            if self._monitor_task is None or self._monitor_task.done():
                logger.bind(tag=TAG).debug("Khởi động tác vụ lắng nghe...")
                self._monitor_task = asyncio.create_task(self._start_monitor_tts_response())

            # Gửi thông điệp run-task để khởi động phiên
            run_task_message = {
                "header": {
                    "action": "run-task",
                    "task_id": session_id,
                    "streaming": "duplex",
                },
                "payload": {
                    "task_group": "audio",
                    "task": "tts",
                    "function": "SpeechSynthesizer",
                    "model": self.model,
                    "parameters": {
                        "text_type": "PlainText",
                        "voice": self.voice,
                        "format": self.format,
                        "sample_rate": self.conn.sample_rate,
                        "volume": self.volume,
                        "rate": self.rate,
                        "pitch": self.pitch,
                    },
                    "input": {}
                },
            }

            await self.ws.send(json.dumps(run_task_message))
            self.last_active_time = time.time()
            logger.bind(tag=TAG).debug("Yêu cầu khởi động phiên đã được gửi")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Khởi động phiên thất bại: {str(e)}")
            await self.close()
            raise

    async def finish_session(self, session_id):
        """Kết thúc phiên TTS"""
        logger.bind(tag=TAG).debug(f"Đóng phiên～～{session_id}")
        try:
            if self.ws and session_id:
                # Gửi thông điệp finish-task
                finish_task_message = {
                    "header": {
                        "action": "finish-task",
                        "task_id": session_id,
                        "streaming": "duplex",
                    },
                    "payload": {
                        "input": {}
                    }
                }

                await self.ws.send(json.dumps(finish_task_message))
                self.last_active_time = time.time()

        except Exception as e:
            logger.bind(tag=TAG).error(f"Đóng phiên thất bại: {str(e)}")
            await self.close()
            raise

    async def close(self):
        """Dọn dẹp tài nguyên"""
        await super().close()
        self.activate_session = False
        await self._cancel_monitor_task()

        # Đóng kết nối WebSocket
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None
            self.last_active_time = None
    
    async def _cancel_monitor_task(self):
        """Huỷ tác vụ lắng nghe"""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.bind(tag=TAG).warning(f"Lỗi huỷ tác vụ lắng nghe: {e}")
        self._monitor_task = None

    async def _start_monitor_tts_response(self):
        """Lắng nghe phản hồi TTS - chạy dài hạn"""
        try:
            while not self.conn.stop_event.is_set():
                try:
                    msg = await self.ws.recv()
                    self.last_active_time = time.time()

                    if isinstance(msg, str):  # Thông điệp điều khiển JSON
                        try:
                            data = json.loads(msg)
                            header = data.get("header", {})
                            event = header.get("event")
                            task_id = header.get("task_id")

                            # Chỉ xử lý phản hồi của phiên đang hoạt động hiện tại
                            if task_id and self.conn.sentence_id != task_id:
                                if event in ["task-finished", "task-failed"]:
                                    logger.bind(tag=TAG).debug(f"Đã nhận phản hồi kết thúc downlink còn sót, đặt lại trạng thái phiên～～")
                                    self.activate_session = False
                                continue

                            if event == "task-started":
                                logger.bind(tag=TAG).debug("Khởi động tác vụ TTS thành công~")
                            elif event == "result-generated":
                                output = data.get("payload", {}).get("output", {})
                                if output.get("type") == "sentence-begin":
                                    original_text = output.get("original_text")
                                    self.tts_text = self._restore_original_text(original_text)
                                    logger.bind(tag=TAG).debug(f"Bắt đầu tạo giọng nói câu: {self.tts_text}")
                                    self.tts_audio_queue.put((SentenceType.FIRST, [], self.tts_text))
                                elif output.get("type") == "sentence-end":
                                    logger.bind(tag=TAG).info(f"Tạo giọng nói câu thành công: {self.tts_text}")
                            elif event == "task-finished":
                                logger.bind(tag=TAG).debug("Tác vụ TTS hoàn tất~")
                                self.activate_session = False
                                self._process_before_stop_play_files()
                            elif event == "task-failed":
                                error_code = header.get("error_code", "unknown")
                                error_message = header.get("error_message", "Lỗi không xác định")
                                logger.bind(tag=TAG).error(
                                    f"Tác vụ TTS thất bại: {error_code} - {error_message}"
                                )
                                break
                        except json.JSONDecodeError:
                            logger.bind(tag=TAG).warning("Đã nhận thông điệp JSON không hợp lệ")
                    elif isinstance(msg, (bytes, bytearray)):
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            msg, False, callback=self.handle_opus
                        )
                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).warning("Kết nối WebSocket đã đóng")
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(
                        f"Lỗi khi xử lý phản hồi TTS: {e}\n{traceback.format_exc()}"
                    )
                    break

            # Đóng WebSocket khi kết nối bất thường
            if self.ws:
                try:
                    await self.ws.close()
                except:
                    pass
                self.ws = None
        # Dọn dẹp tham chiếu khi tác vụ lắng nghe thoát
        finally:
            self.activate_session = False
            self._monitor_task = None

    def audio_to_opus_data_stream(
        self, audio_file_path, callback: Callable[[Any], Any] = None
    ):
        """Ghi đè phương thức lớp cha: dùng bộ mã hoá tạm độc lập để xử lý tệp âm thanh, tránh xung đột đồng thời với bộ mã hoá streaming TTS.
        Trong TTS hai luồng, tác vụ monitor nhận âm thanh TTS trên luồng event loop và mã hoá bằng self.opus_encoder,
        đồng thời tts_text_priority_thread xử lý tệp nhạc cũng dùng self.opus_encoder,
        encoder.buffer dùng chung không an toàn luồng, truy cập đồng thời sẽ khiến assert của SILK resampler thất bại.
        """
        from core.utils.util import audio_to_data_stream

        return audio_to_data_stream(
            audio_file_path,
            is_opus=True,
            callback=callback,
            sample_rate=self.conn.sample_rate,
            opus_encoder=None,
        )

    def to_tts(self, text: str) -> list:
        """Tạo dữ liệu âm thanh không streaming, dùng cho tạo âm thanh và kịch bản kiểm thử"""
        try:
            # Tạo vòng lặp sự kiện
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Tạo ID phiên
            session_id = uuid.uuid4().hex
            # Lưu trữ dữ liệu âm thanh
            audio_data = []

            async def _generate_audio():
                ws = await websockets.connect(
                    self.ws_url,
                    additional_headers=self.header,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=10,
                    max_size=10 * 1024 * 1024,
                    **self.ws_connect_options,
                )

                try:
                    # Gửi thông điệp run-task để khởi động phiên
                    run_task_message = {
                        "header": {
                            "action": "run-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {
                            "task_group": "audio",
                            "task": "tts",
                            "function": "SpeechSynthesizer",
                            "model": self.model,
                            "parameters": {
                                "text_type": "PlainText",
                                "voice": self.voice,
                                "format": self.format,
                                "sample_rate": self.conn.sample_rate,
                                "volume": self.volume,
                                "rate": self.rate,
                                "pitch": self.pitch,
                            },
                            "input": {}
                        },
                    }
                    await ws.send(json.dumps(run_task_message))

                    # Chờ tác vụ khởi động
                    task_started = False
                    while not task_started:
                        msg = await ws.recv()
                        if isinstance(msg, str):
                            data = json.loads(msg)
                            header = data.get("header", {})
                            if header.get("event") == "task-started":
                                task_started = True
                                logger.bind(tag=TAG).debug("Tác vụ TTS đã khởi động")
                            elif header.get("event") == "task-failed":
                                error_code = header.get("error_code", "unknown")
                                error_message = header.get("error_message", "Lỗi không xác định")
                                raise Exception(
                                    f"Khởi động tác vụ thất bại: {error_code} - {error_message}"
                                )

                    # Gửi văn bản
                    filtered_text = MarkdownCleaner.clean_markdown(text)
                    if self._correct_words_pattern:
                        filtered_text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], filtered_text)
                    # Gửi thông điệp continue-task
                    continue_task_message = {
                        "header": {
                            "action": "continue-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {"input": {"text": filtered_text}},
                    }
                    await ws.send(json.dumps(continue_task_message))

                    # Gửi thông điệp finish-task
                    finish_task_message = {
                        "header": {
                            "action": "finish-task",
                            "task_id": session_id,
                            "streaming": "duplex",
                        },
                        "payload": {
                            "input": {}
                        }
                    }
                    await ws.send(json.dumps(finish_task_message))

                    # Nhận dữ liệu âm thanh
                    task_finished = False
                    while not task_finished:
                        msg = await ws.recv()
                        if isinstance(msg, (bytes, bytearray)):
                            self.opus_encoder.encode_pcm_to_opus_stream(
                                msg,
                                end_of_stream=False,
                                callback=lambda opus: audio_data.append(opus)
                            )
                        elif isinstance(msg, str):
                            data = json.loads(msg)
                            header = data.get("header", {})
                            if header.get("event") == "task-finished":
                                task_finished = True
                                logger.bind(tag=TAG).debug("Tác vụ TTS hoàn tất")
                            elif header.get("event") == "task-failed":
                                error_code = header.get("error_code", "unknown")
                                error_message = header.get("error_message", "Lỗi không xác định")
                                raise Exception(
                                    f"Tổng hợp thất bại: {error_code} - {error_message}"
                                )

                finally:
                    # Dọn dẹp tài nguyên
                    try:
                        await ws.close()
                    except:
                        pass

            # Chạy tác vụ bất đồng bộ
            loop.run_until_complete(_generate_audio())
            loop.close()

            return audio_data

        except Exception as e:
            logger.bind(tag=TAG).error(f"Tạo dữ liệu âm thanh thất bại: {str(e)}")
            return []
