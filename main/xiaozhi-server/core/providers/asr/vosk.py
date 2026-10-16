import os
import json
import time
from typing import Optional, Tuple, List
from .base import ASRProviderBase
from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
import vosk

TAG = __name__
logger = setup_logging()

class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool = True):
        super().__init__()
        self.interface_type = InterfaceType.LOCAL
        self.model_path = config.get("model_path")
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file
        
        # Khởi tạo mô hình VOSK
        self.model = None
        self.recognizer = None
        self._load_model()
        
        # Đảm bảo thư mục đầu ra tồn tại
        os.makedirs(self.output_dir, exist_ok=True)

    def _load_model(self):
        """Tải mô hình VOSK"""
        try:
            if not os.path.exists(self.model_path):
                raise FileNotFoundError(f"Đường dẫn mô hình VOSK không tồn tại: {self.model_path}")
                
            logger.bind(tag=TAG).info(f"Đang tải mô hình VOSK: {self.model_path}")
            self.model = vosk.Model(self.model_path)

            # Khởi tạo bộ nhận dạng VOSK (tần số lấy mẫu phải là 16kHz)
            self.recognizer = vosk.KaldiRecognizer(self.model, 16000)

            logger.bind(tag=TAG).info("Tải mô hình VOSK thành công")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Tải mô hình VOSK thất bại: {e}")
            raise

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """Chuyển dữ liệu giọng nói thành văn bản"""
        try:
            # Kiểm tra mô hình đã tải thành công chưa
            if not self.model:
                logger.bind(tag=TAG).error("Mô hình VOSK chưa được tải, không thể nhận dạng")
                return "", None
            
            if artifacts is None:
                return "", None
            if not artifacts.pcm_bytes:
                logger.bind(tag=TAG).warning("Dữ liệu PCM sau khi gộp trống")
                return "", None

            start_time = time.time()
            
            
            # Tiến hành nhận dạng (VOSK khuyến nghị mỗi lần đưa 2000 byte dữ liệu)
            chunk_size = 2000
            text_result = ""
            
            for i in range(0, len(artifacts.pcm_bytes), chunk_size):
                chunk = artifacts.pcm_bytes[i:i+chunk_size]
                if self.recognizer.AcceptWaveform(chunk):
                    result = json.loads(self.recognizer.Result())
                    text = result.get('text', '')
                    if text:
                        text_result += text + " "
            
            # Lấy kết quả cuối cùng
            final_result = json.loads(self.recognizer.FinalResult())
            final_text = final_result.get('text', '')
            if final_text:
                text_result += final_text
            
            logger.bind(tag=TAG).debug(
                f"Nhận dạng giọng nói VOSK mất: {time.time() - start_time:.3f}s | Kết quả: {text_result.strip()}"
            )
            
            return text_result.strip(), artifacts.file_path
            
        except Exception as e:
            logger.bind(tag=TAG).error(f"Nhận dạng giọng nói VOSK thất bại: {e}")
            return "", None
