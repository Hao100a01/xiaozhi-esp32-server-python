import time
import json
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_stt_message, SentenceType

TAG = __name__


async def handleAudioMessage(conn: "ConnectionHandler", pcm_frame):
    # Đoạn hiện tại có người nói không
    have_voice = conn.vad.is_vad(conn, pcm_frame)
    # Nếu thiết bị vừa được đánh thức, tạm thời bỏ qua phát hiện VAD
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # Đặt một độ trễ ngắn rồi khôi phục phát hiện VAD
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        return
    # Chức năng AEC phía server cần kích hoạt ngắt theo thời gian thực
    if conn.client_aec and have_voice:
        if conn.client_is_speaking and conn.client_listen_mode != "manual":
            await handleAbortMessage(conn)
    # Phát hiện thiết bị nhàn rỗi lâu, dùng để say goodbye
    await no_voice_close_connect(conn, have_voice)
    # Nhận audio
    await conn.asr.receive_audio(conn, pcm_frame, have_voice)


async def resume_vad_detection(conn: "ConnectionHandler"):
    # Chờ 2 giây rồi khôi phục phát hiện VAD
    await asyncio.sleep(2)
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text):
    # Kiểm tra đầu vào có phải định dạng JSON (chứa thông tin người nói)
    speaker_name = None
    actual_text = text

    try:
        # Thử phân tích đầu vào định dạng JSON
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                actual_content = data["content"]
                conn.logger.bind(tag=TAG).info(f"Phân tích được thông tin người nói: {speaker_name}")

                # Chỉ giữ JSON {"speaker":...} khi người nói này xuất hiện lần đầu, để model gọi tên một cách tự nhiên một lần;
                # Các lượt sau hạ xuống text thuần, tránh tên lặp lại mỗi lượt khiến model gọi tên liên tục
                if speaker_name not in conn.introduced_speakers:
                    conn.introduced_speakers.add(speaker_name)
                    actual_text = text
                else:
                    actual_text = actual_content
            elif "content" in data:
                # FunASR v.v. trả về {"content","language","emotion"}: chỉ đưa chính văn nhận diện cho LLM,
                # nếu không cả đoạn JSON sẽ đi vào lịch sử hội thoại
                actual_text = data["content"]
    except (json.JSONDecodeError, KeyError):
        # Nếu phân tích thất bại, tiếp tục dùng text gốc
        pass

    # Lưu thông tin người nói vào đối tượng kết nối
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # Nếu số từ output trong ngày lớn hơn số từ giới hạn
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return

    # Ở chế độ manual không ngắt nội dung đang phát
    if conn.client_is_speaking and conn.client_listen_mode != "manual":
        await handleAbortMessage(conn)

    # Trước tiên phân tích ý định, dùng nội dung text thực tế
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # Nếu ý định đã được xử lý thì không chat nữa
        return

    # Ý định chưa được xử lý, tiếp tục quy trình chat thông thường, dùng nội dung text thực tế
    await send_stt_message(conn, actual_text)

    # Chuẩn bị bắt đầu phiên mới
    conn.client_abort = False

    conn.executor.submit(conn.chat, actual_text)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    # Chỉ kiểm tra timeout khi timestamp đã được khởi tạo
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("Kết thúc hội thoại, không cần gửi câu kết thúc")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "Hãy dùng ```thời gian trôi thật nhanh``` làm câu mở đầu, nói lời kết thúc cuộc trò chuyện này thật giàu cảm xúc, đầy lưu luyến nhé!"
            await startToChat(conn, prompt)


async def max_out_size(conn: "ConnectionHandler"):
    # Phát thông báo vượt quá số từ output tối đa
    conn.client_abort = False
    text = "Xin lỗi nhé, giờ tôi có chút việc bận, mai giờ này mình nói chuyện tiếp nhé, hứa rồi đấy! Mai không gặp không về, tạm biệt!"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        # Đảm bảo bind_code là 6 chữ số
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"Định dạng mã liên kết không hợp lệ: {conn.bind_code}")
            text = "Định dạng mã liên kết sai, vui lòng kiểm tra cấu hình."
            await send_stt_message(conn, text)
            return

        text = f"Hãy đăng nhập bảng điều khiển, nhập {conn.bind_code} để liên kết thiết bị."
        await send_stt_message(conn, text)

        # Phát âm báo
        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # Phát từng chữ số
        for i in range(6):  # Đảm bảo chỉ phát 6 chữ số
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"Phát audio chữ số thất bại: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # Phát thông báo chưa liên kết
        conn.client_abort = False
        text = f"Không tìm thấy thông tin phiên bản của thiết bị, hãy cấu hình đúng địa chỉ OTA rồi biên dịch lại firmware."
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
