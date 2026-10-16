import time
import os
from typing import Optional, Tuple, List
from aip import AipSpeech
from core.providers.asr.base import ASRProviderBase
from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool = True):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.app_id = config.get("app_id")
        self.api_key = config.get("api_key")
        self.secret_key = config.get("secret_key")

        dev_pid = config.get("dev_pid", "1537")
        self.dev_pid = int(dev_pid) if dev_pid else 1537

        self.output_dir = config.get("output_dir")
        self.delete_audio_file = delete_audio_file

        self.client = AipSpeech(str(self.app_id), self.api_key, self.secret_key)

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
            if not self.app_id or not self.api_key or not self.secret_key:
                logger.bind(tag=TAG).error("Cấu hình nhận dạng giọng nói Baidu chưa được thiết lập, không thể nhận dạng")
                return None, None

            if artifacts is None:
                return "", None

            start_time = time.time()
            # Nhận dạng tệp cục bộ
            result = self.client.asr(
                artifacts.pcm_bytes,
                "pcm",
                16000,
                {
                    "dev_pid": str(self.dev_pid),
                },
            )

            if result and result["err_no"] == 0:
                logger.bind(tag=TAG).debug(
                    f"Nhận dạng giọng nói Baidu mất: {time.time() - start_time:.3f}s | Kết quả: {result}"
                )
                result = result["result"][0]
                return result, artifacts.file_path
            else:
                raise Exception(
                    f"Nhận dạng giọng nói Baidu thất bại, mã lỗi: {result['err_no']}, thông tin lỗi: {result['err_msg']}"
                )
                return None, artifacts.file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"Đã xảy ra lỗi khi xử lý âm thanh! {e}", exc_info=True)
            return None, None
