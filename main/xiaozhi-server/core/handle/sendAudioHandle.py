import json
import time
import asyncio
import opuslib_next
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils import textUtils
from core.utils.util import audio_to_data
from core.providers.tts.dto.dto import SentenceType
from core.utils.audioRateController import AudioRateController

TAG = __name__
# Thời lượng frame audio (mili giây)
AUDIO_FRAME_DURATION = 60
# Số gói đệm trước, gửi trực tiếp để giảm độ trễ
PRE_BUFFER_COUNT = 5


async def sendAudioMessage(conn: "ConnectionHandler", sentenceType, audios, text, sentence_id=None):
    # Bỏ qua audio còn sót của câu cũ
    if sentence_id is not None and sentence_id != conn.sentence_id:
        return

    if conn.tts.tts_audio_first_sentence:
        conn.logger.bind(tag=TAG).info(f"Gửi đoạn thoại đầu tiên: {text}")
        conn.tts.tts_audio_first_sentence = False

    if sentenceType == SentenceType.FIRST:
        # Message tiếp theo của cùng một câu thì vào hàng đợi điều tiết luồng, các trường hợp khác gửi ngay
        if (
            hasattr(conn, "audio_rate_controller")
            and conn.audio_rate_controller
            and getattr(conn, "audio_flow_control", {}).get("sentence_id")
            == conn.sentence_id
        ):
            conn.audio_rate_controller.add_message(
                lambda: send_tts_message(conn, "sentence_start", text)
            )
        else:
            # Câu mới hoặc bộ điều tiết luồng chưa khởi tạo thì gửi ngay
            await send_tts_message(conn, "sentence_start", text)

    await sendAudio(conn, audios)
    # Gửi message bắt đầu câu
    if sentenceType is not SentenceType.MIDDLE:
        conn.logger.bind(tag=TAG).info(f"Gửi message audio: {sentenceType}, {text}")

    # Gửi message kết thúc (nếu là text cuối cùng)
    # Cuộc gọi cần duy trì trạng thái speaking
    if not conn.calling and sentenceType == SentenceType.LAST:
        await send_tts_message(conn, "stop", None)
        if conn.close_after_chat:
            await conn.close()


async def _wait_for_audio_completion(conn: "ConnectionHandler"):
    """
    Chờ hàng đợi audio rỗng và chờ các gói đệm trước phát xong

    Args:
        conn: Đối tượng kết nối
    """
    if hasattr(conn, "audio_rate_controller") and conn.audio_rate_controller:
        rate_controller = conn.audio_rate_controller
        send_delay_ms = conn.config.get("tts_audio_send_delay", 0)
        conn.logger.bind(tag=TAG).debug(
            f"Chờ gửi audio xong, trong hàng đợi còn {len(rate_controller.queue)} gói"
        )
        await rate_controller.queue_empty_event.wait()

        if send_delay_ms > 0:
            # Nhịp tuỳ chỉnh: chờ frame cuối cùng phát xong ở client
            playback_time = 2 * rate_controller.interval_ms / 1000.0
        else:
            # Nhịp mặc định: N frame đệm trước đã gửi trực tiếp, cần chờ chúng phát xong
            playback_time = (PRE_BUFFER_COUNT + 2) * rate_controller.interval_ms / 1000.0
        await asyncio.sleep(playback_time)

        conn.logger.bind(tag=TAG).debug("Gửi audio hoàn tất")


async def _send_to_mqtt_gateway(
    conn: "ConnectionHandler", opus_packet, timestamp, sequence
):
    """
    Gửi gói opus có header 16 byte tới mqtt_gateway, đồng thời cache audio để xử lý AEC
    Args:
        conn: Đối tượng kết nối
        opus_packet: Gói opus
        timestamp: Timestamp
        sequence: Số thứ tự
    """
    # Nếu đã bật AEC phía server, cache dữ liệu PCM để xử lý AEC về sau
    if conn.client_aec and timestamp > 0:
        if not hasattr(conn, "aec_audio_cache"):
            conn.aec_audio_cache = {}
            conn.aec_audio_cache_time = {}
            conn._send_opus_decoder = opuslib_next.Decoder(16000, 1)
        # Giải mã opus thành PCM rồi cache
        pcm_data = conn._send_opus_decoder.decode(bytes(opus_packet), 960)
        conn.aec_audio_cache[timestamp] = bytes(pcm_data)
        conn.aec_audio_cache_time[timestamp] = time.time()

    # Thêm header 16 byte cho gói opus
    header = bytearray(16)
    header[0] = 1  # type
    header[2:4] = len(opus_packet).to_bytes(2, "big")  # payload length
    header[4:8] = sequence.to_bytes(4, "big")  # sequence
    header[8:12] = timestamp.to_bytes(4, "big")  # Timestamp
    header[12:16] = len(opus_packet).to_bytes(4, "big")  # Độ dài opus

    # Gửi gói đầy đủ gồm header
    complete_packet = bytes(header) + opus_packet
    await conn.websocket.send(complete_packet)


