import re
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

EMOTION_EMOJI_MAP = {
    "HAPPY": "🙂",
    "SAD": "😔",
    "ANGRY": "😡",
    "NEUTRAL": "😶",
    "FEARFUL": "😰",
    "DISGUSTED": "🤢",
    "SURPRISED": "😲",
    "EMO_UNKNOWN": "😶",  # Cảm xúc không xác định mặc định dùng biểu cảm trung tính
}
# EVENT_EMOJI_MAP = {
#     "<|BGM|>": "🎼",
#     "<|Speech|>": "",
#     "<|Applause|>": "👏",
#     "<|Laughter|>": "😀",
#     "<|Cry|>": "😭",
#     "<|Sneeze|>": "🤧",
#     "<|Breath|>": "",
#     "<|Cough|>": "🤧",
# }

def lang_tag_filter(text: str) -> dict:
    """
    Phân tích kết quả nhận dạng FunASR, trích xuất nhãn và nội dung văn bản thuần theo thứ tự

    Args:
        text: Văn bản gốc do ASR nhận dạng, có thể chứa nhiều loại nhãn

    Returns:
        dict: {"language": "zh", "emotion": "SAD", "emoji": "😔", "content": "Xin chào"} nếu có nhãn,
              {"content": "Văn bản thuần"} nếu không có nhãn

    Examples:
        Định dạng đầu ra FunASR: <|ngôn ngữ|><|cảm xúc|><|sự kiện|><|tuỳ chọn khác|>văn bản gốc
        >>> lang_tag_filter("<|zh|><|SAD|><|Speech|><|withitn|>Xin chào, kiểm tra kiểm tra.")
        {"language": "zh", "emotion": "SAD", "emoji": "😔", "content": "Xin chào, kiểm tra kiểm tra."}
        >>> lang_tag_filter("<|en|><|HAPPY|><|Speech|><|withitn|>Hello hello.")
        {"language": "en", "emotion": "HAPPY", "emoji": "🙂", "content": "Hello hello."}
        >>> lang_tag_filter("plain text")
        {"content": "plain text"}
    """
    # Trích xuất mọi nhãn (theo thứ tự)
    tag_pattern = r"<\|([^|]+)\|>"
    all_tags = re.findall(tag_pattern, text)

    # Xoá mọi nhãn định dạng <|...|>, lấy văn bản thuần
    clean_text = re.sub(tag_pattern, "", text).strip()
    # SenseVoice thỉnh thoảng xuất dấu cách theo từ với tiếng Nhật (như「今日 の こと を」), xoá dấu cách giữa các ký tự Trung/Nhật;
    # Dấu cách giữa các từ tiếng Hàn, tiếng Anh không bị ảnh hưởng
    clean_text = re.sub(
        r"(?<=[\u3000-\u303f\u3040-\u30ff\u4e00-\u9fff\uff01-\uff60])\s+"
        r"(?=[\u3000-\u303f\u3040-\u30ff\u4e00-\u9fff\uff01-\uff60])",
        "",
        clean_text,
    )

    # Giữ cấu trúc trả về nhất quán, tránh bên gọi nhầm văn bản thuần thành dict.
    if not all_tags:
        return {"content": clean_text}

    # Trích xuất nhãn theo thứ tự cố định của FunASR, trả về dict
    language = all_tags[0] if len(all_tags) > 0 else "zh"
    emotion = all_tags[1] if len(all_tags) > 1 else "NEUTRAL"
    # event = all_tags[2] if len(all_tags) > 2 else "Speech"  # Nhãn sự kiện tạm thời không dùng

    result = {
        "content": clean_text,
        "language": language,
        "emotion": emotion,
        # "event": event,
    }

    # Thêm ánh xạ emoji
    if emotion in EMOTION_EMOJI_MAP:
        result["emotion"] = EMOTION_EMOJI_MAP[emotion]
    # Nhãn sự kiện tạm thời không dùng
    # if event in EVENT_EMOJI_MAP:
    #     result["event"] = EVENT_EMOJI_MAP[event]

    return result
