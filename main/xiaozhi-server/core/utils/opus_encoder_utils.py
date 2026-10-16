"""
Lớp công cụ mã hoá Opus
Mã hoá dữ liệu audio PCM thành định dạng Opus
"""

import logging
import traceback
import numpy as np
from opuslib_next import Encoder
from opuslib_next import constants
from typing import Optional, Callable, Any

class OpusEncoderUtils:
    """Bộ mã hoá từ PCM sang Opus"""

    def __init__(self, sample_rate: int, channels: int, frame_size_ms: int):
        """
        Khởi tạo bộ mã hoá Opus

        Args:
            sample_rate: Tần số lấy mẫu (Hz)
            channels: Số kênh (1=mono, 2=stereo)
            frame_size_ms: Kích thước frame (mili giây)
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size_ms = frame_size_ms
        # Tính số mẫu mỗi frame = tần số lấy mẫu * kích thước frame (mili giây) / 1000
        self.frame_size = (sample_rate * frame_size_ms) // 1000
        # Tổng kích thước frame = số mẫu mỗi frame * số kênh
        self.total_frame_size = self.frame_size * channels

        # Cài đặt bitrate và độ phức tạp
        self.bitrate = 24000  # bps
        self.complexity = 10  # Chất lượng cao nhất

        # Khởi tạo bộ đệm rỗng
        self.buffer = np.array([], dtype=np.int16)

        try:
            # Tạo bộ mã hoá Opus
            self.encoder = Encoder(
                sample_rate, channels, constants.APPLICATION_AUDIO  # Chế độ tối ưu audio
            )
            self.encoder.bitrate = self.bitrate
            self.encoder.complexity = self.complexity
            self.encoder.signal = constants.SIGNAL_VOICE  # Tối ưu tín hiệu giọng nói
        except Exception as e:
            logging.error(f"Khởi tạo bộ mã hoá Opus thất bại: {e}")
            raise RuntimeError("Khởi tạo thất bại") from e

    def reset_state(self):
        """Đặt lại trạng thái bộ mã hoá"""
        self.encoder.reset_state()
        self.buffer = np.array([], dtype=np.int16)

    def encode_pcm_to_opus_stream(self, pcm_data: bytes, end_of_stream: bool, callback: Callable[[Any], Any]):
        """
        Mã hoá dữ liệu PCM thành định dạng Opus, xử lý theo kiểu streaming

        Args:
            pcm_data: Dữ liệu byte PCM
            end_of_stream: Có phải cuối luồng,
            callback: Phương thức xử lý opus

        Returns:
            Danh sách gói Opus
        """
        # Chuyển dữ liệu byte thành mảng short
        new_samples = self._convert_bytes_to_shorts(pcm_data)

        # Kiểm tra dữ liệu PCM
        self._validate_pcm_data(new_samples)

        # Thêm dữ liệu mới vào bộ đệm
        self.buffer = np.append(self.buffer, new_samples)

        offset = 0

        # Xử lý mọi frame đầy đủ
        while offset <= len(self.buffer) - self.total_frame_size:
            frame = self.buffer[offset : offset + self.total_frame_size]
            output = self._encode(frame)
            if output:
                callback(output)
            offset += self.total_frame_size

        # Giữ lại các mẫu chưa xử lý
        self.buffer = self.buffer[offset:]

        # Xử lý dữ liệu còn lại khi luồng kết thúc
        if end_of_stream and len(self.buffer) > 0:
            # Tạo frame cuối cùng và điền 0
            last_frame = np.zeros(self.total_frame_size, dtype=np.int16)
            last_frame[: len(self.buffer)] = self.buffer

            output = self._encode(last_frame)
            if output:
                callback(output)
            self.buffer = np.array([], dtype=np.int16)

    def _encode(self, frame: np.ndarray) -> Optional[bytes]:
        """Mã hoá một frame dữ liệu audio"""
        try:
            # Bộ mã hoá đã giải phóng, bỏ qua mã hoá
            if not hasattr(self, 'encoder') or self.encoder is None:
                return None
            # Chuyển mảng numpy thành bytes
            frame_bytes = frame.tobytes()
            # opuslib yêu cầu số byte đầu vào phải là bội số của channels*2
            encoded = self.encoder.encode(frame_bytes, self.frame_size)
            return encoded
        except Exception as e:
            logging.error(f"Mã hoá Opus thất bại: {e}")
            traceback.print_exc()
            return None

    def _convert_bytes_to_shorts(self, bytes_data: bytes) -> np.ndarray:
        """Chuyển mảng byte thành mảng short (PCM 16-bit)"""
        # Giả định đầu vào là PCM 16-bit little-endian
        return np.frombuffer(bytes_data, dtype=np.int16)

    def _validate_pcm_data(self, pcm_shorts: np.ndarray) -> None:
        """Kiểm tra dữ liệu PCM có hợp lệ"""
        # Phạm vi dữ liệu PCM 16-bit là -32768 đến 32767
        if np.any((pcm_shorts < -32768) | (pcm_shorts > 32767)):
            invalid_samples = pcm_shorts[(pcm_shorts < -32768) | (pcm_shorts > 32767)]
            logging.warning(f"Phát hiện mẫu PCM không hợp lệ: {invalid_samples[:5]}...")
            # Trong ứng dụng thực tế có thể chọn cắt (clip) thay vì ném ngoại lệ
            # np.clip(pcm_shorts, -32768, 32767, out=pcm_shorts)

    def close(self):
        """Đóng bộ mã hoá và giải phóng tài nguyên"""
        if hasattr(self, 'encoder') and self.encoder:
            try:
                del self.encoder
                self.encoder = None
            except Exception as e:
                logging.error(f"Error releasing Opus encoder: {e}")