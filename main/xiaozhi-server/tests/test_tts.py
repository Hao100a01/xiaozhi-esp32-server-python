"""Test chuỗi TTS (tổng hợp giọng nói).

Chế độ Mock (mặc định): lấy cấu hình từ data/.config.yaml để khởi tạo TTS provider thật;
mọi cuộc gọi HTTP/SDK bên ngoài đi qua shim được conftest inject, không gửi request thật.
Chế độ Live (``RUN_LIVE_API_TESTS=1``): gọi TTS provider thật.

Quy ước interface (core/providers/tts/base.py):
    text_to_speak(text, output_file) -> bất đồng bộ, ghi kết quả tổng hợp vào output_file

Lưu ý:
- 3 test khởi tạo không phụ thuộc cloud; test thứ 4 test_tts_text_to_speak_writes_nonempty_file
  mới là test chuỗi thật, sẽ gửi HTTP thật.
- Ở chế độ Mock: test thứ 4 assertion nới lỏng —— chỉ cần file tồn tại (shim tạo file rỗng),
  không bắt buộc size > 0. Ở chế độ Live kiểm tra nghiêm size > 0.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import (
    LIVE_API_TESTS,
    get_provider_config,
    get_provider_type,
    get_selected_provider_name,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _build_provider():
    name = get_selected_provider_name("TTS")
    if not name:
        pytest.skip("selected_module.TTS chưa được đặt")
    cfg = get_provider_config("TTS", name)
    if not cfg:
        pytest.skip(f"TTS provider '{name}' thiếu cấu hình")
    ptype = get_provider_type("TTS", name)
    if not ptype:
        pytest.skip(f"TTS '{name}' thiếu trường type")
    from core.utils.tts import create_instance

    return create_instance(ptype, cfg, True)


def test_tts_provider_constructs_from_config() -> None:
    """Từ config phải khởi tạo được một subclass của TTSProviderBase."""
    provider = _build_provider()
    from core.providers.tts.base import TTSProviderBase

    assert isinstance(provider, TTSProviderBase), (
        f"TTS provider sai kiểu: {type(provider).__name__}"
    )


def test_tts_provider_exposes_to_tts() -> None:
    """Provider phải implement một trong to_tts / to_tts_stream."""
    provider = _build_provider()
    has_sync = hasattr(provider, "to_tts") and callable(provider.to_tts)
    has_stream = hasattr(provider, "to_tts_stream") and callable(
        provider.to_tts_stream
    )
    assert has_sync or has_stream, (
        f"{type(provider).__name__} thiếu phương thức to_tts / to_tts_stream"
    )


def test_tts_provider_has_output_dir() -> None:
    """Provider nên có thuộc tính output_file (kết quả tổng hợp ghi vào đâu)."""
    provider = _build_provider()
    assert hasattr(provider, "output_file"), (
        f"{type(provider).__name__} thiếu thuộc tính output_file"
    )
    assert isinstance(provider.output_file, str)
    assert provider.output_file, "output_file không được rỗng"


def test_tts_text_to_speak_writes_file() -> None:
    """Chế độ Mock: chỉ cần assert file tồn tại. Chế độ Live: assert size > 0.

    Tham khảo implementation của performance_tester_tts.py:
    - mock conn (sample_rate/audio_format/stop_event/client_abort/headers)
      và opus_encoder (chỉ cần placeholder SimpleNamespace)
    - generate_filename() lấy đường dẫn output
    - text_to_speak() gọi cloud service bất đồng bộ, ghi file
    - Timeout 10s
    - Dùng xong xóa file test, tránh thư mục tmp/ tích tụ
    """
    provider = _build_provider()

    # Tham khảo performance_tester_tts.py: một số provider truy cập nội bộ self.conn.sample_rate v.v.
    if not hasattr(provider, "conn") or provider.conn is None:
        provider.conn = SimpleNamespace(
            sample_rate=16000,
            audio_format="pcm",
            stop_event=threading.Event(),
            client_abort=False,
            headers={},
        )
    if not hasattr(provider, "opus_encoder") or provider.opus_encoder is None:
        provider.opus_encoder = SimpleNamespace()

    out_file = provider.generate_filename()
    try:
        async def run():
            await asyncio.wait_for(
                # Dữ liệu test: text tiếng Trung đưa vào TTS để kiểm tra chuỗi tổng hợp giọng nói.
                provider.text_to_speak("测试一下", out_file),
                timeout=10.0,
            )

        asyncio.run(run())

        assert os.path.exists(out_file), f"File output không tồn tại: {out_file}"
        if LIVE_API_TESTS:
            assert os.path.getsize(out_file) > 0, f"File output rỗng: {out_file}"
    finally:
        if os.path.exists(out_file):
            try:
                os.remove(out_file)
            except OSError:
                pass
