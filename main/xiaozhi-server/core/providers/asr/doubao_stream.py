import json
import gzip
import uuid
import asyncio
import websockets
from core.providers.asr.base import ASRProviderBase
from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config
        self.text = ""
        self.asr_ws = None
        self.forward_task = None
        self.is_processing = False  # Thêm cờ trạng thái xử lý
        self._is_stopping = False  # Thêm cờ dừng, tránh race condition

        # Tham số cấu hình
        self.appid = str(config.get("appid"))
        self.access_token = config.get("access_token")
        # ID tài nguyên, dùng để phân biệt các mô hình ASR (mặc định bản giờ mô hình 1.0, bản v2 dùng seed-asr)
        self.resource_id = config.get("resource_id", "volc.bigasr.sauc.duration")

        self.boosting_table_name = config.get("boosting_table_name", "")
        self.correct_table_name = config.get("correct_table_name", "")
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

        # Cấu hình ASR Volcano Engine
        enable_multilingual = config.get("enable_multilingual", False)
        self.enable_multilingual = (
            False if str(enable_multilingual).lower() == "false" else True
        )
        if self.enable_multilingual:
            self.ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
        else:
            self.ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
        self.uid = config.get("uid", "streaming_asr_service")
        self.workflow = config.get(
            "workflow", "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate"
        )
        self.result_type = config.get("result_type", "single")
        self.format = config.get("format", "pcm")
        self.codec = config.get("codec", "pcm")
        self.rate = config.get("sample_rate", 16000)
        # Tham số language chỉ có hiệu lực ở chế độ đa ngôn ngữ (bigmodel_nostream)
        self.language = config.get("language") if self.enable_multilingual else None
        self.bits = config.get("bits", 16)
        self.channel = config.get("channel", 1)
        self.auth_method = config.get("auth_method", "token")
        self.secret = config.get("secret", "access_secret")
        end_window_size = config.get("end_window_size")
        self.end_window_size = int(end_window_size) if end_window_size else 200

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)

    async def receive_audio(self, conn: "ConnectionHandler", pcm_frame, audio_have_voice):
        # Gọi phương thức lớp cha trước để xử lý logic cơ bản
        await super().receive_audio(conn, pcm_frame, audio_have_voice)

        # Nếu lần này có âm thanh, và trước đó chưa thiết lập kết nối
        if audio_have_voice and self.asr_ws is None and not self.is_processing:
            try:
                self.is_processing = True
                # Thiết lập kết nối WebSocket mới
                headers = self.token_auth() if self.auth_method == "token" else None
                logger.bind(tag=TAG).info(f"Đang kết nối dịch vụ ASR, headers: {headers}")

                self.asr_ws = await websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    max_size=1000000000,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=10,
                )

                # Gửi yêu cầu khởi tạo
                request_params = self.construct_request(str(uuid.uuid4()))
                try:
                    payload_bytes = str.encode(json.dumps(request_params))
                    payload_bytes = gzip.compress(payload_bytes)
                    full_client_request = self.generate_header()
                    full_client_request.extend((len(payload_bytes)).to_bytes(4, "big"))
                    full_client_request.extend(payload_bytes)

                    logger.bind(tag=TAG).info(f"Gửi yêu cầu khởi tạo: {request_params}")
                    await self.asr_ws.send(full_client_request)

                    # Chờ phản hồi khởi tạo
                    init_res = await self.asr_ws.recv()
                    result = self.parse_response(init_res)
                    logger.bind(tag=TAG).info(f"Đã nhận phản hồi khởi tạo: {result}")

                    # Kiểm tra phản hồi khởi tạo
                    if "code" in result and result["code"] != 1000:
                        error_msg = f"Khởi tạo dịch vụ ASR thất bại: {result.get('payload_msg', {}).get('error', 'Lỗi không xác định')}"
                        logger.bind(tag=TAG).error(error_msg)
                        raise Exception(error_msg)

                except Exception as e:
                    logger.bind(tag=TAG).error(f"Gửi yêu cầu khởi tạo thất bại: {str(e)}")
                    if hasattr(e, "__cause__") and e.__cause__:
                        logger.bind(tag=TAG).error(f"Nguyên nhân lỗi: {str(e.__cause__)}")
                    raise e

                # Khởi động tác vụ bất đồng bộ nhận kết quả ASR
                self.forward_task = asyncio.create_task(self._forward_asr_results(conn))

                # Gửi dữ liệu âm thanh đã đệm
                if conn.asr_audio and len(conn.asr_audio) > 0:
                    for cached_pcm in conn.asr_audio[-10:]:
                        try:
                            payload = gzip.compress(cached_pcm)
                            audio_request = bytearray(
                                self.generate_audio_default_header()
                            )
                            audio_request.extend(len(payload).to_bytes(4, "big"))
                            audio_request.extend(payload)
                            await self.asr_ws.send(audio_request)
                        except Exception as e:
                            logger.bind(tag=TAG).info(
                                f"Đã xảy ra lỗi khi gửi dữ liệu âm thanh đã đệm: {e}"
                            )

            except Exception as e:
                logger.bind(tag=TAG).error(f"Thiết lập kết nối ASR thất bại: {str(e)}")
                if hasattr(e, "__cause__") and e.__cause__:
                    logger.bind(tag=TAG).error(f"Nguyên nhân lỗi: {str(e.__cause__)}")
                if self.asr_ws:
                    await self.asr_ws.close()
                    self.asr_ws = None
                self.is_processing = False
                return

        # Gửi dữ liệu âm thanh hiện tại
        if self.asr_ws and self.is_processing and not self._is_stopping:
            try:
                payload = gzip.compress(pcm_frame)
                audio_request = bytearray(self.generate_audio_default_header())
                audio_request.extend(len(payload).to_bytes(4, "big"))
                audio_request.extend(payload)
                await self.asr_ws.send(audio_request)
            except Exception as e:
                logger.bind(tag=TAG).info(f"Đã xảy ra lỗi khi gửi dữ liệu âm thanh: {e}")

    async def _forward_asr_results(self, conn: "ConnectionHandler"):
        try:
            while self.asr_ws and not conn.stop_event.is_set():
                # Lấy dữ liệu âm thanh của kết nối hiện tại
                audio_data = conn.asr_audio
                try:
                    response = await self.asr_ws.recv()
                    result = self.parse_response(response)
                    logger.bind(tag=TAG).debug(f"Đã nhận kết quả ASR: {result}")

                    if "payload_msg" in result:
                        payload = result["payload_msg"]
                        # Kiểm tra có phải mã lỗi 1013 không (không có giọng nói hợp lệ)
                        if "code" in payload and payload["code"] == 1013:
                            # Xử lý im lặng, không ghi log lỗi
                            continue

                        if "result" in payload:
                            utterances = payload["result"].get("utterances", [])
                            # Kiểm tra trường hợp duration và văn bản trống
                            if (
                                not self.enable_multilingual  # Lưu ý: chế độ đa ngôn ngữ không trả về kết quả trung gian, cần chờ kết quả cuối cùng
                                and payload.get("audio_info", {}).get("duration", 0)
                                > 2000
                                and not utterances
                                and not payload["result"].get("text")
                                and conn.client_listen_mode != "manual"
                            ):
                                logger.bind(tag=TAG).error(f"Văn bản nhận dạng: trống")
                                self.text = ""
                                if len(audio_data) > 15:  # Đảm bảo có đủ dữ liệu âm thanh
                                    await self.handle_voice_stop(conn, audio_data)
                                break

                            # Xử lý riêng kết quả nhận dạng không có văn bản (ở chế độ thủ công có thể đã nhận dạng xong nhưng chưa nhả phím)
                            elif not payload["result"].get("text") and not utterances:
                                # Chế độ đa ngôn ngữ sẽ liên tục trả về văn bản trống cho đến khi trả về kết quả đầy đủ cuối cùng, nên cần loại trừ
                                if self.enable_multilingual:
                                    continue

                                if conn.client_listen_mode == "manual" and conn.client_voice_stop and len(audio_data) > 15:
                                    logger.bind(tag=TAG).debug("Cuối thông điệp nhận được tín hiệu dừng, kích hoạt xử lý")
                                    await self.handle_voice_stop(conn, audio_data)
                                    break

                            for utterance in utterances:
                                if utterance.get("definite", False):
                                    current_text = utterance["text"]
                                    logger.bind(tag=TAG).info(
                                        f"Nhận dạng được văn bản: {current_text}"
                                    )

                                    # Tích luỹ kết quả nhận dạng ở chế độ thủ công
                                    if conn.client_listen_mode == "manual":
                                        if self.text:
                                            self.text += current_text
                                        else:
                                            self.text = current_text

                                        # Nhận tín hiệu dừng giữa chừng khi đang nhận thông điệp
                                        if conn.client_voice_stop and len(audio_data) > 0:
                                            logger.bind(tag=TAG).debug("Nhận tín hiệu dừng giữa chừng thông điệp, kích hoạt xử lý")
                                            await self.handle_voice_stop(conn, audio_data)
                                        break
                                    else:
                                        # Ở chế độ tự động ghi đè trực tiếp
                                        self.text = current_text
                                        if len(audio_data) > 15:  # Đảm bảo có đủ dữ liệu âm thanh
                                            await self.handle_voice_stop(
                                                conn, audio_data
                                            )
                                    break
                        elif "error" in payload:
                            error_msg = payload.get("error", "Lỗi không xác định")
                            logger.bind(tag=TAG).error(f"Dịch vụ ASR trả về lỗi: {error_msg}")
                            break

                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).info("Kết nối dịch vụ ASR đã đóng")
                    self.is_processing = False
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Đã xảy ra lỗi khi xử lý kết quả ASR: {str(e)}")
                    if hasattr(e, "__cause__") and e.__cause__:
                        logger.bind(tag=TAG).error(f"Nguyên nhân lỗi: {str(e.__cause__)}")
                    self.is_processing = False
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(f"Tác vụ chuyển tiếp kết quả ASR đã xảy ra lỗi: {str(e)}")
            if hasattr(e, "__cause__") and e.__cause__:
                logger.bind(tag=TAG).error(f"Nguyên nhân lỗi: {str(e.__cause__)}")
        finally:
            if self.asr_ws:
                await self.asr_ws.close()
                self.asr_ws = None
            self.is_processing = False
            self._is_stopping = False
            # Đặt lại mọi trạng thái liên quan đến âm thanh
            conn.reset_audio_states()

    def stop_ws_connection(self):
        if self.asr_ws:
            asyncio.create_task(self.asr_ws.close())
            self.asr_ws = None
        self.is_processing = False
        self._is_stopping = False

    async def _send_stop_request(self):
        """Gửi khung âm thanh cuối cùng để thông báo máy chủ kết thúc"""
        self._is_stopping = True  # Đánh dấu trạng thái dừng trước, ngăn gửi âm thanh tiếp theo
        if self.asr_ws:
            try:
                # Gửi khung âm thanh đánh dấu kết thúc (dữ liệu trống nén gzip)
                empty_payload = gzip.compress(b"")
                last_audio_request = bytearray(
                    self.generate_last_audio_default_header()
                )
                last_audio_request.extend(len(empty_payload).to_bytes(4, "big"))
                last_audio_request.extend(empty_payload)
                await self.asr_ws.send(last_audio_request)
                logger.bind(tag=TAG).debug("Đã gửi khung âm thanh kết thúc")
            except Exception as e:
                logger.bind(tag=TAG).debug(f"Lỗi khi gửi khung âm thanh kết thúc: {e}")

    def construct_request(self, reqid):
        req = {
            "app": {
                "appid": self.appid,
                "token": self.access_token,
            },
            "user": {"uid": self.uid},
            "request": {
                "reqid": reqid,
                "workflow": self.workflow,
                "show_utterances": True,
                "result_type": self.result_type,
                "sequence": 1,
                "end_window_size": self.end_window_size,
                "corpus": {
                    "boosting_table_name": self.boosting_table_name,
                    "correct_table_name": self.correct_table_name,
                }
            },
            "audio": {
                "format": self.format,
                "codec": self.codec,
                "rate": self.rate,
                "bits": self.bits,
                "channel": self.channel,
                "sample_rate": self.rate,
            },
        }

        # Tham số language chỉ được thêm ở chế độ đa ngôn ngữ
        if self.enable_multilingual and self.language:
            req["audio"]["language"] = self.language

        logger.bind(tag=TAG).debug(
            f"Tạo tham số yêu cầu: {json.dumps(req, ensure_ascii=False)}"
        )
        return req

    def token_auth(self):
        return {
            "X-Api-App-Key": self.appid,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def generate_header(
        self,
        version=0x01,
        message_type=0x01,
        message_type_specific_flags=0x00,
        serial_method=0x01,
        compression_type=0x01,
        reserved_data=0x00,
        extension_header: bytes = b"",
    ):
        header = bytearray()
        header_size = int(len(extension_header) / 4) + 1
        header.append((version << 4) | header_size)
        header.append((message_type << 4) | message_type_specific_flags)
        header.append((serial_method << 4) | compression_type)
        header.append(reserved_data)
        header.extend(extension_header)
        return header

    def generate_audio_default_header(self):
        return self.generate_header(
            version=0x01,
            message_type=0x02,
            message_type_specific_flags=0x00,
            serial_method=0x01,
            compression_type=0x01,
        )

    def generate_last_audio_default_header(self):
        return self.generate_header(
            version=0x01,
            message_type=0x02,
            message_type_specific_flags=0x02,
            serial_method=0x01,
            compression_type=0x01,
        )

    def parse_response(self, res: bytes) -> dict:
        try:
            # Kiểm tra độ dài phản hồi
            if len(res) < 4:
                logger.bind(tag=TAG).error(f"Độ dài dữ liệu phản hồi không đủ: {len(res)}")
                return {"error": "Độ dài dữ liệu phản hồi không đủ"}

            # Lấy header thông điệp
            header = res[:4]
            message_type = header[1] >> 4

            # Nếu là phản hồi lỗi
            if message_type == 0x0F:  # SERVER_ERROR_RESPONSE
                code = int.from_bytes(res[4:8], "big", signed=False)
                msg_length = int.from_bytes(res[8:12], "big", signed=False)
                error_msg = json.loads(res[12:].decode("utf-8"))
                return {
                    "code": code,
                    "msg_length": msg_length,
                    "payload_msg": error_msg,
                }

            # Lấy dữ liệu JSON
            try:
                # Kiểm tra byte 8-11 có phải trường độ dài JSON hợp lệ không
                # Định dạng: header 4 byte + số thứ tự 4 byte + độ dài 4 byte + dữ liệu JSON
                length = int.from_bytes(res[8:12], "big")
                if length > 0 and length <= len(res) - 12:
                    # Có trường độ dài, đọc JSON với độ dài chỉ định từ byte 12
                    json_data = res[12:12 + length].decode("utf-8")
                else:
                    # Không có trường độ dài hoặc độ dài không hợp lệ, thử phân tích trực tiếp
                    json_data = res[8:].decode("utf-8")
                result = json.loads(json_data)
                logger.bind(tag=TAG).debug(f"Phân tích phản hồi JSON thành công: {result}")
                return {"payload_msg": result}
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logger.bind(tag=TAG).error(f"Phân tích JSON thất bại: {str(e)}")
                logger.bind(tag=TAG).error(f"Dữ liệu gốc: {res}")
                raise

        except Exception as e:
            logger.bind(tag=TAG).error(f"Phân tích phản hồi thất bại: {str(e)}")
            logger.bind(tag=TAG).error(f"Dữ liệu phản hồi gốc: {res.hex()}")
            raise

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        result = self.text
        self.text = ""  # Xoá text
        return result, None

    async def close(self):
        """Phương thức dọn dẹp tài nguyên"""
        if self.asr_ws:
            await self.asr_ws.close()
            self.asr_ws = None
        if self.forward_task:
            self.forward_task.cancel()
            try:
                await self.forward_task
            except asyncio.CancelledError:
                pass
            self.forward_task = None
        self.is_processing = False
