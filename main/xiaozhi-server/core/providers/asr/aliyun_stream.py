import json
import time
import uuid
import hmac
import base64
import hashlib
import asyncio
import requests
import websockets
from urllib import parse
from datetime import datetime
from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

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
    def create_token(access_key_id, access_key_secret):
        parameters = {
            "AccessKeyId": access_key_id,
            "Action": "CreateToken",
            "Format": "JSON",
            "RegionId": "cn-shanghai",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": str(uuid.uuid1()),
            "SignatureVersion": "1.0",
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Version": "2019-02-28",
        }
        query_string = AccessToken._encode_dict(parameters)
        string_to_sign = (
            "GET" + "&" + AccessToken._encode_text("/") + "&" + AccessToken._encode_text(query_string)
        )
        secreted_string = hmac.new(
            bytes(access_key_secret + "&", encoding="utf-8"),
            bytes(string_to_sign, encoding="utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(secreted_string)
        signature = AccessToken._encode_text(signature)
        full_url = "http://nls-meta.cn-shanghai.aliyuncs.com/?Signature=%s&%s" % (signature, query_string)
        response = requests.get(full_url)
        if response.ok:
            root_obj = response.json()
            if "Token" in root_obj:
                return root_obj["Token"]["Id"], root_obj["Token"]["ExpireTime"]
        return None, None


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config
        self.text = ""
        self.asr_ws = None
        self.forward_task = None
        self.is_processing = False
        self.server_ready = False  # Trạng thái sẵn sàng của máy chủ

        # Cấu hình cơ bản
        self.access_key_id = config.get("access_key_id")
        self.access_key_secret = config.get("access_key_secret")
        self.appkey = config.get("appkey")
        self.token = config.get("token")
        self.host = config.get("host", "nls-gateway-cn-shanghai.aliyuncs.com")
        # Nếu cấu hình địa chỉ mạng nội bộ (chứa -internal.aliyuncs.com) thì dùng giao thức ws, mặc định là wss
        if "-internal." in self.host:
            self.ws_url = f"ws://{self.host}/ws/v1"
        else:
            # Mặc định dùng giao thức wss
            self.ws_url = f"wss://{self.host}/ws/v1"

        self.max_sentence_silence = config.get("max_sentence_silence")
        self.output_dir = config.get("output_dir", "./audio_output")
        self.delete_audio_file = delete_audio_file
        self.expire_time = None

        self.task_id = uuid.uuid4().hex

        # Quản lý token
        if self.access_key_id and self.access_key_secret:
            self._refresh_token()
        elif not self.token:
            raise ValueError("Phải cung cấp access_key_id+access_key_secret hoặc cung cấp trực tiếp token")

    def _refresh_token(self):
        """Làm mới token"""
        self.token, expire_time_str = AccessToken.create_token(self.access_key_id, self.access_key_secret)
        if not self.token:
            raise ValueError("Không lấy được access token hợp lệ")
        
        try:
            expire_str = str(expire_time_str).strip()
            if expire_str.isdigit():
                expire_time = datetime.fromtimestamp(int(expire_str))
            else:
                expire_time = datetime.strptime(expire_str, "%Y-%m-%dT%H:%M:%SZ")
            self.expire_time = expire_time.timestamp() - 60
        except:
            self.expire_time = None

    def _is_token_expired(self):
        """Kiểm tra token có hết hạn không"""
        return self.expire_time and time.time() > self.expire_time

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        # Gọi phương thức lớp cha trước để xử lý logic cơ bản
        await super().receive_audio(conn, pcm_frame, audio_have_voice)

        # Chỉ thiết lập kết nối khi có âm thanh và chưa có kết nối (loại trừ trường hợp đang dừng)
        if audio_have_voice and not self.is_processing and not self.asr_ws:
            try:
                await self._start_recognition(conn)
            except Exception as e:
                logger.bind(tag=TAG).error(f"Bắt đầu nhận dạng thất bại: {str(e)}")
                await self._cleanup()
                return

        if self.asr_ws and self.is_processing and self.server_ready:
            try:
                await self.asr_ws.send(pcm_frame)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"Gửi âm thanh thất bại: {str(e)}")
                await self._cleanup()

    async def _start_recognition(self, conn: "ConnectionHandler"):
        """Bắt đầu phiên nhận dạng"""
        if self._is_token_expired():
            self._refresh_token()
        
        # Thiết lập kết nối
        headers = {"X-NLS-Token": self.token}
        self.asr_ws = await websockets.connect(
            self.ws_url,
            additional_headers=headers,
            max_size=1000000000,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
        )

        self.task_id = uuid.uuid4().hex

        logger.bind(tag=TAG).debug(f"Thiết lập kết nối WebSocket thành công, task_id: {self.task_id}")

        self.is_processing = True
        self.server_ready = False  # Đặt lại trạng thái sẵn sàng của máy chủ
        self.forward_task = asyncio.create_task(self._forward_results(conn))

        # Gửi yêu cầu bắt đầu
        start_request = {
            "header": {
                "namespace": "SpeechTranscriber",
                "name": "StartTranscription",
                "message_id": uuid.uuid4().hex,
                "task_id": self.task_id,
                "appkey": self.appkey
            },
            "payload": {
                "format": "pcm",
                "sample_rate": 16000,
                "enable_intermediate_result": True,
                "enable_punctuation_prediction": True,
                "enable_inverse_text_normalization": True,
                "max_sentence_silence": self.max_sentence_silence,
                "enable_voice_detection": False,
            }
        }
        await self.asr_ws.send(json.dumps(start_request, ensure_ascii=False))
        logger.bind(tag=TAG).debug("Đã gửi yêu cầu bắt đầu, đang chờ máy chủ sẵn sàng...")

    async def _forward_results(self, conn: "ConnectionHandler"):
        """Chuyển tiếp kết quả nhận dạng"""
        try:
            while not conn.stop_event.is_set():
                # Lấy dữ liệu âm thanh của kết nối hiện tại
                audio_data = conn.asr_audio
                try:
                    response = await self.asr_ws.recv()
                    result = json.loads(response)

                    header = result.get("header", {})
                    payload = result.get("payload", {})
                    message_name = header.get("name", "")
                    status = header.get("status", 0)

                    if status != 20000000:
                        if status == 40010004:
                            logger.bind(tag=TAG).warning(f"Vui lòng đóng liên kết sau khi máy chủ phản hồi xong, mã trạng thái: {status}")
                            break
                        if status in [40000004, 40010003]:  # Hết thời gian chờ kết nối hoặc máy khách ngắt kết nối
                            logger.bind(tag=TAG).warning(f"Sự cố kết nối, mã trạng thái: {status}")
                            break
                        elif status in [40270002, 40270003]:  # Sự cố âm thanh
                            logger.bind(tag=TAG).warning(f"Sự cố xử lý âm thanh, mã trạng thái: {status}")
                            continue
                        else:
                            logger.bind(tag=TAG).error(f"Lỗi nhận dạng, mã trạng thái: {status}, thông điệp: {header.get('status_text', '')}")
                            continue

                    # Nhận được TranscriptionStarted nghĩa là máy chủ đã sẵn sàng nhận dữ liệu âm thanh
                    if message_name == "TranscriptionStarted":
                        self.server_ready = True
                        logger.bind(tag=TAG).debug("Máy chủ đã sẵn sàng, bắt đầu gửi âm thanh đã đệm...")

                        # Gửi âm thanh đã đệm
                        if conn.asr_audio:
                            for cached_pcm in conn.asr_audio[-10:]:
                                try:
                                    await self.asr_ws.send(cached_pcm)
                                except Exception as e:
                                    logger.bind(tag=TAG).warning(f"Gửi âm thanh đã đệm thất bại: {e}")
                                    break
                        continue
                    elif message_name == "SentenceEnd":
                        # Kết thúc câu (mỗi câu đều kích hoạt)
                        text = payload.get("result", "")
                        if text:
                            logger.bind(tag=TAG).info(f"Nhận dạng được văn bản: {text}")

                            # Tích luỹ kết quả nhận dạng ở chế độ thủ công
                            if conn.client_listen_mode == "manual":
                                if self.text:
                                    self.text += text
                                else:
                                    self.text = text

                                # Ở chế độ thủ công, chỉ kích hoạt xử lý sau khi nhận tín hiệu stop (chỉ xử lý một lần)
                                if conn.client_voice_stop:
                                    logger.bind(tag=TAG).debug("Đã nhận kết quả nhận dạng cuối cùng, kích hoạt xử lý")
                                    await self.handle_voice_stop(conn, audio_data)
                                    break
                            else:
                                # Ở chế độ tự động ghi đè trực tiếp
                                self.text = text
                                await self.handle_voice_stop(conn, audio_data)
                                break

                except asyncio.TimeoutError:
                    logger.bind(tag=TAG).error("Nhận kết quả hết thời gian chờ")
                    break
                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).info("Kết nối dịch vụ ASR đã đóng")
                    self.is_processing = False
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Xử lý kết quả thất bại: {str(e)}")
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(f"Chuyển tiếp kết quả thất bại: {str(e)}")
        finally:
            # Dọn dẹp bộ đệm âm thanh của kết nối
            await self._cleanup()
            conn.reset_audio_states()

    async def _send_stop_request(self):
        """Gửi yêu cầu dừng nhận dạng (không đóng kết nối)"""
        if self.asr_ws:
            try:
                # Dừng gửi âm thanh trước
                self.is_processing = False

                stop_msg = {
                    "header": {
                        "namespace": "SpeechTranscriber",
                        "name": "StopTranscription",
                        "message_id": uuid.uuid4().hex,
                        "task_id": self.task_id,
                        "appkey": self.appkey
                    }
                }
                logger.bind(tag=TAG).debug("Yêu cầu dừng nhận dạng đã được gửi")
                await self.asr_ws.send(json.dumps(stop_msg, ensure_ascii=False))
            except Exception as e:
                logger.bind(tag=TAG).error(f"Gửi yêu cầu dừng nhận dạng thất bại: {e}")

    async def _cleanup(self):
        """Dọn dẹp tài nguyên (đóng kết nối)"""
        logger.bind(tag=TAG).debug(f"Bắt đầu dọn dẹp phiên ASR | Trạng thái hiện tại: processing={self.is_processing}, server_ready={self.server_ready}")

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
        """Đóng tài nguyên"""
        await self._cleanup()
