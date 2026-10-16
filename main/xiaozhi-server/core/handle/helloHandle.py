import time
import json
import uuid
import random
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.utils.util import audio_to_data
from core.providers.tts.dto.dto import SentenceType
from core.utils.wakeup_word import WakeupWordsConfig
from core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
from core.utils.util import remove_punctuation_and_length, opus_datas_to_wav_bytes
from core.providers.tools.device_mcp import MCPClient, send_mcp_initialize_message

TAG = __name__

WAKEUP_CONFIG = {
    "refresh_time": 10,
    "responses": [
        "Tôi vẫn luôn ở đây, bạn cứ nói đi.",
        "Có tôi đây, bạn cứ việc sai bảo nhé.",
        "Tới đây tới đây, bạn cứ nói đi.",
        "Bạn cứ nói, tôi đang nghe đây.",
        "Mời bạn nói, tôi đã sẵn sàng.",
        "Mời bạn ra lệnh đi.",
        "Tôi đang nghe chăm chú đây, mời bạn nói.",
        "Bạn cần giúp gì nào?",
        "Tôi ở đây, đang chờ lệnh của bạn.",
    ],
}

# Tạo bộ quản lý cấu hình từ đánh thức toàn cục
wakeup_words_config = WakeupWordsConfig()

# Lock chống gọi đồng thời wakeupWordsResponse
_wakeup_response_lock = asyncio.Lock()


async def handleHelloMessage(conn: "ConnectionHandler", msg_json):
    """Xử lý message hello"""
    audio_params = msg_json.get("audio_params")
    if audio_params:
        format = audio_params.get("format")
        conn.logger.bind(tag=TAG).debug(f"Định dạng audio của client: {format}")
        conn.audio_format = format
        conn.welcome_msg["audio_params"] = audio_params
    features = msg_json.get("features")
    if features:
        conn.logger.bind(tag=TAG).debug(f"Tính năng của client: {features}")
        conn.features = features
        if features.get("mcp"):
            conn.logger.bind(tag=TAG).debug("Client hỗ trợ MCP")
            conn.mcp_client = MCPClient()
        if features.get("aec"):
            conn.logger.bind(tag=TAG).debug("Client đã bật AEC phía server")
            conn.client_aec = True

    await conn.websocket.send(json.dumps(conn.welcome_msg))

    # The device waits for the server hello before processing MCP messages.
    if features and features.get("mcp"):
        asyncio.create_task(send_mcp_initialize_message(conn))


async def checkWakeupWords(conn: "ConnectionHandler", text):
    enable_wakeup_words_response_cache = conn.config[
        "enable_wakeup_words_response_cache"
    ]

    # Chờ TTS khởi tạo, tối đa 3 giây
    start_time = time.time()
    while time.time() - start_time < 3:
        if conn.tts:
            break
        await asyncio.sleep(0.1)
    else:
        return False

    if not enable_wakeup_words_response_cache:
        return False

    _, filtered_text = remove_punctuation_and_length(text)
    if filtered_text not in conn.config.get("wakeup_words"):
        return False

    conn.just_woken_up = True
    await send_tts_message(conn, "start")

    # Lấy voice hiện tại
    voice = getattr(conn.tts, "voice", "default")
    if not voice:
        voice = "default"

    # Lấy cấu hình trả lời từ đánh thức
    response = wakeup_words_config.get_wakeup_response(voice)
    if not response or not response.get("file_path"):
        response = {
            "voice": "default",
            "file_path": "config/assets/wakeup_words_short.wav",
            "time": 0,
            "text": "Tôi ở đây nè!",
        }

    # Lấy dữ liệu audio
    opus_packets = await audio_to_data(response.get("file_path"), use_cache=False)
    # Phát câu trả lời từ đánh thức
    conn.client_abort = False

    # Coi câu trả lời từ đánh thức như phiên mới, sinh sentence_id mới, đảm bảo bộ điều tiết luồng được đặt lại
    conn.sentence_id = str(uuid.uuid4().hex)

    conn.logger.bind(tag=TAG).info(f"Phát câu trả lời từ đánh thức: {response.get('text')}")
    await sendAudioMessage(conn, SentenceType.FIRST, opus_packets, response.get("text"))
    await sendAudioMessage(conn, SentenceType.LAST, [], None)

    # Bổ sung hội thoại
    conn.dialogue.put(Message(role="assistant", content=response.get("text")))

    # Kiểm tra có cần cập nhật câu trả lời từ đánh thức
    if time.time() - response.get("time", 0) > WAKEUP_CONFIG["refresh_time"]:
        if not _wakeup_response_lock.locked():
            asyncio.create_task(wakeupWordsResponse(conn))
    return True


async def wakeupWordsResponse(conn: "ConnectionHandler"):
    if not conn.tts:
        return

    try:
        # Thử lấy lock, không lấy được thì trả về
        if not await _wakeup_response_lock.acquire():
            return

        # Chọn ngẫu nhiên một câu trả lời từ danh sách trả lời định sẵn
        result = random.choice(WAKEUP_CONFIG["responses"])
        if not result or len(result) == 0:
            return

        # Sinh audio TTS
        tts_result = await asyncio.to_thread(conn.tts.to_tts, result)
        if not tts_result:
            return

        # Lấy voice hiện tại
        voice = getattr(conn.tts, "voice", "default")

        # Dùng sample_rate của liên kết
        wav_bytes = opus_datas_to_wav_bytes(tts_result, sample_rate=conn.sample_rate)
        file_path = wakeup_words_config.generate_file_path(voice)
        with open(file_path, "wb") as f:
            f.write(wav_bytes)
        # Cập nhật cấu hình
        wakeup_words_config.update_wakeup_response(voice, file_path, result)
    finally:
        # Đảm bảo giải phóng lock trong mọi trường hợp
        if _wakeup_response_lock.locked():
            _wakeup_response_lock.release()
