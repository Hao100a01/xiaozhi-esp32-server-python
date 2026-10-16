import os
import uuid
import json
import hmac
import hashlib
import base64
import time
import queue
import asyncio
import traceback
import websockets

from asyncio import Task
from urllib import parse
from datetime import datetime
from typing import Callable, Any
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType


TAG = __name__
logger = setup_logging()


class AccessToken:
    @staticmethod
    def _encode_text(text):
        encoded_text = parse.quote_plus(text)
        return encoded_text.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")

    @staticmethod
    def _encode_dict(dic):
        keys = dic.keys()
        dic_sorted = [(key, dic[key]) for key in sorted(keys)]
        encoded_text = parse.urlencode(dic_sorted)
        return encoded_text.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")

    @staticmethod
    def create_token(access_key_id, access_key_secret, timeout):
        parameters = {
            "AccessKeyId": access_key_id,
            "Action": "CreateToken",
            "Format": "JSON",
            "RegionId": "cn-shanghai",  # Dùng khu vực Thượng Hải để lấy token
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": str(uuid.uuid1()),
            "SignatureVersion": "1.0",
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Version": "2019-02-28",
        }

        query_string = AccessToken._encode_dict(parameters)
        string_to_sign = (
            "GET"
            + "&"
            + AccessToken._encode_text("/")
            + "&"
            + AccessToken._encode_text(query_string)
        )

        secreted_string = hmac.new(
            bytes(access_key_secret + "&", encoding="utf-8"),
            bytes(string_to_sign, encoding="utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(secreted_string)
        signature = AccessToken._encode_text(signature)

        full_url = "http://nls-meta.cn-shanghai.aliyuncs.com/?Signature=%s&%s" % (
            signature,
            query_string,
        )

        import requests

        response = requests.get(full_url, timeout=timeout)
        if response.ok:
            root_obj = response.json()
            key = "Token"
            if key in root_obj:
                token = root_obj[key]["Id"]
                expire_time = root_obj[key]["ExpireTime"]
                return token, expire_time
        return None, None


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "speech_rate", -500, 500, 0, int),
        ("ttsPitch", "pitch_rate", -500, 500, 0, int),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        # Đặt thành kiểu giao diện streaming
        self.interface_type = InterfaceType.DUAL_STREAM

        # Cấu hình cơ bản
        self.access_key_id = config.get("access_key_id")
        self.access_key_secret = config.get("access_key_secret")
        self.appkey = config.get("appkey")
        self.format = config.get("format", "pcm")
        self.report_on_last = True

        # Cấu hình giọng đọc - giọng đọc mô hình lớn CosyVoice
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice", "longxiaochun")  # Giọng đọc mặc định của CosyVoice

        # Cấu hình tham số âm thanh
        volume = config.get("volume", "50")
        self.volume = int(volume) if volume else 50

        speech_rate = config.get("speech_rate", "0")
        self.speech_rate = int(speech_rate) if speech_rate else 0

        pitch_rate = config.get("pitch_rate", "0")
        self.pitch_rate = int(pitch_rate) if pitch_rate else 0

        # Áp dụng điều chỉnh phần trăm (nếu có), ngược lại dùng cấu hình chung
        self._apply_percentage_params(config)

        # Cấu hình WebSocket
        self.host = config.get("host", "nls-gateway-cn-beijing.aliyuncs.com")
        # Nếu cấu hình địa chỉ mạng nội bộ (chứa -internal.aliyuncs.com) thì dùng giao thức ws, mặc định là wss
        if "-internal." in self.host:
            self.ws_url = f"ws://{self.host}/ws/v1"
        else:
            # Mặc định dùng giao thức wss
            self.ws_url = f"wss://{self.host}/ws/v1"
        self.ws = None
        self._monitor_task = None
        self.activate_session = False
        self.last_active_time = None

        # Thiết lập tts riêng
        self.task_id = uuid.uuid4().hex

        # Quản lý token
        if self.access_key_id and self.access_key_secret:
            self._refresh_token()
        else:
            self.token = config.get("token")
            self.expire_time = None

    def _refresh_token(self):
        """Làm mới token và ghi lại thời gian hết hạn"""
        if self.access_key_id and self.access_key_secret:
            self.token, expire_time_str = AccessToken.create_token(
                self.access_key_id, self.access_key_secret, self.tts_timeout
            )
            if not expire_time_str:
                raise ValueError("Không lấy được thời gian hết hạn hợp lệ của token")

            expire_str = str(expire_time_str).strip()

            try:
                if expire_str.isdigit():
                    expire_time = datetime.fromtimestamp(int(expire_str))
                else:
                    expire_time = datetime.strptime(expire_str, "%Y-%m-%dT%H:%M:%SZ")
                self.expire_time = expire_time.timestamp() - 60
            except Exception as e:
                raise ValueError(f"Định dạng thời gian hết hạn không hợp lệ: {expire_str}") from e
        else:
            self.expire_time = None

        if not self.token:
            raise ValueError("Không lấy được access token hợp lệ")

    def _is_token_expired(self):
        """Kiểm tra token có hết hạn không"""
        if not self.expire_time:
            return False
        return time.time() > self.expire_time

    async def _ensure_connection(self):
        """Đảm bảo kết nối WebSocket khả dụng"""
        try:
            if self._is_token_expired():
                logger.bind(tag=TAG).warning("Token đã hết hạn, đang tự động làm mới...")
                self._refresh_token()
            current_time = time.time()
            if self.ws and current_time - self.last_active_time < 10:
                # Trong 10 giây mới được tái dùng liên kết để hội thoại liên tục
                self.task_id = uuid.uuid4().hex
                logger.bind(tag=TAG).debug(f"Dùng liên kết đã có..., task_id: {self.task_id}")
                return self.ws
            logger.bind(tag=TAG).debug("Bắt đầu thiết lập kết nối mới...")

            # Huỷ tác vụ lắng nghe cũ trước khi thiết lập kết nối mới
            await self._cancel_monitor_task()

            self.ws = await websockets.connect(
                self.ws_url,
                additional_headers={"X-NLS-Token": self.token},
                ping_interval=30,
                ping_timeout=10,
                close_timeout=10,
            )
            self.task_id = uuid.uuid4().hex
            logger.bind(tag=TAG).debug(f"Thiết lập kết nối WebSocket thành công, task_id: {self.task_id}")
            self.last_active_time = time.time()
            return self.ws
        except Exception as e:
            logger.bind(tag=TAG).error(f"Thiết lập kết nối thất bại: {str(e)}")
            self.ws = None
            self.last_active_time = None
            raise

    def tts_text_priority_thread(self):
        """Luồng xử lý văn bản streaming"""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)

                if self.conn.client_abort:
                    logger.bind(tag=TAG).info("Đã nhận thông tin ngắt, dừng luồng xử lý văn bản TTS")
                    asyncio.run_coroutine_threadsafe(
                        self.finish_session(self.conn.sentence_id),
                        loop=self.conn.loop,
                    )
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
                    # Khởi tạo tham số
                    try:
                        logger.bind(tag=TAG).debug("Bắt đầu khởi động phiên TTS...")
                        future = asyncio.run_coroutine_threadsafe(
                            self.start_session(self.task_id),
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
                            self.finish_session(self.task_id),
                            loop=self.conn.loop,
                        )
                        future.result(timeout=self.tts_timeout)
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"Kết thúc phiên TTS thất bại: {str(e)}")
                        continue

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Xử lý văn bản TTS thất bại: {str(e)}, kiểu: {type(e).__name__}, ngăn xếp: {traceback.format_exc()}"
                )

    async def text_to_speak(self, text, _):
        try:
            if self.ws is None:
                logger.bind(tag=TAG).warning(f"Kết nối WebSocket không tồn tại, dừng gửi văn bản")
                return
            filtered_text = MarkdownCleaner.clean_markdown(text)

            if filtered_text:
                # Dùng khớp cửa sổ trượt để xử lý từ thay thế xuyên mảnh
                confirmed_texts, self._pending_prefix = self._match_stream_text(filtered_text)

                # Gửi từng đoạn văn bản đã xác định
                for txt in confirmed_texts:
                    if txt and self.ws:
                        run_request = {
                            "header": {
                                "message_id": uuid.uuid4().hex,
                                "task_id": self.task_id,
                                "namespace": "FlowingSpeechSynthesizer",
                                "name": "RunSynthesis",
                                "appkey": self.appkey,
                            },
                            "payload": {"text": txt},
                        }
                        await self.ws.send(json.dumps(run_request))
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

    async def start_session(self, task_id):
        logger.bind(tag=TAG).debug("Bắt đầu phiên～～")
        try:
            # Khi phiên trước đang hoạt động thì đóng kết nối trước và tạo liên kết mới
            if self.activate_session:
                await self.close()

            # Đặt cờ kích hoạt phiên
            self.activate_session = True

            # Thiết lập kết nối mới
            await self._ensure_connection()

            # Khởi động tác vụ lắng nghe
            if self._monitor_task is None or self._monitor_task.done():
                logger.bind(tag=TAG).debug("Khởi động tác vụ lắng nghe...")
                self._monitor_task = asyncio.create_task(self._start_monitor_tts_response())

            start_request = {
                "header": {
                    "message_id": uuid.uuid4().hex,
                    "task_id": self.task_id,
                    "namespace": "FlowingSpeechSynthesizer",
                    "name": "StartSynthesis",
                    "appkey": self.appkey,
                },
                "payload": {
                    "voice": self.voice,
                    "format": self.format,
                    "sample_rate": self.conn.sample_rate,
                    "volume": self.volume,
                    "speech_rate": self.speech_rate,
                    "pitch_rate": self.pitch_rate,
                    "enable_subtitle": True,
                },
            }
            await self.ws.send(json.dumps(start_request))
            self.last_active_time = time.time()
            logger.bind(tag=TAG).debug("Yêu cầu khởi động phiên đã được gửi")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Khởi động phiên thất bại: {str(e)}")
            # Đảm bảo dọn dẹp tài nguyên
            await self.close()
            raise

    async def finish_session(self, task_id):
        logger.bind(tag=TAG).debug(f"Đóng phiên～～{task_id}")
        try:
            if self.ws:
                stop_request = {
                    "header": {
                        "message_id": uuid.uuid4().hex,
                        "task_id": self.task_id,
                        "namespace": "FlowingSpeechSynthesizer",
                        "name": "StopSynthesis",
                        "appkey": self.appkey,
                    }
                }
                await self.ws.send(json.dumps(stop_request))
                logger.bind(tag=TAG).debug("Yêu cầu kết thúc phiên đã được gửi")
                self.last_active_time = time.time()

        except Exception as e:
            logger.bind(tag=TAG).error(f"Đóng phiên thất bại: {str(e)}")
            # Đảm bảo dọn dẹp tài nguyên
            await self.close()
            raise

    async def close(self):
        """Dọn dẹp tài nguyên"""
        await super().close()
        self.activate_session = False
        await self._cancel_monitor_task()

        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None
            self.last_active_time = None

    async def _start_monitor_tts_response(self):
        """Lắng nghe phản hồi TTS - chạy dài hạn"""
        try:
            while not self.conn.stop_event.is_set():
                try:
                    msg = await self.ws.recv()
                    self.last_active_time = time.time()

                    if isinstance(msg, str):  # Thông điệp điều khiển văn bản
                        try:
                            data = json.loads(msg)
                            header = data.get("header", {})
                            event_name = header.get("name")
                            task_id = header.get("task_id")

                            # Chỉ xử lý phản hồi của phiên đang hoạt động hiện tại
                            if task_id and self.task_id != task_id:
                                if event_name in ["SynthesisCompleted", "TaskFailed"]:
                                    logger.bind(tag=TAG).debug(f"Đã nhận phản hồi kết thúc downlink còn sót, đặt lại trạng thái phiên～～")
                                    self.activate_session = False
                                continue

                            if event_name == "SynthesisStarted":
                                logger.bind(tag=TAG).debug("Tổng hợp TTS đã khởi động")
                                self.tts_audio_queue.put(
                                    (SentenceType.FIRST, [], None)
                                )
                            elif event_name == "SentenceEnd":
                                # Gửi dữ liệu đã đệm
                                tts_text = self.get_tts_text(self.conn.sentence_id)
                                if tts_text:
                                    logger.bind(tag=TAG).info(
                                        f"Tạo giọng nói câu thành công: {tts_text}"
                                    )
                                    self.tts_audio_queue.put(
                                        (SentenceType.FIRST, [], tts_text)
                                    )
                                    self.clear_tts_text(self.conn.sentence_id)
                            elif event_name == "SynthesisCompleted":
                                logger.bind(tag=TAG).debug(f"Kết thúc phiên～～")
                                self.activate_session = False
                                self._process_before_stop_play_files()
                        except json.JSONDecodeError:
                            logger.bind(tag=TAG).warning("Đã nhận thông điệp JSON không hợp lệ")
                    # Thông điệp nhị phân (dữ liệu âm thanh)
                    elif isinstance(msg, (bytes, bytearray)):
                        self.opus_encoder.encode_pcm_to_opus_stream(msg, False, self.handle_opus)
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

    def to_tts(self, text: str) -> list:
        """Xử lý TTS không streaming, dùng cho kịch bản kiểm thử và lưu tệp âm thanh"""
        try:
            # Tạo vòng lặp sự kiện mới
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Lưu trữ dữ liệu âm thanh
            audio_data = []

            async def _generate_audio():
                # Làm mới token (nếu cần)
                if self._is_token_expired():
                    self._refresh_token()

                # Thiết lập kết nối WebSocket
                ws = await websockets.connect(
                    self.ws_url,
                    additional_headers={"X-NLS-Token": self.token},
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=10,
                )
                try:
                    # Gửi yêu cầu StartSynthesis
                    start_request = {
                        "header": {
                            "message_id": uuid.uuid4().hex,
                            "task_id": self.task_id,
                            "namespace": "FlowingSpeechSynthesizer",
                            "name": "StartSynthesis",
                            "appkey": self.appkey,
                        },
                        "payload": {
                            "voice": self.voice,
                            "format": self.format,
                            "sample_rate": self.conn.sample_rate,
                            "volume": self.volume,
                            "speech_rate": self.speech_rate,
                            "pitch_rate": self.pitch_rate,
                            "enable_subtitle": True,
                        },
                    }
                    await ws.send(json.dumps(start_request))

                    # Chờ phản hồi SynthesisStarted
                    synthesis_started = False
                    while not synthesis_started:
                        msg = await ws.recv()
                        if isinstance(msg, str):
                            data = json.loads(msg)
                            header = data.get("header", {})
                            if header.get("name") == "SynthesisStarted":
                                synthesis_started = True
                                logger.bind(tag=TAG).debug("Tổng hợp TTS đã khởi động")
                            elif header.get("name") == "TaskFailed":
                                error_info = data.get("payload", {}).get(
                                    "error_info", {}
                                )
                                error_code = error_info.get("error_code")
                                error_message = error_info.get(
                                    "error_message", "Lỗi không xác định"
                                )
                                raise Exception(
                                    f"Khởi động tổng hợp thất bại: {error_code} - {error_message}"
                                )

                    # Gửi yêu cầu tổng hợp văn bản
                    filtered_text = MarkdownCleaner.clean_markdown(text)
                    if self._correct_words_pattern:
                        filtered_text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], filtered_text)
                    run_request = {
                        "header": {
                            "message_id": uuid.uuid4().hex,
                            "task_id": self.task_id,
                            "namespace": "FlowingSpeechSynthesizer",
                            "name": "RunSynthesis",
                            "appkey": self.appkey,
                        },
                        "payload": {"text": filtered_text},
                    }
                    await ws.send(json.dumps(run_request))

                    # Gửi yêu cầu dừng tổng hợp
                    stop_request = {
                        "header": {
                            "message_id": uuid.uuid4().hex,
                            "task_id": self.task_id,
                            "namespace": "FlowingSpeechSynthesizer",
                            "name": "StopSynthesis",
                            "appkey": self.appkey,
                        }
                    }
                    await ws.send(json.dumps(stop_request))

                    # Nhận dữ liệu âm thanh
                    synthesis_completed = False
                    while not synthesis_completed:
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
                            event_name = header.get("name")
                            if event_name == "SynthesisCompleted":
                                synthesis_completed = True
                                logger.bind(tag=TAG).debug("Tổng hợp TTS hoàn tất")
                            elif event_name == "TaskFailed":
                                error_info = data.get("payload", {}).get(
                                    "error_info", {}
                                )
                                error_code = error_info.get("error_code")
                                error_message = error_info.get(
                                    "error_message", "Lỗi không xác định"
                                )
                                raise Exception(
                                    f"Tổng hợp thất bại: {error_code} - {error_message}"
                                )
                finally:
                    try:
                        await ws.close()
                    except:
                        pass

            loop.run_until_complete(_generate_audio())
            loop.close()

            return audio_data
        except Exception as e:
            logger.bind(tag=TAG).error(f"Tạo dữ liệu âm thanh thất bại: {str(e)}")
            return []
