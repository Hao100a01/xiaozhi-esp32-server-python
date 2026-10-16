import json
import hmac
import base64
import hashlib
import asyncio
import websockets
import gc
from time import mktime
from datetime import datetime
from urllib.parse import urlencode
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from config.logger import setup_logging
from wsgiref.handlers import format_date_time
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()

# Hằng số trạng thái khung
STATUS_FIRST_FRAME = 0  # Đánh dấu khung đầu tiên
STATUS_CONTINUE_FRAME = 1  # Đánh dấu khung giữa
STATUS_LAST_FRAME = 2  # Đánh dấu khung cuối cùng


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config
        self.text = ""
        self.asr_ws = None
        self.forward_task = None
        self.is_processing = False
        self.server_ready = False

        # Cấu hình iFlytek
        self.app_id = config.get("app_id")
        self.api_key = config.get("api_key")
        self.api_secret = config.get("api_secret")

        if not all([self.app_id, self.api_key, self.api_secret]):
            raise ValueError("Phải cung cấp app_id, api_key và api_secret")

        # Tham số nhận dạng
        self.iat_params = {
            "domain": config.get("domain", "slm"),
            "language": config.get("language", "zh_cn"),
            "accent": config.get("accent", "mandarin"),
            "result": {"encoding": "utf8", "compress": "raw", "format": "plain"},
        }

        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file

    def create_url(self) -> str:
        """Tạo URL xác thực"""
        url = "ws://iat.cn-huabei-1.xf-yun.com/v1"
        # Tạo timestamp định dạng RFC1123
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))

        # Nối chuỗi
        signature_origin = "host: " + "iat.cn-huabei-1.xf-yun.com" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v1 " + "HTTP/1.1"

        # Dùng hmac-sha256 để mã hoá
        signature_sha = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_origin.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding="utf-8")

        authorization_origin = (
            'api_key="%s", algorithm="%s", headers="%s", signature="%s"'
            % (self.api_key, "hmac-sha256", "host date request-line", signature_sha)
        )
        authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode(
            encoding="utf-8"
        )

        # Gộp các tham số xác thực của yêu cầu thành dict
        v = {
            "authorization": authorization,
            "date": date,
            "host": "iat.cn-huabei-1.xf-yun.com",
        }

        # Nối tham số xác thực, tạo url
        url = url + "?" + urlencode(v)
        return url

    async def open_audio_channels(self, conn: "ConnectionHandler"):
        await super().open_audio_channels(conn)

    async def receive_audio(self, conn: "ConnectionHandler", pcm_frame, audio_have_voice):
        # Gọi phương thức lớp cha trước để xử lý logic cơ bản
        await super().receive_audio(conn, pcm_frame, audio_have_voice)

        # Nếu lần này có âm thanh, và trước đó chưa thiết lập kết nối
        if audio_have_voice and self.asr_ws is None and not self.is_processing:
            try:
                await self._start_recognition(conn)
            except Exception as e:
                logger.bind(tag=TAG).error(f"Thiết lập kết nối ASR thất bại: {str(e)}")
                await self._cleanup()
                return

        # Gửi dữ liệu âm thanh hiện tại
        if self.asr_ws and self.is_processing and self.server_ready:
            try:
                await self._send_audio_frame(pcm_frame, STATUS_CONTINUE_FRAME)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"Đã xảy ra lỗi khi gửi dữ liệu âm thanh: {e}")
                await self._cleanup()

    async def _start_recognition(self, conn: "ConnectionHandler"):
        """Bắt đầu phiên nhận dạng"""
        try:
            self.is_processing = True
            # Thiết lập kết nối WebSocket
            ws_url = self.create_url()
            logger.bind(tag=TAG).info(f"Đang kết nối dịch vụ ASR: {ws_url[:50]}...")

            # Nếu là chế độ thủ công, đặt thời gian chờ là một phút
            if conn.client_listen_mode == "manual":
                self.iat_params["eos"] = 60000

            self.asr_ws = await websockets.connect(
                ws_url,
                max_size=1000000000,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=10,
            )

            logger.bind(tag=TAG).info("Kết nối ASR WebSocket đã được thiết lập")
            self.server_ready = False
            self.forward_task = asyncio.create_task(self._forward_results(conn))

            # Gửi khung âm thanh đầu tiên
            if conn.asr_audio and len(conn.asr_audio) > 0:
                first_pcm = conn.asr_audio[-1] if conn.asr_audio else b""
                await self._send_audio_frame(first_pcm, STATUS_FIRST_FRAME)
                self.server_ready = True
                logger.bind(tag=TAG).info("Đã gửi khung đầu tiên, bắt đầu nhận dạng")

                # Gửi dữ liệu âm thanh đã đệm
                for cached_pcm in conn.asr_audio[-10:]:
                    try:
                        await self._send_audio_frame(cached_pcm, STATUS_CONTINUE_FRAME)
                    except Exception as e:
                        logger.bind(tag=TAG).info(f"Đã xảy ra lỗi khi gửi dữ liệu âm thanh đã đệm: {e}")
                        break

        except Exception as e:
            logger.bind(tag=TAG).error(f"Thiết lập kết nối ASR thất bại: {str(e)}")
            if hasattr(e, "__cause__") and e.__cause__:
                logger.bind(tag=TAG).error(f"Nguyên nhân lỗi: {str(e.__cause__)}")
            if self.asr_ws:
                await self.asr_ws.close()
                self.asr_ws = None
            self.is_processing = False
            raise

    async def _send_audio_frame(self, audio_data: bytes, status: int):
        """Gửi khung âm thanh"""
        if not self.asr_ws:
            return

        audio_b64 = base64.b64encode(audio_data).decode("utf-8")

        frame_data = {
            "header": {"status": status, "app_id": self.app_id},
            "parameter": {"iat": self.iat_params},
            "payload": {
                "audio": {"audio": audio_b64, "sample_rate": 16000, "encoding": "raw"}
            },
        }

        await self.asr_ws.send(json.dumps(frame_data, ensure_ascii=False))

    async def _forward_results(self, conn: "ConnectionHandler"):
        """Chuyển tiếp kết quả nhận dạng"""
        try:
            while not conn.stop_event.is_set():
                try:
                    response = await asyncio.wait_for(self.asr_ws.recv(), timeout=60)
                    result = json.loads(response)
                    logger.bind(tag=TAG).debug(f"Đã nhận kết quả ASR: {result}")

                    header = result.get("header", {})
                    payload = result.get("payload", {})
                    code = header.get("code", 0)
                    status = header.get("status", 0)

                    if code != 0:
                        logger.bind(tag=TAG).error(
                            f"Lỗi nhận dạng, mã lỗi: {code}, thông điệp: {header.get('message', '')}"
                        )
                        if code in [10114, 10160]:  # Sự cố kết nối
                            break
                        continue

                    # Xử lý kết quả nhận dạng
                    if payload and "result" in payload:
                        text_data = payload["result"]["text"]
                        if text_data:
                            # Giải mã văn bản base64
                            decoded_text = base64.b64decode(text_data).decode("utf-8")
                            text_json = json.loads(decoded_text)
                            # Trích xuất nội dung văn bản
                            text_ws = text_json.get("ws", [])
                            for i in text_ws:
                                for j in i.get("cw", []):
                                    w = j.get("w", "")
                                    self.text += w

                    if status == 2:
                        logger.bind(tag=TAG).debug("Đã nhận kết quả nhận dạng cuối cùng, kích hoạt xử lý")
                        await self.handle_voice_stop(conn, conn.asr_audio)
                        break

                except asyncio.TimeoutError:
                    logger.bind(tag=TAG).error("Nhận kết quả hết thời gian chờ")
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
            # Dọn dẹp tài nguyên kết nối
            await self._cleanup()
            conn.reset_audio_states()

    async def handle_voice_stop(
        self, conn: "ConnectionHandler", asr_audio_task: List[bytes]
    ):
        """Xử lý dừng giọng nói, gửi khung cuối cùng và xử lý kết quả nhận dạng"""
        try:
            # Gửi khung cuối cùng trước để báo hiệu âm thanh kết thúc
            if self.asr_ws and self.is_processing:
                try:
                    await self._send_audio_frame(b"", STATUS_LAST_FRAME)
                    logger.bind(tag=TAG).debug(f"Đã gửi yêu cầu dừng")

                    await asyncio.sleep(0.25)
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Gửi yêu cầu dừng thất bại: {e}")

            await super().handle_voice_stop(conn, asr_audio_task)
        except Exception as e:
            logger.bind(tag=TAG).error(f"Xử lý dừng giọng nói thất bại: {e}")
            import traceback

            logger.bind(tag=TAG).debug(f"Chi tiết ngoại lệ: {traceback.format_exc()}")

    def stop_ws_connection(self):
        if self.asr_ws:
            asyncio.create_task(self.asr_ws.close())
            self.asr_ws = None
        self.is_processing = False

    async def _send_stop_request(self):
        """Gửi yêu cầu dừng nhận dạng (không đóng kết nối)"""
        if self.asr_ws:
            try:
                # Dừng gửi âm thanh trước
                self.is_processing = False
                await self._send_audio_frame(b"", STATUS_LAST_FRAME)
                logger.bind(tag=TAG).debug("Đã gửi yêu cầu dừng")
            except Exception as e:
                logger.bind(tag=TAG).error(f"Gửi yêu cầu dừng thất bại: {e}")

    async def _cleanup(self):
        """Dọn dẹp tài nguyên (đóng kết nối)"""
        logger.bind(tag=TAG).debug(
            f"Bắt đầu dọn dẹp phiên ASR | Trạng thái hiện tại: processing={self.is_processing}, server_ready={self.server_ready}"
        )

        # Đặt lại trạng thái
        self.is_processing = False
        self.server_ready = False
        logger.bind(tag=TAG).debug("Trạng thái ASR đã được đặt lại")

        # Đóng kết nối
        if self.asr_ws:
            try:
                logger.bind(tag=TAG).debug("Đang đóng kết nối WebSocket")
                await asyncio.wait_for(self.asr_ws.close(), timeout=2.0)
                logger.bind(tag=TAG).debug("Kết nối WebSocket đã đóng")
            except Exception as e:
                logger.bind(tag=TAG).error(f"Đóng kết nối WebSocket thất bại: {e}")
            finally:
                self.asr_ws = None

        # Dọn dẹp tham chiếu tác vụ
        self.forward_task = None

        logger.bind(tag=TAG).debug("Dọn dẹp phiên ASR hoàn tất")

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        """Lấy kết quả nhận dạng"""
        result = self.text
        self.text = ""
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

