import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
EMOJI_MAP = {
    "😂": "funny",
    "😭": "crying",
    "😠": "angry",
    "😔": "sad",
    "😍": "loving",
    "😲": "surprised",
    "😱": "shocked",
    "🤔": "thinking",
    "😌": "relaxed",
    "😴": "sleepy",
    "😜": "silly",
    "🙄": "confused",
    "😶": "neutral",
    "🙂": "happy",
    "😆": "laughing",
    "😳": "embarrassed",
    "😉": "winking",
    "😎": "cool",
    "🤤": "delicious",
    "😘": "kissy",
    "😏": "confident",
}
EMOJI_RANGES = [
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
]


def get_string_no_punctuation_or_emoji(s):
    """Loại bỏ khoảng trắng, dấu câu và emoji ở đầu/cuối chuỗi"""
    chars = list(s)
    # Xử lý ký tự ở đầu
    start = 0
    while start < len(chars) and is_punctuation_or_emoji(chars[start]):
        start += 1
    # Xử lý ký tự ở cuối
    end = len(chars) - 1
    while end >= start and is_punctuation_or_emoji(chars[end]):
        end -= 1
    return "".join(chars[start : end + 1])


def is_punctuation_or_emoji(char):
    """Kiểm tra ký tự có phải khoảng trắng, dấu câu chỉ định hoặc emoji"""
    # Định nghĩa dấu câu Trung/Anh cần loại bỏ (gồm full-width/half-width)
    punctuation_set = {
        "，",
        ",",  # Dấu phẩy tiếng Trung + dấu phẩy tiếng Anh
        "。",
        ".",  # Dấu chấm tiếng Trung + dấu chấm tiếng Anh
        "！",
        "!",  # Dấu chấm than tiếng Trung + dấu chấm than tiếng Anh
        "“",
        "”",
        '"',  # Dấu ngoặc kép tiếng Trung + dấu ngoặc kép tiếng Anh
        "：",
        ":",  # Dấu hai chấm tiếng Trung + dấu hai chấm tiếng Anh
        "-",
        "－",  # Dấu gạch nối tiếng Anh + dấu gạch ngang full-width tiếng Trung
        "、",  # Dấu chấm phẩy liệt kê tiếng Trung (、)
        "[",
        "]",  # Ngoặc vuông
        "【",
        "】",  # Ngoặc vuông tiếng Trung
    }
    if char.isspace() or char in punctuation_set:
        return True
    return is_emoji(char)


async def get_emotion(conn: "ConnectionHandler", text):
    """Lấy message cảm xúc trong văn bản"""
    emoji = "🙂"
    emotion = "happy"
    for char in text:
        if char in EMOJI_MAP:
            emoji = char
            emotion = EMOJI_MAP[char]
            break
    try:
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"Gửi emoji cảm xúc thất bại, lỗi:{e}")
    return


def is_emoji(char):
    """Kiểm tra ký tự có phải emoji"""
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in EMOJI_RANGES)


def check_emoji(text):
    """Loại bỏ mọi emoji trong văn bản"""
    return "".join(char for char in text if not is_emoji(char) and char != "\n")
