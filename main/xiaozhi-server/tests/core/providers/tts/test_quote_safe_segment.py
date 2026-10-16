"""TTSProviderBase._get_segment_text must not cut sentences inside quotes."""
import pytest

from core.providers.tts.base import TTSProviderBase


# Stub tối thiểu: chỉ cần đáp ứng yêu cầu khởi tạo của abstract method;
# object.__new__ sẽ bỏ qua kiểm tra abstractmethod, nếu base class thêm
# abstract method mới thì chỗ này sẽ không tự báo lỗi — đúng như dự kiến
# (test không gọi tới)
class _TTS(TTSProviderBase):
    async def text_to_speak(self, text, output_file):
        return None


def _segments(text, step, first_sentence=False):
    tts = object.__new__(_TTS)
    tts.punctuations = ("。", "？", "?", "！", "!", "；", ";", "：")
    tts.first_sentence_punctuations = ("，", "~", "、", ",") + tts.punctuations
    tts.tts_text_buff, tts.processed_chars = [], 0
    tts.is_first_sentence, tts.tts_stop_request = first_sentence, False
    out = []
    for i in range(0, len(text), step):
        tts.tts_text_buff.append(text[i:i + step])
        segment = tts._get_segment_text()
        if segment:
            out.append(segment)
    tts.tts_stop_request = True
    rest = tts._get_segment_text()
    if rest:
        out.append(rest)
    return out


@pytest.mark.parametrize("step", [1, 3, 7])
def test_japanese_quote_followed_by_tte_stays_whole(step):
    assert _segments("思わず「わぁ！」って声が出たんだ。きみは？", step) == [
        "思わず「わぁ！」って声が出たんだ",
        "きみは？",
    ]


@pytest.mark.parametrize("step", [1, 4])
def test_chinese_quote_followed_by_speaker_stays_whole(step):
    segments = _segments("他大声说：“好！”然后笑了。", step)
    assert not any(seg.startswith("”") for seg in segments)
    # the leading “ is stripped by textUtils, the closing ” must stay with its sentence
    assert any("好！”然后笑了" in seg for seg in segments)


@pytest.mark.parametrize("step", [1, 5])
def test_closing_quote_then_period_is_not_left_alone(step):
    assert _segments("「すごい！」。次はね、雨！", step) == ["「すごい！」", "次はね、雨"]


@pytest.mark.parametrize("step", [1, 5])
def test_text_without_quotes_is_split_as_before(step):
    assert _segments("普通の文。次の文！最後", step) == ["普通の文", "次の文", "最後"]


@pytest.mark.parametrize("step", [1, 4])
def test_ascii_quote_at_stream_end_stays_whole(step):
    # Logic cũ cắt ở dấu ！, bỏ dấu " lẻ lại cho đầu đoạn sau; giờ đợi ngoặc đóng rồi xuất cả câu một lần
    assert _segments('She said, "Hi!"', step) == ['She said, "Hi!"']


_LQ, _RQ = "\u00ab", "\u00bb"        # « » dấu ngoặc kép kiểu tên sách của tiếng Pháp/Nga
_GOPEN, _GCLOSE = "\u201e", "\u201c"  # „ “ dấu ngoặc kép thấp kiểu Đức


@pytest.mark.parametrize("step", [1, 4])
def test_guillemets_are_not_split(step):
    # Dấu ！ nằm trong «», phải đợi ngoặc phải đóng xong mới cắt ở dấu câu cuối câu
    text = "Il a dit " + _LQ + "Bonjour ! " + _RQ + " puis est parti."
    assert _segments(text, step) == [text]


@pytest.mark.parametrize("step", [1, 4])
def test_german_low9_quotes_stay_whole(step):
    # Dấu đóng của „…“ (“) trùng với dấu mở ngoặc kép của tiếng Trung: “ luôn bị
    # cặp (“”, ””) tính là opener, nên sau khi „…“ đóng vẫn bị xem là mất cân bằng,
    # bất kể phía sau có dấu câu cuối câu hay không (thực nghiệm); tiếng Đức chỉ có
    # thể stop flush cả câu —— đây là kỳ vọng thận trọng khi xung đột ký tự, không phải lỗi
    text = "Er sagte " + _GOPEN + "Hallo!" + _GCLOSE + " und ging."
    assert _segments(text, step) == [text]
    # Đóng ngoặc + dấu chấm tiếng Trung cũng flush cả câu: đường chính tắc không tới được vì “ mang nghĩa kép
    text2 = "Er sagte " + _GOPEN + "Hallo!" + _GCLOSE + "。Dann geht er。"
    assert _segments(text2, step) == [text2]


@pytest.mark.parametrize("step", [1, 4])
def test_unterminated_quote_flushes_on_stop(step):
    # Một dấu ngoặc đơn lẻ (chưa đóng) không bị kẹt: trong khi stream thì chờ, lúc stop thì flush cả đoạn
    assert _segments('"Hi', step) == ['"Hi']
    assert _segments("「わぁ", step) == ["「わぁ"]


_LB, _RB = "\u3010", "\u3011"  # 【】


@pytest.mark.parametrize("step", [1, 4])
def test_brackets_with_punct_inside_are_not_split(step):
    # Dấu ！ nằm trong 【】 không được cắt (nếu cắt, đoạn đầu chỉ tới “他说【重要”),
    # phải đợi ngoặc phải đóng xong mới cắt ở dấu câu cuối câu
    assert _segments("他说" + _LB + "重要！步骤" + _RB + "。然后走了。", step) == [
        "他说" + _LB + "重要！步骤",
        "然后走了",
    ]


def test_nested_mixed_quotes_stay_whole():
    # Ngoặc lồng nhau được đếm độc lập theo từng cặp, không ảnh hưởng nhau
    assert _segments("“他说『好』了。”然后离开。", 1) == ["他说『好』了。”然后离开"]


def test_apostrophes_do_not_block_splitting():
    # Dấu nháy đơn dễ nhầm với dấu apostrophe (don't), không được đưa vào kiểm tra cân bằng khiến cả câu bị trễ
    # Ghi chú: đoạn flush (text còn lại lúc stop) xuất nguyên văn, không strip
    assert _segments("don't! It's fine.", 1) == ["don't", " It's fine."]


def test_first_sentence_still_splits_at_comma():
    assert _segments("そっか、実習で疲れたんだね。", 1, first_sentence=True)[0] == "そっか"