async def sendAudio(
    conn: "ConnectionHandler", audios, frame_duration=AUDIO_FRAME_DURATION
):
    """
    Gửi gói audio, dùng AudioRateController để điều tiết lưu lượng chính xác

    Args:
        conn: Đối tượng kết nối
        audios: Một gói opus (bytes) hoặc danh sách gói opus
        frame_duration: Thời lượng frame (mili giây), mặc định dùng hằng số toàn cục AUDIO_FRAME_DURATION
    """
    if audios is None or len(audios) == 0:
        return

    send_delay_ms = conn.config.get("tts_audio_send_delay", 0)
    is_single_packet = isinstance(audios, bytes)

    # Khởi tạo hoặc lấy RateController
    rate_controller, flow_control = _get_or_create_rate_controller(
        conn, frame_duration, is_single_packet, send_delay_ms
    )

    # Chuyển thống nhất thành danh sách để xử lý
    audio_list = [audios] if is_single_packet else audios

    # Gửi gói audio
    await _send_audio_with_rate_control(
        conn, audio_list, rate_controller, flow_control, send_delay_ms
    )


def _get_or_create_rate_controller(
    conn: "ConnectionHandler", frame_duration, is_single_packet, send_delay_ms=0
):
    """
    Lấy hoặc tạo RateController và flow_control

    Args:
        conn: Đối tượng kết nối
        frame_duration: Thời lượng frame
        is_single_packet: Có phải chế độ một gói (True: một gói streaming TTS, False: gói hàng loạt)
        send_delay_ms: Khoảng cách gửi tuỳ chỉnh (mili giây).

    Returns:
        (rate_controller, flow_control)
    """
    # Kiểm tra có cần đặt lại bộ điều khiển
    need_reset = False

    if not hasattr(conn, "audio_rate_controller"):
        # Bộ điều khiển chưa tồn tại, cần tạo
        need_reset = True
    else:
        rate_controller = conn.audio_rate_controller

        # Task gửi nền đã dừng thì cần đặt lại
        if (
            not rate_controller.pending_send_task
            or rate_controller.pending_send_task.done()
        ):
            need_reset = True
        # Khi sentence_id thay đổi, cần đặt lại
        elif (
            getattr(conn, "audio_flow_control", {}).get("sentence_id")
            != conn.sentence_id
        ):
            need_reset = True

    if need_reset:
        # Tạo hoặc lấy rate_controller
        if not hasattr(conn, "audio_rate_controller"):
            conn.audio_rate_controller = AudioRateController(
                frame_duration, send_delay=send_delay_ms
            )
        else:
            conn.audio_rate_controller.reset()

        # Khởi tạo flow_control
        conn.audio_flow_control = {
            "packet_count": 0,
            "sequence": 0,
            "sentence_id": conn.sentence_id,
        }

        # Khởi động vòng lặp gửi nền
        _start_background_sender(
            conn, conn.audio_rate_controller, conn.audio_flow_control
        )

    return conn.audio_rate_controller, conn.audio_flow_control


def _start_background_sender(conn: "ConnectionHandler", rate_controller, flow_control):
    """
    Khởi động task vòng lặp gửi nền

    Args:
        conn: Đối tượng kết nối
        rate_controller: Bộ điều khiển tốc độ
        flow_control: Trạng thái điều tiết luồng
    """

    async def send_callback(packet):
        # Kiểm tra có nên huỷ bỏ
        if conn.client_abort:
            raise asyncio.CancelledError("Client đã huỷ")

        conn.last_activity_time = time.time() * 1000
        await _do_send_audio(conn, packet, flow_control)

    # Dùng start_sending để khởi động vòng lặp nền
    rate_controller.start_sending(send_callback)


