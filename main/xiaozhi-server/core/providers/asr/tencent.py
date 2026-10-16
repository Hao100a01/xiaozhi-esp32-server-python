import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
import os
from typing import Optional, Tuple, List
from core.providers.asr.dto.dto import InterfaceType
import requests
from core.providers.asr.base import ASRProviderBase
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    API_URL = "https://asr.tencentcloudapi.com"
    API_VERSION = "2019-06-14"
    FORMAT = "pcm"  # Định dạng âm thanh được hỗ trợ: pcm, wav, mp3

    def __init__(self, config: dict, delete_audio_file: bool = True):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        self.output_dir = config.get("output_dir")
        self.delete_audio_file = delete_audio_file

        # Đảm bảo thư mục đầu ra tồn tại
        os.makedirs(self.output_dir, exist_ok=True)

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Chuyển dữ liệu giọng nói thành văn bản"""
        if not opus_data:
            logger.bind(tag=TAG).warning("Dữ liệu âm thanh trống!")
            return None, None

        try:
            # Kiểm tra cấu hình đã được thiết lập chưa
            if not self.secret_id or not self.secret_key:
                logger.bind(tag=TAG).error("Cấu hình nhận dạng giọng nói Tencent Cloud chưa được thiết lập, không thể nhận dạng")
                return None, None

            if artifacts is None:
                return "", None

            # Chuyển dữ liệu âm thanh sang mã hoá Base64
            base64_audio = base64.b64encode(artifacts.pcm_bytes).decode("utf-8")

            # Tạo body yêu cầu
            request_body = self._build_request_body(base64_audio)

            # Lấy header xác thực
            timestamp, authorization = self._get_auth_headers(request_body)

            # Gửi yêu cầu
            start_time = time.time()
            result = self._send_request(request_body, timestamp, authorization)

            if result:
                logger.bind(tag=TAG).debug(
                    f"Nhận dạng giọng nói Tencent Cloud mất: {time.time() - start_time:.3f}s | Kết quả: {result}"
                )

            return result, artifacts.file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"Đã xảy ra lỗi khi xử lý âm thanh! {e}", exc_info=True)
            return None, None

    def _build_request_body(self, base64_audio: str) -> str:
        """Tạo body yêu cầu"""
        request_map = {
            "ProjectId": 0,
            "SubServiceType": 2,  # Nhận dạng một câu
            "EngSerViceType": "16k_zh",  # Tiếng Trung phổ thông chung
            "SourceType": 1,  # Nguồn dữ liệu âm thanh là tệp giọng nói
            "VoiceFormat": self.FORMAT,  # Định dạng âm thanh
            "Data": base64_audio,  # Dữ liệu âm thanh mã hoá Base64
            "DataLen": len(base64_audio),  # Độ dài dữ liệu
        }
        return json.dumps(request_map)

    def _get_auth_headers(self, request_body: str) -> Tuple[str, str]:
        """Lấy header xác thực"""
        try:
            # Lấy timestamp UTC hiện tại
            now = datetime.now(timezone.utc)
            timestamp = str(int(now.timestamp()))
            date = now.strftime("%Y-%m-%d")

            # Tên dịch vụ phải là "asr"
            service = "asr"

            # Nối phạm vi credential
            credential_scope = f"{date}/{service}/tc3_request"

            # Dùng phương thức ký TC3-HMAC-SHA256
            algorithm = "TC3-HMAC-SHA256"

            # Tạo chuỗi request chuẩn
            http_request_method = "POST"
            canonical_uri = "/"
            canonical_query_string = ""

            # Lưu ý: thông tin header cần sắp xếp tăng dần theo ASCII, và cả key lẫn value đều chuyển thành chữ thường
            # Phải chứa header content-type và host
            content_type = "application/json; charset=utf-8"
            host = "asr.tencentcloudapi.com"
            action = "SentenceRecognition"  # Tên giao diện

            # Tạo thông tin header chuẩn, chú ý thứ tự và định dạng
            canonical_headers = (
                f"content-type:{content_type.lower()}\n"
                + f"host:{host.lower()}\n"
                + f"x-tc-action:{action.lower()}\n"
            )

            signed_headers = "content-type;host;x-tc-action"

            # Giá trị hash của body yêu cầu
            payload_hash = self._sha256_hex(request_body)

            # Tạo chuỗi request chuẩn
            canonical_request = (
                f"{http_request_method}\n"
                + f"{canonical_uri}\n"
                + f"{canonical_query_string}\n"
                + f"{canonical_headers}\n"
                + f"{signed_headers}\n"
                + f"{payload_hash}"
            )

            # Tính giá trị hash của request chuẩn
            hashed_canonical_request = self._sha256_hex(canonical_request)

            # Tạo chuỗi cần ký
            string_to_sign = (
                f"{algorithm}\n"
                + f"{timestamp}\n"
                + f"{credential_scope}\n"
                + f"{hashed_canonical_request}"
            )

            # Tính khoá ký
            secret_date = self._hmac_sha256(f"TC3{self.secret_key}", date)
            secret_service = self._hmac_sha256(secret_date, service)
            secret_signing = self._hmac_sha256(secret_service, "tc3_request")

            # Tính chữ ký
            signature = self._bytes_to_hex(
                self._hmac_sha256(secret_signing, string_to_sign)
            )

            # Tạo header uỷ quyền
            authorization = (
                f"{algorithm} "
                + f"Credential={self.secret_id}/{credential_scope}, "
                + f"SignedHeaders={signed_headers}, "
                + f"Signature={signature}"
            )

            return timestamp, authorization

        except Exception as e:
            logger.bind(tag=TAG).error(f"Tạo header xác thực thất bại: {e}", exc_info=True)
            raise RuntimeError(f"Tạo header xác thực thất bại: {e}")

    def _send_request(
        self, request_body: str, timestamp: str, authorization: str
    ) -> Optional[str]:
        """Gửi yêu cầu tới API Tencent Cloud"""
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": "asr.tencentcloudapi.com",
            "Authorization": authorization,
            "X-TC-Action": "SentenceRecognition",
            "X-TC-Version": self.API_VERSION,
            "X-TC-Timestamp": timestamp,
            "X-TC-Region": "ap-shanghai",
        }

        try:
            response = requests.post(self.API_URL, headers=headers, data=request_body)

            if not response.ok:
                raise IOError(f"Yêu cầu thất bại: {response.status_code} {response.reason}")

            response_json = response.json()

            # Kiểm tra có lỗi không
            if "Response" in response_json and "Error" in response_json["Response"]:
                error = response_json["Response"]["Error"]
                error_code = error["Code"]
                error_message = error["Message"]
                raise IOError(f"API trả về lỗi: {error_code}: {error_message}")

            # Trích xuất kết quả nhận dạng
            if "Response" in response_json and "Result" in response_json["Response"]:
                return response_json["Response"]["Result"]
            else:
                logger.bind(tag=TAG).warning(f"Trong phản hồi không có kết quả nhận dạng: {response_json}")
                return ""

        except Exception as e:
            logger.bind(tag=TAG).error(f"Gửi yêu cầu thất bại: {e}", exc_info=True)
            return None

    def _sha256_hex(self, data: str) -> str:
        """Tính giá trị hash SHA256 của chuỗi"""
        digest = hashlib.sha256(data.encode("utf-8")).digest()
        return self._bytes_to_hex(digest)

    def _hmac_sha256(self, key, data: str) -> bytes:
        """Tính HMAC-SHA256"""
        if isinstance(key, str):
            key = key.encode("utf-8")

        return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()

    def _bytes_to_hex(self, bytes_data: bytes) -> str:
        """Chuyển mảng byte thành chuỗi hex"""
        return "".join(f"{b:02x}" for b in bytes_data)
