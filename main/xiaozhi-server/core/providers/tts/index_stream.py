import os
import time
import queue
import aiohttp
import asyncio
import requests
import traceback
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.providers.tts.base import TTSProviderBase
from core.utils import opus_encoder_utils, textUtils
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.voice = config.get("voice", "xiao_he")
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice", "xiao_he")
        self.api_url = config.get("api_url", "http://8.138.114.124:11996/tts")
        self.audio_format = "pcm"
        self.before_stop_play_files = []

        # Tạo bộ mã hoá Opus, lưu ý tần số lấy mẫu giao diện trả về là 24000
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=24000, channels=1, frame_size_ms=60
        )

        # Bộ đệm PCM
        self.pcm_buffer = bytearray()

    def tts_text_priority_thread(self):
        """Luồng xử lý văn bản streaming"""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if message.sentence_type == SentenceType.FIRST:
                    # Khởi tạo tham số
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.before_stop_play_files.clear()
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text)

                elif ContentType.FILE == message.content_type:
                    logger.bind(tag=TAG).info(
                        f"Thêm tệp âm thanh vào danh sách chờ phát: {message.content_file}"
                    )
                    if message.content_file and os.path.exists(message.content_file):
                        # Xử lý trước dữ liệu âm thanh tệp
                        self._process_audio_file_stream(message.content_file, callback=lambda audio_data: self.handle_audio_file(audio_data, message.content_detail))

                if message.sentence_type == SentenceType.LAST:
                    # Xử lý văn bản còn lại
                    self._process_remaining_text_stream(True)

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Xử lý văn bản TTS thất bại: {str(e)}, kiểu: {type(e).__name__}, ngăn xếp: {traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self, is_last=False):
        """Xử lý văn bản còn lại và tạo giọng nói
        Returns:
            bool: Có xử lý văn bản thành công không
        """
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                self.processed_chars += len(full_text)
            else:
                self._process_before_stop_play_files()
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        try:
            max_repeat_time = 5
            original_text = text
            text = MarkdownCleaner.clean_markdown(text)
            if self._correct_words_pattern:
                text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], text)
            try:
                asyncio.run(self.text_to_speak(text, is_last))
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"Tạo giọng nói thất bại lần {5 - max_repeat_time + 1}: {original_text}, lỗi: {e}"
                )
                max_repeat_time -= 1

            if max_repeat_time > 0:
                logger.bind(tag=TAG).info(
                    f"Tạo giọng nói thành công: {original_text}, thử lại {5 - max_repeat_time} lần"
                )
            else:
                logger.bind(tag=TAG).error(
                    f"Tạo giọng nói thất bại: {original_text}, vui lòng kiểm tra mạng hoặc dịch vụ có bình thường không"
                )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
        finally:
            return None

    async def text_to_speak(self, text, is_last):
        """Xử lý streaming âm thanh TTS, mỗi câu chỉ đẩy một lần danh sách âm thanh"""
        payload = {"text": text, "character": self.voice}

        frame_bytes = int(
            self.opus_encoder.sample_rate
            * self.opus_encoder.channels  # 1
            * self.opus_encoder.frame_size_ms
            / 1000
            * 2
        )  # 16-bit = 2 bytes
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url, json=payload, timeout=self.tts_timeout
                ) as resp:

                    if resp.status != 200:
                        logger.bind(tag=TAG).error(
                            f"Yêu cầu TTS thất bại: {resp.status}, {await resp.text()}"
                        )
                        self.tts_audio_queue.put((SentenceType.LAST, [], None))
                        return

                    self.pcm_buffer.clear()
                    self.tts_audio_queue.put((SentenceType.FIRST, [], text))

                    # Xử lý dữ liệu luồng âm thanh
                    async for chunk in resp.content.iter_any():
                        data = chunk[0] if isinstance(chunk, (list, tuple)) else chunk
                        if not data:
                            continue

                        self.pcm_buffer.extend(data)

                        while len(self.pcm_buffer) >= frame_bytes:
                            frame = bytes(self.pcm_buffer[:frame_bytes])
                            del self.pcm_buffer[:frame_bytes]

                            self.opus_encoder.encode_pcm_to_opus_stream(
                                frame,
                                end_of_stream=False,
                                callback=self.handle_opus
                            )

                    # flush dữ liệu còn lại chưa đủ một khung
                    if self.pcm_buffer:
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            bytes(self.pcm_buffer),
                            end_of_stream=True,
                            callback=self.handle_opus
                        )
                        self.pcm_buffer.clear()

                    # Nếu là đoạn cuối cùng, xuất thông báo đã lấy xong âm thanh
                    if is_last:
                        self._process_before_stop_play_files()

        except Exception as e:
            logger.bind(tag=TAG).error(f"Ngoại lệ yêu cầu TTS: {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    def audio_to_pcm_data_stream(
        self, audio_file_path, callback=None
    ):
        """Chuyển tệp âm thanh sang mã hoá PCM, dùng tần số lấy mẫu 24kHz"""
        from core.utils.util import audio_to_data_stream
        return audio_to_data_stream(audio_file_path, is_opus=False, callback=callback, sample_rate=24000, opus_encoder=None)

    def audio_to_opus_data_stream(
        self, audio_file_path, callback=None
    ):
        """Chuyển tệp âm thanh sang mã hoá Opus, dùng tần số lấy mẫu 24kHz và bộ mã hoá riêng"""
        from core.utils.util import audio_to_data_stream
        return audio_to_data_stream(audio_file_path, is_opus=True, callback=callback, sample_rate=24000, opus_encoder=self.opus_encoder)

    async def close(self):
        """Dọn dẹp tài nguyên"""
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()

    def to_tts(self, text: str) -> list:
        """Xử lý TTS không streaming, dùng cho kịch bản kiểm thử và lưu tệp âm thanh
        Args:
            text: Văn bản cần chuyển
        Returns:
            list: Danh sách dữ liệu âm thanh sau khi mã hoá opus
        """
        start_time = time.time()
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], text)

        payload = {"text": text, "character": self.voice}

        try:
            with requests.post(
                self.api_url, json=payload, timeout=self.tts_timeout
            ) as response:
                if response.status_code != 200:
                    logger.bind(tag=TAG).error(
                        f"Yêu cầu TTS thất bại: {response.status_code}, {response.text}"
                    )
                    return []

                logger.info(f"Yêu cầu TTS thành công: {text}, mất: {time.time() - start_time} giây")

                # Dùng bộ mã hoá opus để xử lý dữ liệu PCM
                opus_datas = []
                pcm_data = response.content

                # Tính số byte mỗi khung
                frame_bytes = int(
                    self.opus_encoder.sample_rate
                    * self.opus_encoder.channels
                    * self.opus_encoder.frame_size_ms
                    / 1000
                    * 2
                )

                # Xử lý dữ liệu PCM theo khung
                for i in range(0, len(pcm_data), frame_bytes):
                    frame = pcm_data[i : i + frame_bytes]
                    if len(frame) < frame_bytes:
                        # Khung cuối cùng có thể thiếu, điền bằng 0
                        frame = frame + b"\x00" * (frame_bytes - len(frame))

                    self.opus_encoder.encode_pcm_to_opus_stream(
                        frame,
                        end_of_stream=(i + frame_bytes >= len(pcm_data)),
                        callback=lambda opus: opus_datas.append(opus)
                    )

                return opus_datas

        except Exception as e:
            logger.bind(tag=TAG).error(f"Ngoại lệ yêu cầu TTS: {e}")
            return []
