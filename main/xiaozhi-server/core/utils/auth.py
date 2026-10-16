import jwt
import time
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
import base64


class AuthToken:
    def __init__(self, secret_key: str):
        self.secret_key = secret_key.encode()  # Chuyển thành bytes
        # Sinh khóa mã hoá độ dài cố định từ secret key (32 byte cho AES-256)
        self.encryption_key = self._derive_key(32)

    def _derive_key(self, length: int) -> bytes:
        """Sinh khóa có độ dài cố định"""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        # Dùng salt cố định (môi trường production thực tế nên dùng salt ngẫu nhiên)
        salt = b"fixed_salt_placeholder"  # Môi trường production nên đổi thành sinh ngẫu nhiên
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=length,
            salt=salt,
            iterations=100000,
            backend=default_backend(),
        )
        return kdf.derive(self.secret_key)

    def _encrypt_payload(self, payload: dict) -> str:
        """Mã hoá toàn bộ payload bằng AES-GCM"""
        # Chuyển payload thành chuỗi JSON
        payload_json = json.dumps(payload)

        # Sinh IV ngẫu nhiên
        iv = os.urandom(12)
        # Tạo bộ mã hoá
        cipher = Cipher(
            algorithms.AES(self.encryption_key),
            modes.GCM(iv),
            backend=default_backend(),
        )
        encryptor = cipher.encryptor()

        # Mã hoá và sinh tag
        ciphertext = encryptor.update(payload_json.encode()) + encryptor.finalize()
        tag = encryptor.tag

        # Ghép IV + ciphertext + tag
        encrypted_data = iv + ciphertext + tag
        return base64.urlsafe_b64encode(encrypted_data).decode()

    def _decrypt_payload(self, encrypted_data: str) -> dict:
        """Giải mã payload được mã hoá AES-GCM"""
        # Giải mã Base64
        data = base64.urlsafe_b64decode(encrypted_data.encode())
        # Tách các thành phần
        iv = data[:12]
        tag = data[-16:]
        ciphertext = data[12:-16]

        # Tạo bộ giải mã
        cipher = Cipher(
            algorithms.AES(self.encryption_key),
            modes.GCM(iv, tag),
            backend=default_backend(),
        )
        decryptor = cipher.decryptor()

        # Giải mã
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return json.loads(plaintext.decode())

    def generate_token(self, device_id: str) -> str:
        """
        Sinh JWT token
        :param device_id: ID thiết bị
        :return: Chuỗi JWT token
        """
        # Đặt thời gian hết hạn là 1 giờ sau
        expire_time = datetime.now(timezone.utc) + timedelta(hours=1)

        # Tạo payload gốc
        payload = {"device_id": device_id, "exp": expire_time.timestamp()}

        # Mã hoá toàn bộ payload
        encrypted_payload = self._encrypt_payload(payload)

        # Tạo payload lớp ngoài, chứa dữ liệu đã mã hoá
        outer_payload = {"data": encrypted_payload}

        # Mã hoá bằng JWT
        token = jwt.encode(outer_payload, self.secret_key, algorithm="HS256")
        return token

    def verify_token(self, token: str) -> Tuple[bool, Optional[str]]:
        """
        Xác thực token
        :param token: Chuỗi JWT token
        :return: (Có hợp lệ không, ID thiết bị)
        """
        try:
            # Xác thực JWT lớp ngoài trước (chữ ký và thời gian hết hạn)
            outer_payload = jwt.decode(token, self.secret_key, algorithms=["HS256"])

            # Giải mã payload lớp trong
            inner_payload = self._decrypt_payload(outer_payload["data"])

            # Kiểm tra lại thời gian hết hạn (xác thực kép)
            if inner_payload["exp"] < time.time():
                return False, None

            return True, inner_payload["device_id"]

        except jwt.InvalidTokenError:
            return False, None
        except json.JSONDecodeError:
            return False, None
        except Exception as e:  # Bắt các lỗi khác có thể xảy ra
            print(f"Token verification failed: {str(e)}")
            return False, None