async def _send_audio_with_rate_control(
    conn: "ConnectionHandler", audio_list, rate_controller, flow_control, send_delay_ms
):
    """
    Dùng rate_controller gửi gói audio

    Args:
        conn: Đối tượng kết nối
        audio_list: Danh sách gói audio
        rate_controller: Bộ điều khiển tốc độ
        flow_control: Trạng thái điều tiết luồng
        send_delay_ms: Khoảng cách gửi tuỳ chỉnh (mili giây).
    """
    for packet in audio_list:
        if conn.client_abort:
            return

        conn.last_activity_time = time.time() * 1000

        if send_delay_ms > 0:
            # Chế độ nhịp tuỳ chỉnh: mọi gói vào hàng đợi gửi theo send_delay (tắt đệm trước)
            rate_controller.add_audio(packet)
        else:
            # Chế độ nhịp mặc định (60ms/gói): N gói đầu đệm trước gửi trực tiếp, các gói sau vào hàng đợi do task nền gửi theo nhịp interval_ms
            if flow_control["packet_count"] < PRE_BUFFER_COUNT:
                await _do_send_audio(conn, packet, flow_control)
            else:
                rate_controller.add_audio(packet)


async def _do_send_audio(conn: "ConnectionHandler", opus_packet, flow_control):
    """
    Thực hiện gửi audio thực tế
    """
    packet_index = flow_control.get("packet_count", 0)
    sequence = flow_control.get("sequence", 0)

    if conn.conn_from_mqtt_gateway:
        # Tính timestamp (dựa trên vị trí phát)
        start_time = time.time()
        timestamp = int(start_time * 1000) % (2**32)
        await _send_to_mqtt_gateway(conn, opus_packet, timestamp, sequence)
    else:
        # Gửi trực tiếp gói opus
        await conn.websocket.send(opus_packet)

    # Cập nhật trạng thái điều tiết luồng
    flow_control["packet_count"] = packet_index + 1
    flow_control["sequence"] = sequence + 1


async def send_tts_message(conn: "ConnectionHandler", state, text=None):
    """Gửi message trạng thái TTS"""
    if text is None and state == "sentence_start":
        return
    message = {"type": "tts", "state": state, "session_id": conn.session_id}
    if text is not None:
        message["text"] = textUtils.check_emoji(text)

    # Phát TTS kết thúc
    if state == "stop":
        # Lưu sentence_id hiện tại để về sau xác định có phải lượt hiện tại
        current_sentence_id = conn.sentence_id
        # Phát âm báo
        tts_notify = conn.config.get("enable_stop_tts_notify", False)
        if tts_notify:
            stop_tts_notify_voice = conn.config.get(
                "stop_tts_notify_voice", "config/assets/tts_notify.mp3"
            )
            audios = await audio_to_data(stop_tts_notify_voice, is_opus=True)
            await sendAudio(conn, audios)
        # Chờ mọi gói audio gửi xong
        await _wait_for_audio_completion(conn)

        # Kiểm tra có phải lượt hiện tại
        if current_sentence_id != conn.sentence_id:
            return

        # Dừng vòng lặp gửi audio (chỉ gọi khi bộ điều tiết luồng đã khởi tạo)
        if hasattr(conn, "audio_rate_controller") and conn.audio_rate_controller:
            conn.audio_rate_controller.stop_sending()
        conn.clearSpeakStatus()

    # Gửi message tới client
    await conn.websocket.send(json.dumps(message))


async def send_stt_message(conn: "ConnectionHandler", text):
    """Gửi message trạng thái STT"""
    end_prompt_str = conn.config.get("end_prompt", {}).get("prompt")
    if end_prompt_str and end_prompt_str == text:
        await send_tts_message(conn, "start")
        return

    # Phân tích định dạng JSON, trích xuất nội dung nói thực tế của người dùng
    display_text = text
    try:
        # Thử phân tích định dạng JSON
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                # Nếu là định dạng JSON chứa thông tin người nói, chỉ hiển thị phần content
                display_text = parsed_data["content"]
                # Lưu thông tin người nói vào đối tượng conn
                if "speaker" in parsed_data:
                    conn.current_speaker = parsed_data["speaker"]
    except (json.JSONDecodeError, TypeError):
        # Nếu không phải định dạng JSON, dùng trực tiếp text gốc
        display_text = text
    stt_text = textUtils.get_string_no_punctuation_or_emoji(display_text)
    await conn.websocket.send(
        json.dumps({"type": "stt", "text": stt_text, "session_id": conn.session_id})
    )
    await send_tts_message(conn, "start")
    # Sau khi gửi message start, client sẽ ở trạng thái đang nói, đồng bộ trạng thái phía server
    conn.client_is_speaking = True


async def send_display_message(conn: "ConnectionHandler", text):
    """Gửi message chỉ hiển thị"""
    message = {
        "type": "stt",
        "text": text,
        "session_id": conn.session_id
    }
    await conn.websocket.send(json.dumps(message))
