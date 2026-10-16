import hashlib
import hmac
import time
import uuid
import json
import base64
import requests
from datetime import datetime, timezone
from core.providers.tts.base import TTSProviderBase


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", -10, 10, 0, lambda v: round(float(v), 1)),
        ("ttsRate", "speed", -2, 6, 0, lambda v: round(float(v), 2)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.appid = config.get("appid")
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = int(config.get("voice"))
        self.api_url = "https://tts.tencentcloudapi.com"  # Endpoint API đúng
        self.region = config.get("region")
        self.output_file = config.get("output_dir")
        self.audio_file_type = config.get("format", "wav")

        # Cấu hình tham số âm thanh
        speed = config.get("speed", "0")
        self.speed = int(speed) if speed else 0

        volume = config.get("volume", "0")
        self.volume = int(volume) if volume else 0

        # Áp dụng điều chỉnh phần trăm (nếu có), ngược lại dùng cấu hình chung
        self._apply_percentage_params(config)

    def _get_auth_headers(self, request_body):
        """Tạo header yêu cầu xác thực"""
        # Lấy timestamp UTC hiện tại
        timestamp = int(time.time())

        # Dùng giờ UTC để tính ngày
        utc_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )

        # Tên dịch vụ phải là "tts"
        service = "tts"

        # Nối phạm vi credential
        credential_scope = f"{utc_date}/{service}/tc3_request"

        # Dùng phương thức ký TC3-HMAC-SHA256
        algorithm = "TC3-HMAC-SHA256"

        # Tạo chuỗi request chuẩn
        http_request_method = "POST"
        canonical_uri = "/"
        canonical_querystring = ""

        # Header yêu cầu phải chứa host và content-type, và sắp xếp theo thứ tự từ điển
        canonical_headers = (
            f"content-type:application/json\n" f"host:tts.tencentcloudapi.com\n"
        )
        signed_headers = "content-type;host"

        # Giá trị hash của body yêu cầu
        payload = json.dumps(request_body)
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        # Tạo chuỗi request chuẩn
        canonical_request = (
            f"{http_request_method}\n"
            f"{canonical_uri}\n"
            f"{canonical_querystring}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        # Tính giá trị hash của request chuẩn
        hashed_canonical_request = hashlib.sha256(
            canonical_request.encode("utf-8")
        ).hexdigest()

        # Tạo chuỗi cần ký
        string_to_sign = (
            f"{algorithm}\n"
            f"{timestamp}\n"
            f"{credential_scope}\n"
            f"{hashed_canonical_request}"
        )

        # Tính khoá ký
        secret_date = self._hmac_sha256(
            f"TC3{self.secret_key}".encode("utf-8"), utc_date
        )
        secret_service = self._hmac_sha256(secret_date, service)
        secret_signing = self._hmac_sha256(secret_service, "tc3_request")

        # Tính chữ ký
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Tạo header uỷ quyền
        authorization = (
            f"{algorithm} "
            f"Credential={self.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        # Tạo header yêu cầu
        headers = {
            "Content-Type": "application/json",
            "Host": "tts.tencentcloudapi.com",
            "Authorization": authorization,
            "X-TC-Action": "TextToVoice",
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": "2019-08-23",
            "X-TC-Region": self.region,
            "X-TC-Language": "zh-CN",
        }

        return headers

    def _hmac_sha256(self, key, msg):
        """Mã hoá HMAC-SHA256"""
        if isinstance(msg, str):
            msg = msg.encode("utf-8")
        return hmac.new(key, msg, hashlib.sha256).digest()

    async def text_to_speak(self, text, output_file):
        # Tạo body yêu cầu
        request_json = {
            "Text": text,  # Văn bản nguồn để tổng hợp giọng nói
            "SessionId": str(uuid.uuid4()),  # ID phiên, tạo ngẫu nhiên
            "VoiceType": int(self.voice),  # Giọng đọc
            "Codec": self.audio_file_type,  # Định dạng mã hoá âm thanh
            "Volume": self.volume,  # Âm lượng
            "Speed": self.speed,  # Tốc độ nói
            "SampleRate": self.conn.sample_rate,  # Tần số lấy mẫu hỗ trợ một phần 24000
        }

        try:
            # Lấy header yêu cầu (mỗi yêu cầu đều tạo lại, để đảm bảo timestamp và chữ ký là mới nhất)
            headers = self._get_auth_headers(request_json)

            # Gửi yêu cầu
            resp = requests.post(
                self.api_url,
                json.dumps(request_json),
                headers=headers,
                timeout=self.tts_timeout,
            )

            # Kiểm tra phản hồi
            if resp.status_code == 200:
                response_data = resp.json()

                # Kiểm tra có thành công không
                if response_data.get("Response", {}).get("Error") is not None:
                    error_info = response_data["Response"]["Error"]
                    raise Exception(
                        f"API trả về lỗi: {error_info['Code']}: {error_info['Message']}"
                    )

                # Giải mã dữ liệu âm thanh Base64
                audio_bytes = base64.b64decode(response_data["Response"].get("Audio"))
                if audio_bytes:
                    if output_file:
                        with open(output_file, "wb") as f:
                            f.write(audio_bytes)
                    else:
                        return audio_bytes
                else:
                    raise Exception(f"{__name__}: Không trả về dữ liệu âm thanh: {response_data}")
            else:
                raise Exception(
                    f"{__name__} status_code: {resp.status_code} response: {resp.content}"
                )
        except Exception as e:
            raise Exception(f"{__name__} error: {e}")
