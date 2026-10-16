from enum import Enum
from typing import Union, Optional


class SentenceType(Enum):
    # Giai đoạn nói
    FIRST = "FIRST"  # Câu đầu tiên
    MIDDLE = "MIDDLE"  # Đang nói
    LAST = "LAST"  # Câu cuối cùng


class ContentType(Enum):
    # Kiểu nội dung
    TEXT = "TEXT"  # Nội dung văn bản
    FILE = "FILE"  # Nội dung tệp
    ACTION = "ACTION"  # Nội dung hành động


class InterfaceType(Enum):
    # Kiểu giao diện
    DUAL_STREAM = "DUAL_STREAM"  # Hai luồng
    SINGLE_STREAM = "SINGLE_STREAM"  # Một luồng
    NON_STREAM = "NON_STREAM"  # Không streaming


class TTSMessageDTO:
    def __init__(
        self,
        sentence_id: str,
        # Giai đoạn nói
        sentence_type: SentenceType,
        # Kiểu nội dung
        content_type: ContentType,
        # Chi tiết nội dung, thường là văn bản cần chuyển hoặc lời bài hát của âm thanh
        content_detail: Optional[str] = None,
        # Nếu kiểu nội dung là tệp, cần truyền đường dẫn tệp
        content_file: Optional[str] = None,
    ):
        self.sentence_id = sentence_id
        self.sentence_type = sentence_type
        self.content_type = content_type
        self.content_detail = content_detail
        self.content_file = content_file
