import uuid
import json
import hmac
import time
import hashlib
import base64
import requests

from urllib import parse
from datetime import datetime
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.utils.tts import convert_percentage_to_range


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
            "RegionId": "cn-shanghai",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": str(uuid.uuid1()),
            "SignatureVersion": "1.0",
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Version": "2019-02-28",
        }
        # Tạo chuỗi request đã chuẩn hoá
        query_string = AccessToken._encode_dict(parameters)
        # print('Chuỗi request đã chuẩn hoá: %s' % query_string)
        # Tạo chuỗi cần ký
        string_to_sign = (
            "GET"
            + "&"
            + AccessToken._encode_text("/")
            + "&"
            + AccessToken._encode_text(query_string)
        )
        # print('Chuỗi cần ký: %s' % string_to_sign)
        # Tính chữ ký
        secreted_string = hmac.new(
            bytes(access_key_secret + "&", encoding="utf-8"),
            bytes(string_to_sign, encoding="utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(secreted_string)
        # print('Chữ ký: %s' % signature)
        # Mã hoá URL
        signature = AccessToken._encode_text(signature)
        # print('Chữ ký sau khi mã hoá URL: %s' % signature)
        # Gọi dịch vụ
        full_url = "http://nls-meta.cn-shanghai.aliyuncs.com/?Signature=%s&%s" % (
            signature,
            query_string,
        )
        # print('url: %s' % full_url)
        # Gửi HTTP GET request
        response = requests.get(full_url, timeout=timeout)
        if response.ok:
            root_obj = response.json()
            key = "Token"
            if key in root_obj:
                token = root_obj[key]["Id"]
                expire_time = root_obj[key]["ExpireTime"]
                return token, expire_time
        # print(response.text)
        return None, None


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "speech_rate", -500, 500, 0, int),
        ("ttsPitch", "pitch_rate", -500, 500, 0, int),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        # Thêm logic kiểm tra giá trị rỗng
        self.access_key_id = config.get("access_key_id")
        self.access_key_secret = config.get("access_key_secret")

        self.appkey = config.get("appkey")
        self.audio_file_type = config.get("format", "wav")

        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice", "xiaoyun")

        volume = config.get("volume", "50")
        self.volume = int(volume) if volume else 50

        speech_rate = config.get("speech_rate", "0")
        self.speech_rate = int(speech_rate) if speech_rate else 0

        pitch_rate = config.get("pitch_rate", "0")
        self.pitch_rate = int(pitch_rate) if pitch_rate else 0

        # Áp dụng điều chỉnh phần trăm (nếu có), ngược lại dùng cấu hình chung
        self._apply_percentage_params(config)

        self.host = config.get("host", "nls-gateway-cn-shanghai.aliyuncs.com")
        self.api_url = f"https://{self.host}/stream/v1/tts"
        self.header = {"Content-Type": "application/json"}

        if self.access_key_id and self.access_key_secret:
            # Dùng cặp khoá để tạo token tạm thời
            self._refresh_token()
        else:
            # Dùng trực tiếp token dài hạn đã tạo sẵn
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

            try:
                # Chuyển thống nhất sang chuỗi để xử lý
                expire_str = str(expire_time_str).strip()

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
            return False  # Token dài hạn không hết hạn
        # Thêm log gỡ lỗi
        # current_time = time.time()
        # remaining = self.expire_time - current_time
        # print(f"Kiểm tra hết hạn token: thời gian hiện tại {datetime.fromtimestamp(current_time)} | "
        #              f"Thời gian hết hạn {datetime.fromtimestamp(self.expire_time)} | "
        #              f"Còn lại {remaining:.2f} giây")
        return time.time() > self.expire_time

    async def text_to_speak(self, text, output_file):
        if self._is_token_expired():
            logger.warning("Token đã hết hạn, đang tự động làm mới...")
            self._refresh_token()
        request_json = {
            "appkey": self.appkey,
            "token": self.token,
            "text": text,
            "format": self.audio_file_type,
            "sample_rate": self.conn.sample_rate,
            "voice": self.voice,
            "volume": self.volume,
            "speech_rate": self.speech_rate,
            "pitch_rate": self.pitch_rate,
        }

        # print(self.api_url, json.dumps(request_json, ensure_ascii=False))
        try:
            resp = requests.post(
                self.api_url,
                json.dumps(request_json),
                headers=self.header,
                timeout=self.tts_timeout,
            )
            if resp.status_code == 401:  # Xử lý đặc biệt khi token hết hạn
                self._refresh_token()
                resp = requests.post(
                    self.api_url,
                    json.dumps(request_json),
                    headers=self.header,
                    timeout=self.tts_timeout,
                )
            # Kiểm tra mime type của dữ liệu yêu cầu trả về có phải audio/*** không, nếu đúng thì lưu vào đường dẫn chỉ định; trả về ở định dạng binary
            if resp.headers["Content-Type"].startswith("audio/"):
                if output_file:
                    with open(output_file, "wb") as f:
                        f.write(resp.content)
                    return output_file
                else:
                    return resp.content
            else:
                raise Exception(
                    f"{__name__} status_code: {resp.status_code} response: {resp.content}"
                )
        except Exception as e:
            raise Exception(f"{__name__} error: {e}")
