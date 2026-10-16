import os
import re
import time
import random
import difflib
import traceback
from pathlib import Path
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

MUSIC_CACHE = {}

play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": "Khi người dùng yêu cầu phát nhạc, bài hát thì gọi.",
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "Tên bài hát; nếu người dùng không chỉ định tên bài cụ thể thì là 'random', khi chỉ định rõ thì trả về tên của bài nhạc. Ví dụ: ```người dùng: phát bài 两只老虎\ntham số: 两只老虎``` ```người dùng: phát nhạc\ntham số: random ```",
                }
            },
            "required": ["song_name"],
        },
    },
}


@register_function("play_music", play_music_function_desc, ToolType.SYSTEM_CTL)
async def play_music(conn: "ConnectionHandler", song_name: str):
    try:
        music_intent = (
            f"播放音乐 {song_name}" if song_name != "random" else "随机播放音乐"
        )
        await handle_music_command(conn, music_intent)
        return ActionResponse(
            action=Action.RECORD, result="Đã nhận lệnh", response="Đang phát nhạc cho bạn"
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"Lỗi xử lý ý định âm nhạc: {e}")
        return ActionResponse(
            action=Action.RESPONSE, result=str(e), response="Đã xảy ra lỗi khi phát nhạc"
        )


def _extract_song_name(text):
    """Trích xuất tên bài hát từ đầu vào của người dùng"""
    for keyword in ["播放音乐"]:
        if keyword in text:
            parts = text.split(keyword)
            if len(parts) > 1:
                return parts[1].strip()
    return None


def _find_best_match(potential_song, music_files):
    """Tìm bài hát khớp nhất"""
    best_match = None
    highest_ratio = 0

    for music_file in music_files:
        song_name = os.path.splitext(music_file)[0]
        ratio = difflib.SequenceMatcher(None, potential_song, song_name).ratio()
        if ratio > highest_ratio and ratio > 0.4:
            highest_ratio = ratio
            best_match = music_file
    return best_match


def get_music_files(music_dir, music_ext):
    music_dir = Path(music_dir)
    music_files = []
    music_file_names = []
    for file in music_dir.rglob("*"):
        # Kiểm tra có phải file không
        if file.is_file():
            # Lấy phần mở rộng của file
            ext = file.suffix.lower()
            # Kiểm tra phần mở rộng có trong danh sách không
            if ext in music_ext:
                # Thêm đường dẫn tương đối
                music_files.append(str(file.relative_to(music_dir)))
                music_file_names.append(
                    os.path.splitext(str(file.relative_to(music_dir)))[0]
                )
    return music_files, music_file_names


def initialize_music_handler(conn: "ConnectionHandler"):
    global MUSIC_CACHE
    if MUSIC_CACHE == {}:
        plugins_config = conn.config.get("plugins", {})
        if "play_music" in plugins_config:
            MUSIC_CACHE["music_config"] = plugins_config["play_music"]
            MUSIC_CACHE["music_dir"] = os.path.abspath(
                MUSIC_CACHE["music_config"].get("music_dir", "./music")  # Đường dẫn mặc định đã sửa
            )
            MUSIC_CACHE["music_ext"] = MUSIC_CACHE["music_config"].get(
                "music_ext", (".mp3", ".wav", ".p3")
            )
            MUSIC_CACHE["refresh_time"] = MUSIC_CACHE["music_config"].get(
                "refresh_time", 60
            )
        else:
            MUSIC_CACHE["music_dir"] = os.path.abspath("./music")
            MUSIC_CACHE["music_ext"] = (".mp3", ".wav", ".p3")
            MUSIC_CACHE["refresh_time"] = 60
        # Lấy danh sách file nhạc
        MUSIC_CACHE["music_files"], MUSIC_CACHE["music_file_names"] = get_music_files(
            MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"]
        )
        MUSIC_CACHE["scan_time"] = time.time()
    return MUSIC_CACHE


async def handle_music_command(conn: "ConnectionHandler", text):
    initialize_music_handler(conn)
    global MUSIC_CACHE

    """Xử lý lệnh phát nhạc"""
    clean_text = re.sub(r"[^\w\s]", "", text).strip()
    conn.logger.bind(tag=TAG).debug(f"Kiểm tra có phải lệnh nhạc không: {clean_text}")

    # Thử khớp tên bài hát cụ thể
    if os.path.exists(MUSIC_CACHE["music_dir"]):
        if time.time() - MUSIC_CACHE["scan_time"] > MUSIC_CACHE["refresh_time"]:
            # Làm mới danh sách file nhạc
            MUSIC_CACHE["music_files"], MUSIC_CACHE["music_file_names"] = (
                get_music_files(MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"])
            )
            MUSIC_CACHE["scan_time"] = time.time()

        potential_song = _extract_song_name(clean_text)
        if potential_song:
            best_match = _find_best_match(potential_song, MUSIC_CACHE["music_files"])
            if best_match:
                conn.logger.bind(tag=TAG).info(f"Tìm thấy bài hát khớp nhất: {best_match}")
                await play_local_music(conn, specific_file=best_match)
                return True
    # Kiểm tra có phải lệnh phát nhạc chung không
    await play_local_music(conn)
    return True


def _get_random_play_prompt(song_name):
    """Tạo câu dẫn phát ngẫu nhiên"""
    # Xóa phần mở rộng của file
    clean_name = os.path.splitext(song_name)[0]
    prompts = [
        f"Đang phát cho bạn, bài «{clean_name}»",
        f"Mời bạn thưởng thức bài hát «{clean_name}»",
        f"Sắp phát cho bạn, bài «{clean_name}»",
        f"Giờ đây gửi đến bạn, bài «{clean_name}»",
        f"Hãy cùng nhau lắng nghe, bài «{clean_name}»",
        f"Tiếp theo mời bạn thưởng thức, bài «{clean_name}»",
        f"Ngay lúc này gửi tặng bạn, bài «{clean_name}»",
    ]
    # Dùng trực tiếp random.choice, không đặt seed
    return random.choice(prompts)


async def play_local_music(conn: "ConnectionHandler", specific_file=None):
    global MUSIC_CACHE
    """Phát file nhạc local"""
    try:
        if not os.path.exists(MUSIC_CACHE["music_dir"]):
            conn.logger.bind(tag=TAG).error(
                f"Thư mục nhạc không tồn tại: " + MUSIC_CACHE["music_dir"]
            )
            return

        # Đảm bảo tính đúng đắn của đường dẫn
        if specific_file:
            selected_music = specific_file
            music_path = os.path.join(MUSIC_CACHE["music_dir"], specific_file)
        else:
            if not MUSIC_CACHE["music_files"]:
                conn.logger.bind(tag=TAG).error("Không tìm thấy file nhạc MP3")
                return
            selected_music = random.choice(MUSIC_CACHE["music_files"])
            music_path = os.path.join(MUSIC_CACHE["music_dir"], selected_music)

        if not os.path.exists(music_path):
            conn.logger.bind(tag=TAG).error(f"File nhạc đã chọn không tồn tại: {music_path}")
            return
        text = _get_random_play_prompt(selected_music)
        conn.tts.store_tts_text(conn.sentence_id, text)
        # conn.dialogue.put(Message(role="assistant", content=text))

        if conn.intent_type == "intent_llm":
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=text,
            )
        )
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.FILE,
                content_file=music_path,
            )
        )
        if conn.intent_type == "intent_llm":
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )

    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"Phát nhạc thất bại: {str(e)}")
        conn.logger.bind(tag=TAG).error(f"Lỗi chi tiết: {traceback.format_exc()}")
