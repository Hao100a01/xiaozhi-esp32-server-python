import http.client
import json
import asyncio
from typing import Optional, Tuple, List
import os
import uuid
import hmac
import hashlib
import base64
import requests
from urllib import parse
import time
from datetime import datetime
from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

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
        response = requests.get(full_url)
        if response.ok:
            root_obj = response.json()
            key = "Token"
            if key in root_obj:
                token = root_obj[key]["Id"]
                expire_time = root_obj[key]["ExpireTime"]
                return token, expire_time
        # print(response.text)
        return None, None


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        """Khởi tạo ASR Alibaba Cloud"""
        # Thêm logic kiểm tra giá trị rỗng
        self.access_key_id = config.get("access_key_id")
        self.access_key_secret = config.get("access_key_secret")

        self.app_key = config.get("appkey")
        self.host = "nls-gateway-cn-shanghai.aliyuncs.com"
        self.base_url = f"https://{self.host}/stream/v1/asr"
        self.sample_rate = 16000
        self.format = "wav"
        self.output_dir = config.get("output_dir", "./audio_output")
        self.delete_audio_file = delete_audio_file

        if self.access_key_id and self.access_key_secret:
            # Dùng cặp khoá để tạo token tạm thời
            self._refresh_token()
        else:
            # Dùng trực tiếp token dài hạn đã tạo sẵn
            self.token = config.get("token")
            self.expire_time = None

        # Đảm bảo thư mục đầu ra tồn tại
        os.makedirs(self.output_dir, exist_ok=True)

    def _refresh_token(self):
        """Làm mới token và ghi lại thời gian hết hạn"""
        if self.access_key_id and self.access_key_secret:
            self.token, expire_time_str = AccessToken.create_token(
                self.access_key_id, self.access_key_secret
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

    def _construct_request_url(self) -> str:
        """Tạo URL yêu cầu, bao gồm tham số"""
        request = f"{self.base_url}?appkey={self.app_key}"
        request += f"&format={self.format}"
        request += f"&sample_rate={self.sample_rate}"
        request += "&enable_punctuation_prediction=true"
        request += "&enable_inverse_text_normalization=true"
        request += "&enable_voice_detection=false"
        return request

    async def _send_request(self, pcm_data: bytes) -> Optional[str]:
        """Gửi yêu cầu tới dịch vụ ASR Alibaba Cloud"""
        try:
            # Thiết lập HTTP header
            headers = {
                "X-NLS-Token": self.token,
                "Content-type": "application/octet-stream",
                "Content-Length": str(len(pcm_data)),
            }

            # Tạo kết nối và gửi yêu cầu
            conn = http.client.HTTPSConnection(self.host)
            request_url = self._construct_request_url()

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: conn.request(
                    method="POST", url=request_url, body=pcm_data, headers=headers
                ),
            )

            # Lấy phản hồi
            response = await loop.run_in_executor(None, conn.getresponse)
            body = await loop.run_in_executor(None, response.read)
            conn.close()

            # Phân tích phản hồi
            try:
                body_json = json.loads(body)
                status = body_json.get("status")

                if status == 20000000:
                    result = body_json.get("result", "")
                    logger.bind(tag=TAG).debug(f"Kết quả ASR: {result}")
                    return result
                else:
                    logger.bind(tag=TAG).error(f"ASR thất bại, mã trạng thái: {status}")
                    return None

            except ValueError:
                logger.bind(tag=TAG).error("Phản hồi không phải định dạng JSON")
                return None

        except Exception as e:
            logger.bind(tag=TAG).error(f"Yêu cầu ASR thất bại: {e}", exc_info=True)
            return None

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Chuyển dữ liệu giọng nói thành văn bản"""
        if self._is_token_expired():
            logger.warning("Token đã hết hạn, đang tự động làm mới...")
            self._refresh_token()

        try:
            if artifacts is None:
                return "", None
            # Gửi yêu cầu và lấy văn bản
            text = await self._send_request(artifacts.pcm_bytes)

            if text:
                return text, artifacts.file_path

            return "", artifacts.file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"Nhận dạng giọng nói thất bại: {e}", exc_info=True)
            return "", None
