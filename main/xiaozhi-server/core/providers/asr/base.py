import os
import io
import wave
import uuid
import json
import time
import queue
import shutil
import asyncio
import tempfile
import traceback
import threading

from abc import ABC, abstractmethod
from config.logger import setup_logging
from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.utils.util import remove_punctuation_and_length
from core.handle.receiveAudioHandle import handleAudioMessage
from typing import Optional, Tuple, List, NamedTuple, TYPE_CHECKING


if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class ASRProviderBase(ABC):
    def __init__(self):
        pass

    # Mở kênh âm thanh
    async def open_audio_channels(self, conn: "ConnectionHandler"):
        conn.asr_priority_thread = threading.Thread(
            target=self.asr_text_priority_thread, args=(conn,), daemon=True
        )
        conn.asr_priority_thread.start()

    # Xử lý âm thanh ASR theo thứ tự
    def asr_text_priority_thread(self, conn: "ConnectionHandler"):
        while not conn.stop_event.is_set():
            try:
                message = conn.asr_audio_queue.get(timeout=1)
                future = asyncio.run_coroutine_threadsafe(
                    handleAudioMessage(conn, message),
                    conn.loop,
                )
                future.result()
            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Xử lý văn bản ASR thất bại: {str(e)}, kiểu: {type(e).__name__}, ngăn xếp: {traceback.format_exc()}"
                )
                continue

    # Nhận âm thanh
    async def receive_audio(self, conn: "ConnectionHandler", pcm_frame, audio_have_voice):
        if conn.client_listen_mode == "manual":
            # Chế độ thủ công: đệm âm thanh để nhận dạng ASR
            conn.asr_audio.append(pcm_frame)
        else:
            # Chế độ tự động/thời gian thực: dùng VAD để phát hiện
            conn.asr_audio.append(pcm_frame)

            # Nếu không có giọng nói, và trước đó cũng không có âm thanh, đệm một phần âm thanh
            if not audio_have_voice and not conn.client_have_voice:
                conn.asr_audio = conn.asr_audio[-10:]
                return

            # Ở chế độ tự động, kích hoạt nhận dạng khi VAD phát hiện giọng nói dừng
            if conn.asr.interface_type != InterfaceType.STREAM and conn.client_voice_stop:
                # Dùng trực tiếp dữ liệu PCM trong asr_audio
                pcm_bytes = b"".join(conn.asr_audio)
                # Kiểm tra có đủ dữ liệu âm thanh không (mỗi khung 1920 byte, 15 khung khoảng 28800 byte)
                if len(pcm_bytes) > 1920 * 15:
                    await self.handle_voice_stop(conn, [pcm_bytes])
                conn.reset_audio_states()

    # Xử lý dừng giọng nói
    async def handle_voice_stop(self, conn: "ConnectionHandler", asr_audio_task: List[bytes]):
        """Xử lý song song ASR và nhận dạng vân giọng"""
        try:
            total_start_time = time.monotonic()

            # Dữ liệu đã là PCM, dùng trực tiếp
            pcm_data = asr_audio_task
            combined_pcm_data = b"".join(pcm_data)

            # Chuẩn bị trước dữ liệu WAV
            wav_data = None
            if conn.voiceprint_provider and combined_pcm_data:
                wav_data = self._pcm_to_wav(combined_pcm_data)

            # Định nghĩa tác vụ ASR
            asr_task = self.speech_to_text_wrapper(
                asr_audio_task, conn.session_id
            )

            if conn.voiceprint_provider and wav_data:
                voiceprint_task = conn.voiceprint_provider.identify_speaker(
                    wav_data, conn.session_id
                )
                # Chờ đồng thời hai kết quả
                asr_result, voiceprint_result = await asyncio.gather(
                    asr_task, voiceprint_task, return_exceptions=True
                )
            else:
                asr_result = await asr_task
                voiceprint_result = None

            # Ghi lại kết quả nhận dạng - kiểm tra có phải ngoại lệ không
            if isinstance(asr_result, Exception):
                logger.bind(tag=TAG).error(f"Nhận dạng ASR thất bại: {asr_result}")
                raw_text = ""
            else:
                raw_text, _ = asr_result

            if isinstance(voiceprint_result, Exception):
                logger.bind(tag=TAG).error(f"Nhận dạng vân giọng thất bại: {voiceprint_result}")
                speaker_name = ""
            else:
                speaker_name = voiceprint_result

            # Xác định kiểu kết quả ASR
            if isinstance(raw_text, dict):
                # Định dạng dict do FunASR trả về
                if speaker_name:
                    raw_text["speaker"] = speaker_name

                # Ghi lại kết quả nhận dạng
                if raw_text.get("language"):
                    logger.bind(tag=TAG).info(f"Ngôn ngữ nhận dạng: {raw_text['language']}")
                if raw_text.get("emotion"):
                    logger.bind(tag=TAG).info(f"Cảm xúc nhận dạng: {raw_text['emotion']}")
                if raw_text.get("content"):
                    logger.bind(tag=TAG).info(f"Văn bản nhận dạng: {raw_text['content']}")
                if speaker_name:
                    logger.bind(tag=TAG).info(f"Người nói được nhận dạng: {speaker_name}")

                # Chuyển thành chuỗi JSON để dùng cho bước sau
                enhanced_text = json.dumps(raw_text, ensure_ascii=False)
                content_for_length_check = raw_text.get("content", "")
            else:
                # Văn bản thuần do các ASR khác trả về
                if raw_text:
                    logger.bind(tag=TAG).info(f"Văn bản nhận dạng: {raw_text}")
                if speaker_name:
                    logger.bind(tag=TAG).info(f"Người nói được nhận dạng: {speaker_name}")

                # Tạo chuỗi JSON chứa thông tin người nói
                enhanced_text = self._build_enhanced_text(raw_text, speaker_name)
                content_for_length_check = raw_text

            # Giám sát hiệu năng
            total_time = time.monotonic() - total_start_time
            logger.bind(tag=TAG).debug(f"Tổng thời gian xử lý: {total_time:.3f}s")

            # Kiểm tra độ dài văn bản
            text_len, _ = remove_punctuation_and_length(content_for_length_check)
            self.stop_ws_connection()

            if text_len > 0:
                audio_snapshot = asr_audio_task.copy()
                enqueue_asr_report(conn, enhanced_text, audio_snapshot)
                # Dùng module tuỳ chỉnh để báo cáo
                await startToChat(conn, enhanced_text)
        except Exception as e:
            logger.bind(tag=TAG).error(f"Xử lý dừng giọng nói thất bại: {e}")
            import traceback

            logger.bind(tag=TAG).debug(f"Chi tiết ngoại lệ: {traceback.format_exc()}")

    def _build_enhanced_text(self, text: str, speaker_name: Optional[str]) -> str:
        """Tạo văn bản chứa thông tin người nói (chỉ dùng cho ASR văn bản thuần)"""
        if speaker_name and speaker_name.strip():
            return json.dumps(
                {"speaker": speaker_name, "content": text}, ensure_ascii=False
            )
        else:
            return text

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """Chuyển dữ liệu PCM sang định dạng WAV"""
        if len(pcm_data) == 0:
            logger.bind(tag=TAG).warning("Dữ liệu PCM trống, không thể chuyển sang WAV")
            return b""

        # Đảm bảo độ dài dữ liệu là số chẵn (âm thanh 16-bit)
        if len(pcm_data) % 2 != 0:
            pcm_data = pcm_data[:-1]

        # Tạo header tệp WAV
        wav_buffer = io.BytesIO()
        try:
            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)  # Đơn kênh (mono)
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(16000)  # Tần số lấy mẫu 16kHz
                wav_file.writeframes(pcm_data)

            wav_buffer.seek(0)
            wav_data = wav_buffer.read()

            return wav_data
        except Exception as e:
            logger.bind(tag=TAG).error(f"Chuyển đổi WAV thất bại: {e}")
            return b""

    def stop_ws_connection(self):
        pass

    async def close(self):
        pass

    class AudioArtifacts(NamedTuple):
        pcm_frames: List[bytes]
        """Danh sách khung âm thanh PCM"""
        pcm_bytes: bytes
        """Dữ liệu byte âm thanh PCM sau khi gộp"""
        file_path: Optional[str]
        """Đường dẫn tệp WAV"""
        temp_path: Optional[str]
        """Đường dẫn tệp WAV tạm thời"""

    def get_current_artifacts(self) -> Optional["ASRProviderBase.AudioArtifacts"]:
        return self._current_artifacts

    def requires_file(self) -> bool:
        """Có cần đầu vào tệp không"""
        return False

    def prefers_temp_file(self) -> bool:
        """Có ưu tiên dùng tệp tạm thời không"""
        return False

    def build_temp_file(self, pcm_bytes: bytes) -> Optional[str]:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_path = temp_file.name
            with wave.open(temp_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(pcm_bytes)
            return temp_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"Tạo tệp âm thanh tạm thời thất bại: {e}")
            return None

    def save_audio_to_file(self, pcm_data: List[bytes], session_id: str) -> str:
        """Lưu dữ liệu PCM thành tệp WAV"""
        module_name = __name__.split(".")[-1]
        file_name = f"asr_{module_name}_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)

        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))

        return file_path

    async def speech_to_text_wrapper(
        self, pcm_data: List[bytes], session_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        file_path = None
        temp_path = None
        try:
            combined_pcm_data = b"".join(pcm_data)

            free_space = shutil.disk_usage(self.output_dir).free
            if free_space < len(combined_pcm_data) * 2:
                raise OSError("Dung lượng đĩa không đủ")

            if self.requires_file() and self.prefers_temp_file():
                temp_path = self.build_temp_file(combined_pcm_data)

            if (hasattr(self, "delete_audio_file") and not self.delete_audio_file) or (
                self.requires_file() and not self.prefers_temp_file()
            ):
                file_path = self.save_audio_to_file(pcm_data, session_id)

            if len(combined_pcm_data) == 0:
                artifacts = None
            else:
                artifacts = ASRProviderBase.AudioArtifacts(
                    pcm_frames=pcm_data,
                    pcm_bytes=combined_pcm_data,
                    file_path=file_path,
                    temp_path=temp_path,
                )

            text, _ = await self.speech_to_text(
                pcm_data, session_id, artifacts
            )
            return text, file_path
        except OSError as e:
            logger.bind(tag=TAG).error(f"Lỗi thao tác tệp: {e}")
            return None, None
        except Exception as e:
            logger.bind(tag=TAG).error(f"Nhận dạng giọng nói thất bại: {e}")
            return None, None
        finally:
            try:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
                if (
                    hasattr(self, "delete_audio_file")
                    and self.delete_audio_file
                    and file_path
                    and os.path.exists(file_path)
                ):
                    os.remove(file_path)
            except Exception as e:
                logger.bind(tag=TAG).error(f"Dọn dẹp tệp thất bại: {e}")

    @abstractmethod
    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        artifacts: Optional[AudioArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Chuyển dữ liệu giọng nói thành văn bản

        :param opus_data: Dữ liệu âm thanh Opus đầu vào
        :param session_id: ID phiên
        :param artifacts: Đối tượng âm thanh, chứa dữ liệu PCM, đường dẫn tệp, v.v.
        :return: Văn bản kết quả nhận dạng và đường dẫn tệp (nếu có)
        """
        pass
