import time
import uuid
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.utils.dialogue import Message
from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType


TAG = __name__

class ListenTextMessageHandler(TextMessageHandler):
    """Bộ xử lý message Listen"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"Chế độ thu âm của client: {conn.client_listen_mode}"
            )
        if msg_json["state"] == "start":
            # Thiết bị chuyển từ chế độ phát sang chế độ thu, xóa toàn bộ trạng thái âm thanh và bộ đệm
            conn.reset_audio_states()
        elif msg_json["state"] == "stop":
            # Nhận được stop nhưng ASR chưa khởi tạo, bỏ qua xử lý
            if conn.asr is None:
                return

            conn.client_voice_stop = True
            if conn.asr.interface_type == InterfaceType.STREAM:
                # Ở chế độ streaming, gửi yêu cầu kết thúc
                asyncio.create_task(conn.asr._send_stop_request())
            else:
                # Chế độ không streaming: kích hoạt nhận dạng ASR trực tiếp
                if len(conn.asr_audio) > 0:
                    asr_audio_task = conn.asr_audio.copy()
                    conn.reset_audio_states()

                    if len(asr_audio_task) > 0:
                        await conn.asr.handle_voice_stop(conn, asr_audio_task)
        elif msg_json["state"] == "detect":
            conn.client_have_voice = False
            conn.reset_audio_states()
            if "text" in msg_json:
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]  # Giữ lại văn bản gốc
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                # Kiểm tra có phải lệnh gọi thiết bị [device_call]
                if original_text.startswith("[device_call]"):
                    # Trích xuất văn bản sau tag
                    call_text = original_text[len("[device_call]"):].strip()
                    conn.logger.bind(tag=TAG).info(f"Nhận được lệnh gọi thiết bị: {call_text}")

                    # Đánh dấu là chế độ nghe cuộc gọi đến
                    conn.incoming_call = True

                    # Chuẩn bị bắt đầu phiên mới
                    conn.sentence_id = uuid.uuid4().hex

                    await send_stt_message(conn, call_text)

                    # Chờ TTS khởi tạo, tối đa 3 giây
                    start_time = time.time()
                    while time.time() - start_time < 3:
                        if conn.tts:
                            break
                        await asyncio.sleep(0.1)

                    if conn.tts:
                        conn.tts.store_tts_text(conn.sentence_id, call_text)
                        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=conn.sentence_id, sentence_type=SentenceType.FIRST, content_type=ContentType.ACTION))
                        conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=call_text)
                        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=conn.sentence_id, sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))

                    # Thêm vào lịch sử hội thoại để model hiểu ngữ cảnh
                    conn.dialogue.put(Message(role="assistant", content=call_text))
                    return

                # Nhận diện có phải từ đánh thức (wakeup word)
                is_wakeup_words = filtered_text in conn.config.get("wakeup_words")
                # Có bật trả lời từ đánh thức hay không
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup_words and not enable_greeting:
                    # Nếu là từ đánh thức mà đã tắt trả lời từ đánh thức thì không cần trả lời
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup_words:
                    conn.just_woken_up = True
                    # Báo cáo dữ liệu text thuần (tái dùng chức năng báo cáo ASR, nhưng không cung cấp dữ liệu audio)
                    enqueue_asr_report(conn, "Này, chào bạn nhé", [])
                    await startToChat(conn, "Này, chào bạn nhé")
                else:
                    conn.just_woken_up = True
                    # Báo cáo dữ liệu text thuần (tái dùng chức năng báo cáo ASR, nhưng không cung cấp dữ liệu audio)
                    enqueue_asr_report(conn, original_text, [])
                    # Ngược lại cần LLM trả lời nội dung văn bản
                    await startToChat(conn, original_text)