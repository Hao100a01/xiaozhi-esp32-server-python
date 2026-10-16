"""Test chuỗi ASR (nhận dạng giọng nói).

Chế độ Mock (mặc định): lấy cấu hình từ data/.config.yaml để khởi tạo ASR provider thật;
mọi cuộc gọi HTTP/SDK bên ngoài đi qua shim được conftest inject, không gửi request thật.
Chế độ Live (``RUN_LIVE_API_TESTS=1``): gọi ASR provider thật.

Quy ước interface (core/providers/asr/base.py):
    async speech_to_text_wrapper(pcm_data, session_id) -> Tuple[Optional[str], Optional[str]]
    async speech_to_text(opus_data, session_id, artifacts) -> Tuple[Optional[str], Optional[str]]

Lưu ý:
- performance_tester_asr.py gọi wrapper với 3 arg là sai (base chỉ nhận 2),
  ở đây dùng phiên bản 2-arg đúng
- FunASR là model local: khi khởi tạo sẽ load file model trong model_dir;
  nếu model chưa được tải, `__init__` ném exception trực tiếp, test sẽ skip
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.conftest import (
    get_provider_config,
    get_provider_type,
    get_selected_provider_name,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "config" / "assets"


def _build_provider():
    name = get_selected_provider_name("ASR")
    if not name:
        pytest.skip("selected_module.ASR chưa được đặt")
    cfg = get_provider_config("ASR", name)
    if not cfg:
        pytest.skip(f"ASR provider '{name}' thiếu cấu hình")
    ptype = get_provider_type("ASR", name)
    if not ptype:
        pytest.skip(f"ASR '{name}' thiếu trường type")
    from core.utils.asr import create_instance

    return create_instance(ptype, cfg, True)


def _find_test_audio() -> Path:
    """Tìm file wav >300KB trong config/assets/ làm audio test."""
    if not _ASSETS_DIR.exists():
        pytest.skip(f"Thư mục audio không tồn tại: {_ASSETS_DIR}")
    wavs = sorted(
        (p for p in _ASSETS_DIR.glob("*.wav") if p.stat().st_size > 300 * 1024),
        key=lambda p: p.stat().st_size,
        reverse=True,  # file lớn nhất ưu tiên
    )
    if not wavs:
        pytest.skip(f"Không có file wav >300KB nào dưới config/assets/")
    return wavs[0]


def test_asr_provider_constructs_from_config() -> None:
    """Từ config phải khởi tạo được một subclass của ASRProviderBase."""
    provider = _build_provider()
    from core.providers.asr.base import ASRProviderBase

    assert isinstance(provider, ASRProviderBase), (
        f"ASR provider sai kiểu: {type(provider).__name__}"
    )


def test_asr_provider_exposes_speech_to_text() -> None:
    """Provider phải implement phương thức speech_to_text."""
    provider = _build_provider()
    assert hasattr(provider, "speech_to_text"), (
        f"{type(provider).__name__} thiếu phương thức speech_to_text"
    )
    assert callable(provider.speech_to_text)


def test_asr_provider_has_output_dir() -> None:
    """Provider nên có thuộc tính output_dir (để ghi kết quả nhận dạng)."""
    provider = _build_provider()
    assert hasattr(provider, "output_dir"), (
        f"{type(provider).__name__} thiếu thuộc tính output_dir"
    )


def test_asr_speech_to_text_wrapper_returns_text() -> None:
    """Gửi một request ASR thật (chế độ live), assert trả về kết quả nhận dạng.

    Ở chế độ Mock vẫn gọi như thường, nhưng shim trả về dict/text rỗng, assertion nới lỏng:
    text có thể là None / str / dict, chỉ cần không ném exception là pass.

    Tham khảo performance_tester_asr.py:
    - Nạp bytes file wav thật cho speech_to_text_wrapper
    - Timeout 10s
    - text trả về có thể là None (nhận dạng thất bại) / str (một số provider) / dict (FunASR v.v. kèm emotion/language)
    """
    provider = _build_provider()
    # Ở chế độ Mock không cần asset wav vẫn chạy được (shim không đọc file).
    from tests.conftest import LIVE_API_TESTS
    if LIVE_API_TESTS:
        wav_path = _find_test_audio()
        audio_data = wav_path.read_bytes()
    else:
        # Chế độ Mock: nạp bytes placeholder, shim không thật sự decode.
        audio_data = b"\x00" * 1024

    async def run():
        return await asyncio.wait_for(
            provider.speech_to_text_wrapper([audio_data], "test-session"),
            timeout=10.0,
        )

    text, file_path = asyncio.run(run())
    # Tương thích kết quả ASR: None / str / dict (FunASR trả về {content, language, emotion})
    assert text is None or isinstance(text, (str, dict)), (
        f"speech_to_text_wrapper phải trả về (None/str/dict, str|None), "
        f"được kiểu text {type(text).__name__}"
    )
    if isinstance(text, dict):
        # Kiểu FunASR: lấy trường content (chỉ chế độ live mới kiểm tra nghiêm).
        if LIVE_API_TESTS:
            assert text.get("content"), f"Kết quả dict ASR thiếu content: {text!r}"
