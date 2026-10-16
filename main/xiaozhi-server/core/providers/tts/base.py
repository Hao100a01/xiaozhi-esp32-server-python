import os
import re
import math
import uuid
import queue
import asyncio
import threading
import traceback

from core.utils import p3
from datetime import datetime
from core.utils import textUtils
from typing import Callable, Any, Optional
from abc import ABC, abstractmethod
from config.logger import setup_logging
from core.utils import opus_encoder_utils
from core.utils.tts import MarkdownCleaner, convert_percentage_to_range
from core.utils.output_counter import add_device_output
from core.handle.reportHandle import enqueue_tts_report
from core.handle.sendAudioHandle import sendAudioMessage
from core.utils.util import audio_bytes_to_data_stream, audio_to_data_stream
from core.providers.tts.dto.dto import (
    TTSMessageDTO,
    SentenceType,
    ContentType,
    InterfaceType,
)

TAG = __name__
logger = setup_logging()


class TTSProviderBase(ABC):
    def __init__(self, config, delete_audio_file):
        self.interface_type = InterfaceType.NON_STREAM
        self.conn = None
        self.delete_audio_file = delete_audio_file
        self.audio_file_type = "wav"
        self.output_file = config.get("output_dir", "tmp/")
        self.tts_timeout = float(config.get("tts_timeout", 15))
        if not math.isfinite(self.tts_timeout) or self.tts_timeout <= 0:
            raise ValueError("tts_timeout must be a positive finite number")
        self.tts_text_queue = queue.Queue()
        self.tts_audio_queue = queue.Queue()
        self.tts_audio_first_sentence = True
        self.before_stop_play_files = []
        self.report_on_last = False
        # Ánh xạ sentence_id tới văn bản, dùng để TTS streaming lấy đúng văn bản phụ đề
        self._sentence_text_map = {}
        # Tải từ thay thế, dùng cho thay thế regex một lần
        raw_words = config.get("correct_words", [])
        self.correct_words = {}
        for item in raw_words:
            parts = item.split("|", 1)
            if len(parts) == 2:
                self.correct_words[parts[0]] = parts[1]
        # Tạo biểu thức chính quy, ưu tiên khớp dài nhất (sắp xếp rồi escape nối lại)
        if self.correct_words:
            # Sắp xếp giảm dần theo độ dài key, từ dài khớp trước, tránh từ ngắn gây nhiễu một phần
            sorted_keys = sorted(self.correct_words.keys(), key=len, reverse=True)
            pattern_str = "|".join(re.escape(k) for k in sorted_keys)
            self._correct_words_pattern = re.compile(pattern_str)
            # Tạo regex thay thế ngược, dùng để khôi phục văn bản đã thay thế do dịch vụ TTS trả về thành văn bản gốc (hiển thị phụ đề)
            reverse_map = {v: k for k, v in self.correct_words.items()}
            sorted_reverse_keys = sorted(reverse_map.keys(), key=len, reverse=True)
            reverse_pattern_str = "|".join(re.escape(k) for k in sorted_reverse_keys)
            self._reverse_words_pattern = re.compile(reverse_pattern_str)
            self._reverse_words_map = reverse_map
            # Cửa sổ trượt streaming: dict từ thay thế nhóm theo ký tự đầu, dùng để tra nhanh
            self._words_by_first_char = {}
            for key in sorted_keys:  # Dùng keys đã sắp xếp giảm dần theo độ dài, đảm bảo từ dài khớp trước
                first_char = key[0] if key else ""
                if first_char not in self._words_by_first_char:
                    self._words_by_first_char[first_char] = []
                self._words_by_first_char[first_char].append(key)
        else:
            self._correct_words_pattern = None
            self._reverse_words_pattern = None
            self._reverse_words_map = None

        # Cửa sổ trượt streaming: văn bản đệm chờ khớp
        self._pending_prefix = ""
        self.tts_text_buff = []
        self.punctuations = (
            "。",
            "？",
            "?",
            "！",
            "!",
            "；",
            ";",
            "：",
        )
        self.first_sentence_punctuations = (
            "，",
            "~",
            "、",
            ",",
            "。",
            "？",
            "?",
            "！",
            "!",
            "；",
            ";",
            "：",
        )
        self.tts_stop_request = False
        self.processed_chars = 0
        self.is_first_sentence = True

    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    def handle_opus(self, opus_data: bytes):
        logger.bind(tag=TAG).debug(f"Số khung đẩy vào hàng đợi～～ {len(opus_data)}")
        self.tts_audio_queue.put((SentenceType.MIDDLE, opus_data, None, getattr(self, 'current_sentence_id', None)))

    def handle_audio_file(self, file_audio: bytes, text):
        self.before_stop_play_files.append((file_audio, text))

    def to_tts_stream(self, text, opus_handler: Callable[[bytes], None] = None) -> None:
        # Giữ văn bản gốc để hiển thị/báo cáo
        original_text = text
        text = MarkdownCleaner.clean_markdown(text)
        # Dùng regex thay thế một lần, tránh duyệt lặp và vấn đề khớp một phần
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], text)
        max_repeat_time = 5
        if self.delete_audio_file:
            # Tệp cần xoá thì chuyển trực tiếp thành dữ liệu âm thanh
            while max_repeat_time > 0:
                try:
                    audio_bytes = asyncio.run(self.text_to_speak(text, None))
                    if audio_bytes:
                        # Dùng văn bản gốc để hiển thị/báo cáo
                        self.tts_audio_queue.put((SentenceType.FIRST, None, original_text, getattr(self, 'current_sentence_id', None)))
                        audio_bytes_to_data_stream(
                            audio_bytes,
                            file_type=self.audio_file_type,
                            is_opus=True,
                            callback=opus_handler,
                            sample_rate=self.conn.sample_rate,
                            opus_encoder=self.opus_encoder,
                        )
                        break
                    else:
                        max_repeat_time -= 1
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
            return None
        else:
            tmp_file = self.generate_filename()
            try:
                while not os.path.exists(tmp_file) and max_repeat_time > 0:
                    try:
                        asyncio.run(self.text_to_speak(text, tmp_file))
                    except Exception as e:
                        logger.bind(tag=TAG).warning(
                            f"Tạo giọng nói thất bại lần {5 - max_repeat_time + 1}: {original_text}, lỗi: {e}"
                        )
                        # Chưa thực thi thành công, xoá tệp
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                        max_repeat_time -= 1

                if max_repeat_time > 0:
                    logger.bind(tag=TAG).info(
                        f"Tạo giọng nói thành công: {original_text}:{tmp_file}, thử lại {5 - max_repeat_time} lần"
                    )
                else:
                    logger.bind(tag=TAG).error(
                        f"Tạo giọng nói thất bại: {original_text}, vui lòng kiểm tra mạng hoặc dịch vụ có bình thường không"
                    )
                self.tts_audio_queue.put((SentenceType.FIRST, None, original_text, getattr(self, 'current_sentence_id', None)))
                self._process_audio_file_stream(tmp_file, callback=opus_handler)
            except Exception as e:
                logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
                return None
    
    def to_tts(self, text):
        # Giữ văn bản gốc cho log/hiển thị
        original_text = text
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(lambda m: self.correct_words[m.group(0)], text)
        max_repeat_time = 5
        if self.delete_audio_file:
            # Tệp cần xoá thì chuyển trực tiếp thành dữ liệu âm thanh
            while max_repeat_time > 0:
                try:
                    audio_bytes = asyncio.run(self.text_to_speak(text, None))
                    if audio_bytes:
                        audio_datas = []
                        audio_bytes_to_data_stream(
                            audio_bytes,
                            file_type=self.audio_file_type,
                            is_opus=True,
                            callback=lambda data: audio_datas.append(data),
                            sample_rate=self.conn.sample_rate,
                        )
                        return audio_datas
                    else:
                        max_repeat_time -= 1
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
            return None
        else:
            tmp_file = self.generate_filename()
            try:
                while not os.path.exists(tmp_file) and max_repeat_time > 0:
                    try:
                        asyncio.run(self.text_to_speak(text, tmp_file))
                    except Exception as e:
                        logger.bind(tag=TAG).warning(
                            f"Tạo giọng nói thất bại lần {5 - max_repeat_time + 1}: {original_text}, lỗi: {e}"
                        )
                        # Chưa thực thi thành công, xoá tệp
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                        max_repeat_time -= 1

                if max_repeat_time > 0:
                    logger.bind(tag=TAG).info(
                        f"Tạo giọng nói thành công: {original_text}:{tmp_file}, thử lại {5 - max_repeat_time} lần"
                    )
                else:
                    logger.bind(tag=TAG).error(
                        f"Tạo giọng nói thất bại: {original_text}, vui lòng kiểm tra mạng hoặc dịch vụ có bình thường không"
                    )

                return tmp_file
            except Exception as e:
                logger.bind(tag=TAG).error(f"Failed to generate TTS file: {e}")
                return None

    @abstractmethod
    async def text_to_speak(self, text, output_file):
        pass

    def audio_to_pcm_data_stream(
        self, audio_file_path, callback: Callable[[Any], Any] = None
    ):
        """Chuyển tệp âm thanh sang mã hoá PCM"""
        return audio_to_data_stream(audio_file_path, is_opus=False, callback=callback, sample_rate=self.conn.sample_rate, opus_encoder=None)

    def audio_to_opus_data_stream(
        self, audio_file_path, callback: Callable[[Any], Any] = None
    ):
        """Chuyển tệp âm thanh sang mã hoá Opus"""
        return audio_to_data_stream(audio_file_path, is_opus=True, callback=callback, sample_rate=self.conn.sample_rate, opus_encoder=self.opus_encoder)

    def tts_one_sentence(
        self,
        conn,
        content_type,
        content_detail=None,
        content_file=None,
        sentence_id=None,
    ):
        """Gửi một câu"""
        if not sentence_id:
            if conn.sentence_id:
                sentence_id = conn.sentence_id
            else:
                sentence_id = str(uuid.uuid4().hex)
                conn.sentence_id = sentence_id
        # Với văn bản một câu, xử lý chia đoạn
        segments = re.split(r"([。！？!?；;\n])", content_detail)
        for seg in segments:
            self.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=content_type,
                    content_detail=seg,
                    content_file=content_file,
                )
            )

    async def open_audio_channels(self, conn):
        self.conn = conn

        # Tạo bộ mã hoá theo sample_rate của conn, nếu lớp con đã tạo thì không ghi đè (giao diện IndexTTS trả về 24kHz - chờ xử lý resample)
        if not hasattr(self, 'opus_encoder') or self.opus_encoder is None:
            self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
                sample_rate=conn.sample_rate, channels=1, frame_size_ms=60
            )

        # Luồng tiêu hoá tts
        self.tts_priority_thread = threading.Thread(
            target=self.tts_text_priority_thread, daemon=True
        )
        self.tts_priority_thread.start()

        # Luồng tiêu hoá phát âm thanh
        self.audio_play_priority_thread = threading.Thread(
            target=self._audio_play_priority_thread, daemon=True
        )
        self.audio_play_priority_thread.start()

    def store_tts_text(self, sentence_id, text):
        """Lưu trữ văn bản tương ứng với sentence_id được chỉ định, dùng để TTS streaming lấy đúng văn bản phụ đề

        Args:
            sentence_id: ID phiên
            text: Văn bản cần lưu trữ
        """
        if sentence_id and text:
            self._sentence_text_map[sentence_id] = text
            # Chỉ giữ 5 cái gần nhất, tránh rò rỉ bộ nhớ
            if len(self._sentence_text_map) > 5:
                oldest = next(iter(self._sentence_text_map))
                del self._sentence_text_map[oldest]

    def get_tts_text(self, sentence_id):
        """Lấy văn bản tương ứng với sentence_id được chỉ định

        Args:
            sentence_id: ID phiên

        Returns:
            str: Văn bản tương ứng, nếu không tồn tại trả về None
        """
        return self._sentence_text_map.get(sentence_id)

    def clear_tts_text(self, sentence_id):
        """Xoá văn bản của sentence_id được chỉ định

        Args:
            sentence_id: ID phiên
        """
        if sentence_id in self._sentence_text_map:
            del self._sentence_text_map[sentence_id]

    def _restore_original_text(self, text):
        if not self._reverse_words_pattern or not text:
            return text
        return self._reverse_words_pattern.sub(
            lambda m: self._reverse_words_map[m.group(0)], text
        )

    # Ở đây mặc định là cách xử lý không streaming
    # Cách xử lý streaming hãy ghi đè trong lớp con
    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if self.conn.client_abort:
                    logger.bind(tag=TAG).info("Đã nhận thông tin ngắt, dừng luồng xử lý văn bản TTS")
                    continue
                # Lọc thông điệp cũ: kiểm tra sentence_id có khớp không
                if message.sentence_id != self.conn.sentence_id:
                    continue
                if message.sentence_type == SentenceType.FIRST:
                    self.current_sentence_id = message.sentence_id
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.is_first_sentence = True
                    self.tts_audio_first_sentence = True
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_stream(segment_text, opus_handler=self.handle_opus)
                elif ContentType.FILE == message.content_type:
                    self._process_remaining_text_stream(opus_handler=self.handle_opus)
                    tts_file = message.content_file
                    if tts_file and os.path.exists(tts_file):
                        self._process_audio_file_stream(
                            tts_file, callback=self.handle_opus
                        )
                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text_stream(opus_handler=self.handle_opus)
                    self.tts_audio_queue.put(
                        (message.sentence_type, [], message.content_detail, message.sentence_id)
                    )

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Xử lý văn bản TTS thất bại: {str(e)}, kiểu: {type(e).__name__}, ngăn xếp: {traceback.format_exc()}"
                )
                continue

    def _audio_play_priority_thread(self):
        # Danh sách văn bản và âm thanh cần báo cáo
        enqueue_text = None
        enqueue_audio = []
        while not self.conn.stop_event.is_set():
            text = None
            try:
                try:
                    item = self.tts_audio_queue.get(timeout=0.1)
                    if len(item) == 4:
                        sentence_type, audio_datas, text, sentence_id = item
                    else:
                        sentence_type, audio_datas, text = item
                        sentence_id = None
                except queue.Empty:
                    if self.conn.stop_event.is_set():
                        break
                    continue

                if self.conn.client_abort:
                    logger.bind(tag=TAG).debug("Đã nhận tín hiệu ngắt, bỏ qua dữ liệu âm thanh hiện tại")
                    enqueue_text, enqueue_audio = None, []
                    continue

                # Báo cáo khi nhận bắt đầu văn bản tiếp theo hoặc kết thúc phiên
                if sentence_type is not SentenceType.MIDDLE:
                    if self.report_on_last:
                        # Chế độ tích luỹ: phù hợp TTS chỉ có một luồng giọng nói suốt quá trình (như seed-tts-2.0)
                        # Khi FIRST chỉ ghi văn bản, âm thanh tích luỹ liên tục, chỉ báo cáo thống nhất khi LAST
                        if text:
                            enqueue_text = text
                        if sentence_type == SentenceType.LAST:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                            enqueue_audio = []
                            enqueue_text = None
                    else:
                        # Chế độ không tích luỹ: mỗi câu báo cáo riêng
                        if enqueue_text is not None:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                        enqueue_audio = []
                        enqueue_text = text

                # Thu thập dữ liệu âm thanh báo cáo
                if isinstance(audio_datas, bytes):
                    enqueue_audio.append(audio_datas)

                # Gửi âm thanh
                future = asyncio.run_coroutine_threadsafe(
                    sendAudioMessage(self.conn, sentence_type, audio_datas, text, sentence_id),
                    self.conn.loop,
                )
                future.result()

                # Ghi đầu ra và báo cáo
                if self.conn.max_output_size > 0 and text:
                    add_device_output(self.conn.headers.get("device-id"), len(text))

            except Exception as e:
                logger.bind(tag=TAG).error(f"audio_play_priority_thread: {text} {e}")

    async def start_session(self, session_id):
        pass

    async def finish_session(self, session_id):
        pass

    async def close(self):
        """Phương thức dọn dẹp tài nguyên"""
        self._sentence_text_map.clear()
        if hasattr(self, "ws") and self.ws:
            await self.ws.close()

    # Dấu ngoặc kép phải/ngoặc đơn phải, và ký hiệu bên trái tương ứng của chúng
    _QUOTE_PAIRS = (
        ("「", "」"),
        ("『", "』"),
        ("“", "”"),
        ('"', '"'),
        ("（", "）"),
        ("《", "》"),
        ("«", "»"),
        ("„", "“"),
        ("【", "】"),
        ("〈", "〉"),
        ("［", "］"),
        ("[", "]"),
    )
    # Lưu ý: " (U+201C) có hai nghĩa: trong tiếng Trung là dấu mở ngoặc kép (tương ứng "),
    # Trong tiếng Đức „…" là dấu đóng ngoặc kép; khi hai nghĩa xung đột, phương pháp đếm không thể đáp ứng cả hai,
    # Việc chia câu tiếng Đức chỉ có thể xử lý thận trọng bằng cách "không cân bằng thì dời tiếp" (cuối cùng flush sẽ dự phòng).
    _CLOSERS = '」』”》）"»“】〉］]'
    _QUOTE_CHARS = "".join(ch for pair in _QUOTE_PAIRS for ch in pair)

    @classmethod
    def _quote_safe_cut(cls, text: str, pos: int, punctuations) -> int:
        """Tránh chia trong ngoặc kép (như 「わぁ！」って / "Tốt!" anh ấy nói).

        Dấu ngoặc kép phải và dấu câu cuối câu ngay sau vị trí ứng viên được mang theo cùng; nếu ứng viên nằm trong ngoặc kép chưa đóng,
        hoặc câu vẫn tiếp tục sau dấu ngoặc kép phải, thì dời tới dấu câu tiếp theo; khi đều không an toàn thì trả về -1, chờ thêm văn bản.
        """
        if not any(ch in cls._QUOTE_CHARS for ch in text):
            # Toàn văn không chứa ngoặc kép/ngoặc đơn: không tồn tại ngoặc kép chưa đóng, dấu ngoặc kép phải cũng không cần mang theo, vị trí ứng viên chắc chắn an toàn
            return pos
        candidates = [pos] + [
            i for i, ch in enumerate(text) if i > pos and ch in punctuations
        ]
        for candidate in candidates:
            end = candidate + 1
            carried_closer = False
            while end < len(text) and (
                text[end] in cls._CLOSERS or text[end] in punctuations
            ):
                if text[end] in cls._CLOSERS:
                    carried_closer = True
                end += 1
            segment = text[:end]
            # Ngoặc kép ghép cặp trước-sau (「」 v.v.) dùng chênh lệch đếm để phát hiện; ngoặc kép cùng ký tự trước-sau (") dùng chẵn lẻ để phát hiện
            balanced = True
            for opener, closer in cls._QUOTE_PAIRS:
                n_open = segment.count(opener)
                if opener == closer:
                    if n_open % 2:
                        balanced = False
                        break
                elif n_open > segment.count(closer):
                    balanced = False
                    break
            if not balanced:
                continue
            if carried_closer and (end == len(text) or not text[end].isspace()):
                # Câu vẫn tiếp tục sau dấu ngoặc kép phải (「…」って、"…" anh ấy nói), hoặc tạm thời chưa thấy phần sau
                continue
            return end - 1
        return -1

    def _get_segment_text(self) -> Optional[str]:
        # Gộp toàn bộ văn bản hiện tại và xử lý phần chưa chia
        full_text = "".join(self.tts_text_buff)
        current_text = full_text[self.processed_chars :]  # Bắt đầu từ vị trí chưa xử lý
        last_punct_pos = -1

        # Chọn tập hợp dấu câu khác nhau tuỳ có phải câu đầu tiên không
        punctuations_to_use = (
            self.first_sentence_punctuations
            if self.is_first_sentence
            else self.punctuations
        )

        # Lấy vị trí xuất hiện cuối cùng sớm nhất của mỗi loại dấu câu làm điểm chia (tương đương với rfind từng cái rồi lấy giá trị nhỏ nhất)
        last_seen = {}
        for i, ch in enumerate(current_text):
            if ch in punctuations_to_use:
                last_seen[ch] = i
        last_punct_pos = min(last_seen.values(), default=-1)

        cut_pos = -1
        if last_punct_pos != -1:
            cut_pos = self._quote_safe_cut(
                current_text, last_punct_pos, punctuations_to_use
            )
        if cut_pos != -1:
            segment_text_raw = current_text[: cut_pos + 1]
            segment_text = textUtils.get_string_no_punctuation_or_emoji(
                segment_text_raw
            )
            self.processed_chars += len(segment_text_raw)  # Cập nhật vị trí ký tự đã xử lý

            # Nếu là câu đầu tiên, sau khi tìm thấy dấu phẩy đầu tiên, đặt cờ thành False
            if self.is_first_sentence:
                self.is_first_sentence = False

            return segment_text
        elif self.tts_stop_request and current_text:
            segment_text = current_text
            self.is_first_sentence = True  # Đặt lại cờ
            return segment_text
        else:
            return None

    def _process_audio_file_stream(
        self, tts_file, callback: Callable[[Any], Any]
    ) -> None:
        """Xử lý tệp âm thanh và chuyển sang định dạng được chỉ định

        Args:
            tts_file: Đường dẫn tệp âm thanh
            callback: Hàm xử lý tệp
        """
        if tts_file.endswith(".p3"):
            p3.decode_opus_from_file_stream(tts_file, callback=callback)
        elif self.conn.audio_format == "pcm":
            self.audio_to_pcm_data_stream(tts_file, callback=callback)
        else:
            self.audio_to_opus_data_stream(tts_file, callback=callback)

        if (
            self.delete_audio_file
            and tts_file is not None
            and os.path.exists(tts_file)
            and tts_file.startswith(self.output_file)
        ):
            os.remove(tts_file)

    def _process_before_stop_play_files(self):
        for audio_datas, text in self.before_stop_play_files:
            self.tts_audio_queue.put((SentenceType.MIDDLE, audio_datas, text, getattr(self, 'current_sentence_id', None)))
        self.before_stop_play_files.clear()
        self.tts_audio_queue.put((SentenceType.LAST, [], None, getattr(self, 'current_sentence_id', None)))

    def _process_remaining_text_stream(
        self, opus_handler: Callable[[bytes], None] = None
    ):
        """Xử lý văn bản còn lại và tạo giọng nói

        Returns:
            bool: Có xử lý văn bản thành công không
        """
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_stream(segment_text, opus_handler=opus_handler)
                self.processed_chars += len(full_text)
                return True
        return False

    def _apply_percentage_params(self, config):
        """Áp dụng hàng loạt tham số phần trăm theo TTS_PARAM_CONFIG do lớp con định nghĩa"""
        for config_key, attr_name, min_val, max_val, base_val, transform in self.TTS_PARAM_CONFIG:
            if config_key in config:
                val = convert_percentage_to_range(config[config_key], min_val, max_val, base_val)
                setattr(self, attr_name, transform(val) if transform else val)

    def _match_stream_text(self, text):
        """Khớp cửa sổ trượt văn bản streaming, dùng để xử lý từ thay thế xuyên mảnh

        Args:
            text: Đoạn văn bản đầu vào

        Returns:
            tuple: (danh sách văn bản đã xác định, tiền tố còn chờ khớp)
        """
        if not self.correct_words or not text:
            return [text] if text else [], ""

        result = []
        pending = self._pending_prefix
        i = 0

        while i < len(text):
            char = text[i]

            # Thử: pending + ký tự hiện tại có khớp từ thay thế không
            test_text = pending + char

            matched = False
            # Duyệt các từ thay thế có thể khớp
            candidates = self._words_by_first_char.get(pending[0], []) if pending else self._words_by_first_char.get(char, [])
            for key in candidates:
                if test_text == key:
                    # Khớp đầy đủ, thay thế rồi gửi
                    result.append(self.correct_words[key])
                    pending = ""
                    matched = True
                    break
                elif key.startswith(test_text):
                    # Là tiền tố của từ thay thế, tiếp tục chờ
                    pending = test_text
                    matched = True
                    break

            if matched:
                i += 1
                continue

            # Không khớp được từ dài hơn, nội dung của pending chắc chắn có thể gửi
            if pending:
                result.append(pending)
                pending = ""

            # Kiểm tra ký tự hiện tại có phải đầu của từ thay thế nào không
            if char in self._words_by_first_char:
                pending = char
            else:
                result.append(char)

            i += 1

        return result, pending

    def reset_stream_state(self):
        """Đặt lại trạng thái xử lý streaming, dùng để dọn trạng thái còn sót khi bắt đầu phiên"""
        self._pending_prefix = ""
