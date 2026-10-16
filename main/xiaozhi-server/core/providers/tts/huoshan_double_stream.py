import os
import uuid
import json
import queue
import asyncio
import traceback
import websockets

from typing import Callable, Any
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from core.utils.tts import MarkdownCleaner, convert_percentage_to_range
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType


TAG = __name__
logger = setup_logging()

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

# Message Type:
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_RESPONSE = 0b1011
FULL_SERVER_RESPONSE = 0b1001
ERROR_INFORMATION = 0b1111

# Message Type Specific Flags
MsgTypeFlagNoSeq = 0b0000  # Non-terminal packet with no sequence
MsgTypeFlagPositiveSeq = 0b1  # Non-terminal packet with sequence > 0
MsgTypeFlagLastNoSeq = 0b10  # last packet with no sequence
MsgTypeFlagNegativeSeq = 0b11  # Payload contains event number (int32)
MsgTypeFlagWithEvent = 0b100
# Message Serialization
NO_SERIALIZATION = 0b0000
JSON = 0b0001
# Message Compression
COMPRESSION_NO = 0b0000
COMPRESSION_GZIP = 0b0001

EVENT_NONE = 0
EVENT_Start_Connection = 1

EVENT_FinishConnection = 2

EVENT_ConnectionStarted = 50  # Thiết lập kết nối thành công

EVENT_ConnectionFailed = 51  # Thiết lập kết nối thất bại (có thể không qua được xác thực quyền)

EVENT_ConnectionFinished = 52  # Kết thúc kết nối

# Sự kiện Session uplink
EVENT_StartSession = 100
EVENT_CancelSession = 101
EVENT_FinishSession = 102
# Sự kiện Session downlink
EVENT_SessionStarted = 150
EVENT_SessionCanceled = 151
EVENT_SessionFinished = 152

EVENT_SessionFailed = 153

# Sự kiện chung uplink
EVENT_TaskRequest = 200

# Sự kiện TTS downlink
EVENT_TTSSentenceStart = 350

EVENT_TTSSentenceEnd = 351

EVENT_TTSResponse = 352


class Header:
    def __init__(
        self,
        protocol_version=PROTOCOL_VERSION,
        header_size=DEFAULT_HEADER_SIZE,
        message_type: int = 0,
        message_type_specific_flags: int = 0,
        serial_method: int = NO_SERIALIZATION,
        compression_type: int = COMPRESSION_NO,
        reserved_data=0,
    ):
        self.header_size = header_size
        self.protocol_version = protocol_version
        self.message_type = message_type
        self.message_type_specific_flags = message_type_specific_flags
        self.serial_method = serial_method
        self.compression_type = compression_type
        self.reserved_data = reserved_data

    def as_bytes(self) -> bytes:
        return bytes(
            [
                (self.protocol_version << 4) | self.header_size,
                (self.message_type << 4) | self.message_type_specific_flags,
                (self.serial_method << 4) | self.compression_type,
                self.reserved_data,
            ]
        )


class Optional:
    def __init__(
        self, event: int = EVENT_NONE, sessionId: str = None, sequence: int = None
    ):
        self.event = event
        self.sessionId = sessionId
        self.errorCode: int = 0
        self.connectionId: str | None = None
        self.response_meta_json: str | None = None
        self.sequence = sequence

    # Chuyển thành chuỗi byte
    def as_bytes(self) -> bytes:
        option_bytes = bytearray()
        if self.event != EVENT_NONE:
            option_bytes.extend(self.event.to_bytes(4, "big", signed=True))
        if self.sessionId is not None:
            session_id_bytes = str.encode(self.sessionId)
            size = len(session_id_bytes).to_bytes(4, "big", signed=True)
            option_bytes.extend(size)
            option_bytes.extend(session_id_bytes)
        if self.sequence is not None:
            option_bytes.extend(self.sequence.to_bytes(4, "big", signed=True))
        return option_bytes


