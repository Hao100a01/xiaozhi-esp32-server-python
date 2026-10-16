import hmac
import base64
import hashlib
import time


class AuthenticationError(Exception):
    """Ngoại lệ xác thực"""

    pass


class AuthManager:
    """
    Bộ quản lý xác thực ủy quyền thống nhất
    Sinh và xác thực bộ ba xác thực client_id device_id token (HMAC-SHA256)
    token không chứa client_id/device_id dạng plaintext, chỉ mang chữ ký + timestamp; client_id/device_id được truyền khi kết nối
    Trong MQTT, client_id: client_id, username: device_id, password: token
    Trong Websocket, header:{Device-ID: device_id, Client-ID: client_id, Authorization: Bearer token, ......}
    """

    def __init__(self, secret_key: str, expire_seconds: int = 60 * 60 * 24 * 30):
        if not expire_seconds or expire_seconds < 0:
            self.expire_seconds = 60 * 60 * 24 * 30
        else:
            self.expire_seconds = expire_seconds
        self.secret_key = secret_key

    def _sign(self, content: str) -> str:
        """Ký HMAC-SHA256 và mã hoá Base64"""
        sig = hmac.new(
            self.secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    def generate_token(self, client_id: str, username: str) -> str:
        """
        Sinh token
        Args:
            client_id: ID kết nối của thiết bị
            username: Tên người dùng của thiết bị (thường là deviceId)
        Returns:
            str: Chuỗi token
        """
        ts = int(time.time())
        content = f"{client_id}|{username}|{ts}"
        signature = self._sign(content)
        # token chỉ chứa chữ ký và timestamp, không chứa thông tin plaintext
        token = f"{signature}.{ts}"
        return token

    def verify_token(self, token: str, client_id: str, username: str) -> bool:
        """
        Xác thực tính hợp lệ của token
        Args:
            token: Token do client truyền vào
            client_id: client_id dùng cho kết nối
            username: username dùng cho kết nối
        """
        try:
            sig_part, ts_str = token.split(".")
            ts = int(ts_str)
            if int(time.time()) - ts > self.expire_seconds:
                return False  # Hết hạn

            expected_sig = self._sign(f"{client_id}|{username}|{ts}")
            if not hmac.compare_digest(sig_part, expected_sig):
                return False

            return True
        except Exception:
            return False