class Response:
    def __init__(self, header: Header, optional: Optional):
        self.optional = optional
        self.header = header
        self.payload: bytes | None = None

    def __str__(self):
        return super().__str__()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.ws = None
        self.interface_type = InterfaceType.DUAL_STREAM
        self._monitor_task = None  # Tham chiếu tác vụ lắng nghe
        self.appId = config.get("appid")
        self.access_token = config.get("access_token")
        self.cluster = config.get("cluster")
        self.resource_id = config.get("resource_id")
        self.resource_type = True if self.resource_id == "seed-tts-2.0" else False
        self.report_on_last = self.resource_type
        self.activate_session = False
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("speaker")

        # Cấu hình audio_params mặc định
        default_audio_params = {
            "speech_rate": 0,
            "loudness_rate": 0
        }

        # Cấu hình additions mặc định
        default_additions = {
            "aigc_metadata": {},
            "cache_config": {},
            "post_process": {
                "pitch": 0
            }
        }

        # Cấu hình mix_speaker mặc định
        default_mix_speaker = {}

        # Gộp cấu hình người dùng
        self.audio_params = {**default_audio_params, **config.get("audio_params", {})}
        self.additions = {**default_additions, **config.get("additions", {})}
        self.mix_speaker = {**default_mix_speaker, **config.get("mix_speaker", {})}

        # Áp dụng điều chỉnh phần trăm (nếu có), ngược lại dùng cấu hình chung
        if "ttsVolume" in config:
            self.audio_params["loudness_rate"] = int(convert_percentage_to_range(
                config["ttsVolume"], min_val=-50, max_val=100, base_val=0
            ))

        if "ttsRate" in config:
            self.audio_params["speech_rate"] = int(convert_percentage_to_range(
                config["ttsRate"], min_val=-50, max_val=100, base_val=0
            ))

        if "ttsPitch" in config:
            self.additions["post_process"]["pitch"] = int(convert_percentage_to_range(
                config["ttsPitch"], min_val=-12, max_val=12, base_val=0
            ))

        self.ws_url = config.get("ws_url")
        self.authorization = config.get("authorization")
        self.header = {"Authorization": f"{self.authorization}{self.access_token}"}
        enable_ws_reuse_value = config.get("enable_ws_reuse", True)
        self.enable_ws_reuse = False if str(enable_ws_reuse_value).lower() == 'false' else True
        self.tts_text = ""

        model_key_msg = check_model_key("TTS", self.access_token)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)

    async def open_audio_channels(self, conn):
        try:
            await super().open_audio_channels(conn)
            # Cập nhật tần số lấy mẫu trong audio_params thành conn.sample_rate thực tế
            self.audio_params["sample_rate"] = conn.sample_rate
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to open audio channels: {str(e)}")
            self.ws = None
            raise

    async def _ensure_connection(self):
        """Thiết lập kết nối WebSocket mới, và khởi động tác vụ lắng nghe (chỉ lần đầu)"""
        try:
            if self.ws:
                if self.enable_ws_reuse:
                    logger.bind(tag=TAG).debug(f"Dùng liên kết đã có...")
                    return self.ws
                else:
                    try:
                        await self.finish_connection()
                    except:
                        pass
            logger.bind(tag=TAG).debug("Bắt đầu thiết lập kết nối mới...")

            # Huỷ tác vụ lắng nghe cũ trước khi thiết lập kết nối mới
            await self._cancel_monitor_task()

            ws_header = {
                "X-Api-App-Key": self.appId,
                "X-Api-Access-Key": self.access_token,
                "X-Api-Resource-Id": self.resource_id,
                "X-Api-Connect-Id": uuid.uuid4(),
            }
            self.ws = await websockets.connect(
                self.ws_url, additional_headers=ws_header, max_size=1000000000
            )
            logger.bind(tag=TAG).debug("Thiết lập kết nối WebSocket thành công")
            
            # Sau khi thiết lập kết nối thành công, khởi động tác vụ lắng nghe
            if self._monitor_task is None or self._monitor_task.done():
                logger.bind(tag=TAG).debug("Khởi động tác vụ lắng nghe...")
                self._monitor_task = asyncio.create_task(self._start_monitor_tts_response())
            
            return self.ws
        except Exception as e:
            logger.bind(tag=TAG).error(f"Thiết lập kết nối thất bại: {str(e)}")
            self.ws = None
            raise
    
    async def finish_connection(self):
        """Gửi sự kiện FinishConnection, chờ máy chủ trả về EVENT_ConnectionFinished"""
        try:
            if self.ws:
                logger.bind(tag=TAG).debug("Bắt đầu đóng kết nối...")
                header = Header(
                    message_type=FULL_CLIENT_REQUEST,
                    message_type_specific_flags=MsgTypeFlagWithEvent,
                    serial_method=JSON,
                ).as_bytes()
                optional = Optional(event=EVENT_FinishConnection).as_bytes()
                payload = str.encode("{}")
                await self.send_event(self.ws, header, optional, payload)
        except:
            pass

    def tts_text_priority_thread(self):
        """Luồng xử lý văn bản TTS hai luồng của Volcano Engine"""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)

                if self.conn.client_abort:
                    try:
                        logger.bind(tag=TAG).info("Đã nhận thông tin ngắt, dừng luồng xử lý văn bản TTS")
                        if self.enable_ws_reuse:
                            asyncio.run_coroutine_threadsafe(
                                self.cancel_session(self.conn.sentence_id),
                                loop=self.conn.loop,
                            )
                        else:
                            asyncio.run_coroutine_threadsafe(
                                self.finish_connection(),
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
                    # Khởi tạo tham số
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
                continue

    async def text_to_speak(self, text, _):
        """Gửi văn bản tới dịch vụ TTS"""
        try:
            # Thiết lập kết nối mới
            if self.ws is None:
                logger.bind(tag=TAG).warning(f"Kết nối WebSocket không tồn tại, dừng gửi văn bản")
                return

            # Lọc Markdown
            filtered_text = MarkdownCleaner.clean_markdown(text)

            if filtered_text:
                # Dùng khớp cửa sổ trượt để xử lý từ thay thế xuyên mảnh
                confirmed_texts, self._pending_prefix = self._match_stream_text(filtered_text)

                # Gửi từng đoạn văn bản đã xác định
                for txt in confirmed_texts:
                    if txt and self.ws:
                        await self.send_text(self.voice, txt, self.conn.sentence_id)

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
        logger.bind(tag=TAG).debug(f"Bắt đầu phiên～～{session_id}")
        try:       
            # Khi phiên trước đang hoạt động thì đóng kết nối trước và tạo liên kết mới
            if self.activate_session:
                await self.close()
            
            # Đặt cờ kích hoạt phiên
            self.activate_session = True
            
            # Đảm bảo kết nối được thiết lập
            await self._ensure_connection()

            header = Header(
                message_type=FULL_CLIENT_REQUEST,
                message_type_specific_flags=MsgTypeFlagWithEvent,
                serial_method=JSON,
            ).as_bytes()
            optional = Optional(
                event=EVENT_StartSession, sessionId=session_id
            ).as_bytes()
            payload = self.get_payload_bytes(
                event=EVENT_StartSession, speaker=self.voice
            )
            await self.send_event(self.ws, header, optional, payload)
            logger.bind(tag=TAG).debug("Yêu cầu khởi động phiên đã được gửi")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Khởi động phiên thất bại: {str(e)}")
            # Đảm bảo dọn dẹp tài nguyên
            await self.close()
            raise

    async def finish_session(self, session_id):
        logger.bind(tag=TAG).debug(f"Đóng phiên～～{session_id}")
        try:
            if self.ws:
                header = Header(
                    message_type=FULL_CLIENT_REQUEST,
                    message_type_specific_flags=MsgTypeFlagWithEvent,
                    serial_method=JSON,
                ).as_bytes()
                optional = Optional(
                    event=EVENT_FinishSession, sessionId=session_id
                ).as_bytes()
                payload = str.encode("{}")
                await self.send_event(self.ws, header, optional, payload)
                logger.bind(tag=TAG).debug("Yêu cầu kết thúc phiên đã được gửi")

        except Exception as e:
            logger.bind(tag=TAG).error(f"Đóng phiên thất bại: {str(e)}")
            # Đảm bảo dọn dẹp tài nguyên
            await self.close()
            raise

    async def cancel_session(self,session_id):
        logger.bind(tag=TAG).debug(f"Huỷ phiên, giải phóng tài nguyên máy chủ～～{session_id}")
        try:
            if self.ws:
                header = Header(
                    message_type=FULL_CLIENT_REQUEST,
                    message_type_specific_flags=MsgTypeFlagWithEvent,
                    serial_method=JSON,
                ).as_bytes()
                optional = Optional(
                    event=EVENT_CancelSession, sessionId=session_id
                ).as_bytes()
                payload = str.encode("{}")
                await self.send_event(self.ws, header, optional, payload)
                logger.bind(tag=TAG).debug("Yêu cầu huỷ phiên đã được gửi")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Huỷ phiên thất bại: {str(e)}")
            # Đảm bảo dọn dẹp tài nguyên
            await self.close()
            raise

    async def close(self):
        """Phương thức dọn dẹp tài nguyên"""
        await super().close()
        self.activate_session = False
        await self._cancel_monitor_task()

        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None

    async def _start_monitor_tts_response(self):
        """Lắng nghe phản hồi TTS - chạy dài hạn"""
        try:
            while not self.conn.stop_event.is_set():
                try:
                    # Đảm bảo `recv()` chạy trên cùng event loop
                    msg = await self.ws.recv()
                    res = self.parser_response(msg)
                    self.print_response(res, "send_text res:")

                    # Ưu tiên xử lý sự kiện cấp kết nối
                    if res.optional.event == EVENT_ConnectionFinished:
                        logger.bind(tag=TAG).debug(f"Đóng liên kết thành công～～")
                        break

                    # Chỉ xử lý phản hồi của phiên đang hoạt động hiện tại
                    if res.optional.sessionId and self.conn.sentence_id != res.optional.sessionId:
                        # Nếu là sự kiện liên quan kết thúc phiên, dù ID phiên không khớp cũng phải đặt lại trạng thái
                        if res.optional.event in [EVENT_SessionCanceled, EVENT_SessionFailed, EVENT_SessionFinished]:
                            logger.bind(tag=TAG).debug(f"Đã nhận phản hồi kết thúc downlink còn sót, đặt lại trạng thái phiên～～")
                            self.activate_session = False
                        continue

                    if res.optional.event == EVENT_SessionCanceled:
                        logger.bind(tag=TAG).debug(f"Giải phóng tài nguyên máy chủ thành công～～")
                        self.activate_session = False
                    elif not self.resource_type and res.optional.event == EVENT_TTSSentenceStart:
                        json_data = json.loads(res.payload.decode("utf-8"))
                        self.tts_text = json_data.get("text", "")
                        logger.bind(tag=TAG).debug(f"Bắt đầu tạo giọng nói câu: {self.tts_text}")
                        # Khôi phục văn bản đã thay thế do dịch vụ TTS trả về thành văn bản gốc, dùng để hiển thị phụ đề
                        display_text = self._restore_original_text(self.tts_text)
                        self.tts_audio_queue.put((SentenceType.FIRST, [], display_text))
                    elif (
                        res.optional.event == EVENT_TTSResponse
                        and res.header.message_type == AUDIO_ONLY_RESPONSE
                    ):
                        # Xử lý phụ đề văn bản seed-tts-2.0
                        if self.resource_type:
                            tts_text = self.get_tts_text(self.conn.sentence_id)
                            if tts_text:
                                logger.bind(tag=TAG).info(
                                    f"Tạo giọng nói câu thành công: {tts_text}"
                                )
                                self.tts_audio_queue.put(
                                    (SentenceType.FIRST, [], tts_text)
                                )
                                self.clear_tts_text(self.conn.sentence_id)
                        self.wav_to_opus_data_audio_raw_stream(res.payload, callback=self.handle_opus)
                    elif not self.resource_type and res.optional.event == EVENT_TTSSentenceEnd:
                        logger.bind(tag=TAG).info(f"Tạo giọng nói câu thành công: {self.tts_text}")
                    elif res.optional.event == EVENT_SessionFinished:
                        logger.bind(tag=TAG).debug(f"Kết thúc phiên～～")
                        self.activate_session = False
                        self._process_before_stop_play_files()
                        # Ở chế độ không tái dùng, sau khi phiên kết thúc gửi FinishConnection
                        if not self.enable_ws_reuse:
                            await self.finish_connection()
                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).warning("Kết nối WebSocket đã đóng")
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(
                        f"Error in _start_monitor_tts_response: {e}"
                    )
                    traceback.print_exc()
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

    async def send_event(
        self,
        ws: websockets.WebSocketClientProtocol,
        header: bytes,
        optional: bytes | None = None,
        payload: bytes = None,
    ):
        try:
            full_client_request = bytearray(header)
            if optional is not None:
                full_client_request.extend(optional)
            if payload is not None:
                payload_size = len(payload).to_bytes(4, "big", signed=True)
                full_client_request.extend(payload_size)
                full_client_request.extend(payload)
            await ws.send(full_client_request)
        except websockets.ConnectionClosed:
            logger.bind(tag=TAG).error(f"ConnectionClosed")
            raise

    async def send_text(self, speaker: str, text: str, session_id):
        header = Header(
            message_type=FULL_CLIENT_REQUEST,
            message_type_specific_flags=MsgTypeFlagWithEvent,
            serial_method=JSON,
        ).as_bytes()
        optional = Optional(event=EVENT_TaskRequest, sessionId=session_id).as_bytes()
        payload = self.get_payload_bytes(
            event=EVENT_TaskRequest, text=text, speaker=speaker
        )
        return await self.send_event(self.ws, header, optional, payload)

    # Đọc nội dung chuỗi của đoạn nào đó trong mảng res
    def read_res_content(self, res: bytes, offset: int):
        content_size = int.from_bytes(res[offset : offset + 4], "big", signed=True)
        offset += 4
        content = res[offset : offset + content_size].decode('utf-8')
        offset += content_size
        return content, offset

    # Đọc payload
    def read_res_payload(self, res: bytes, offset: int):
        payload_size = int.from_bytes(res[offset : offset + 4], "big", signed=True)
        offset += 4
        payload = res[offset : offset + payload_size]
        offset += payload_size
        return payload, offset

    def parser_response(self, res) -> Response:
        if isinstance(res, str):
            raise RuntimeError(res)
        response = Response(Header(), Optional())
        # Phân tích kết quả
        # header
        header = response.header
        num = 0b00001111
        header.protocol_version = res[0] >> 4 & num
        header.header_size = res[0] & 0x0F
        header.message_type = (res[1] >> 4) & num
        header.message_type_specific_flags = res[1] & 0x0F
        header.serialization_method = res[2] >> num
        header.message_compression = res[2] & 0x0F
        header.reserved = res[3]
        #
        offset = 4
        optional = response.optional
        if header.message_type == FULL_SERVER_RESPONSE or AUDIO_ONLY_RESPONSE:
            # read event
            if header.message_type_specific_flags == MsgTypeFlagWithEvent:
                optional.event = int.from_bytes(res[offset:8], "big", signed=True)
                offset += 4
                if optional.event == EVENT_NONE:
                    return response
                # read connectionId
                elif optional.event == EVENT_ConnectionStarted:
                    optional.connectionId, offset = self.read_res_content(res, offset)
                elif optional.event == EVENT_ConnectionFailed:
                    optional.response_meta_json, offset = self.read_res_content(
                        res, offset
                    )
                elif (
                    optional.event == EVENT_SessionStarted
                    or optional.event == EVENT_SessionFailed
                    or optional.event == EVENT_SessionFinished
                ):
                    optional.sessionId, offset = self.read_res_content(res, offset)
                    optional.response_meta_json, offset = self.read_res_content(
                        res, offset
                    )
                else:
                    optional.sessionId, offset = self.read_res_content(res, offset)
                    response.payload, offset = self.read_res_payload(res, offset)

        elif header.message_type == ERROR_INFORMATION:
            optional.errorCode = int.from_bytes(
                res[offset : offset + 4], "big", signed=True
            )
            offset += 4
            response.payload, offset = self.read_res_payload(res, offset)
        return response

    async def start_connection(self):
        header = Header(
            message_type=FULL_CLIENT_REQUEST,
            message_type_specific_flags=MsgTypeFlagWithEvent,
        ).as_bytes()
        optional = Optional(event=EVENT_Start_Connection).as_bytes()
        payload = str.encode("{}")
        return await self.send_event(self.ws, header, optional, payload)

    def print_response(self, res, tag_msg: str):
        logger.bind(tag=TAG).debug(f"===>{tag_msg} header:{res.header.__dict__}")
        logger.bind(tag=TAG).debug(f"===>{tag_msg} optional:{res.optional.__dict__}")

    def get_payload_bytes(
        self,
        uid="1234",
        event=EVENT_NONE,
        text="",
        speaker="",
        audio_format="pcm",
    ):
        # Tạo req_params
        req_params = {
            "text": text,
            "speaker": speaker,
            "audio_params": {**self.audio_params, "format": audio_format},
            "additions": json.dumps(self.additions)
        }
        
        # Nếu có cấu hình mix_speaker, thêm vào req_params
        if self.mix_speaker:
            req_params["mix_speaker"] = self.mix_speaker

        return str.encode(
            json.dumps(
                {
                    "user": {"uid": uid},
                    "event": event,
                    "namespace": "BidirectionalTTS",
                    "req_params": req_params
                }
            )
        )

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
            audio_file_path, is_opus=True, callback=callback,
            sample_rate=self.conn.sample_rate, opus_encoder=None
        )

    def wav_to_opus_data_audio_raw_stream(self, raw_data_var, is_end=False, callback: Callable[[Any], Any]=None):
        return self.opus_encoder.encode_pcm_to_opus_stream(raw_data_var, is_end, callback=callback)

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
        """Tạo dữ liệu âm thanh không streaming, dùng cho tạo âm thanh và kịch bản kiểm thử
        Args:
            text: Văn bản cần chuyển
        Returns:
            list: Danh sách dữ liệu âm thanh
        """
        try:
            # Tạo vòng lặp sự kiện
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Tạo ID phiên
            session_id = uuid.uuid4().__str__().replace("-", "")

            # Lưu trữ dữ liệu âm thanh
            audio_data = []

            async def _generate_audio():
                # Tạo kết nối WebSocket mới
                ws_header = {
                    "X-Api-App-Key": self.appId,
                    "X-Api-Access-Key": self.access_token,
                    "X-Api-Resource-Id": self.resource_id,
                    "X-Api-Connect-Id": uuid.uuid4(),
                }
                ws = await websockets.connect(
                    self.ws_url, additional_headers=ws_header, max_size=1000000000
                )

                try:
                    # Khởi động phiên
                    header = Header(
                        message_type=FULL_CLIENT_REQUEST,
                        message_type_specific_flags=MsgTypeFlagWithEvent,
                        serial_method=JSON,
                    ).as_bytes()
                    optional = Optional(
                        event=EVENT_StartSession, sessionId=session_id
                    ).as_bytes()
                    payload = self.get_payload_bytes(
                        event=EVENT_StartSession, speaker=self.voice
                    )
                    await self.send_event(ws, header, optional, payload)

                    # Gửi văn bản
                    header = Header(
                        message_type=FULL_CLIENT_REQUEST,
                        message_type_specific_flags=MsgTypeFlagWithEvent,
                        serial_method=JSON,
                    ).as_bytes()
                    optional = Optional(
                        event=EVENT_TaskRequest, sessionId=session_id
                    ).as_bytes()
                    payload = self.get_payload_bytes(
                        event=EVENT_TaskRequest, text=text, speaker=self.voice
                    )
                    await self.send_event(ws, header, optional, payload)

                    # Gửi yêu cầu kết thúc phiên
                    header = Header(
                        message_type=FULL_CLIENT_REQUEST,
                        message_type_specific_flags=MsgTypeFlagWithEvent,
                        serial_method=JSON,
                    ).as_bytes()
                    optional = Optional(
                        event=EVENT_FinishSession, sessionId=session_id
                    ).as_bytes()
                    payload = str.encode("{}")
                    await self.send_event(ws, header, optional, payload)

                    # Nhận dữ liệu âm thanh
                    while True:
                        msg = await ws.recv()
                        res = self.parser_response(msg)

                        if (
                            res.optional.event == EVENT_TTSResponse
                            and res.header.message_type == AUDIO_ONLY_RESPONSE
                        ):
                            self.wav_to_opus_data_audio_raw_stream(res.payload, callback=lambda opus_frame: audio_data.append(opus_frame))
                        elif res.optional.event == EVENT_SessionFinished:
                            break

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
