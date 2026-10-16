"""Test chuỗi Memory.

Chế độ Mock (mặc định): lấy cấu hình từ data/.config.yaml để khởi tạo provider thật;
các cuộc gọi SDK bên ngoài (client mem0ai / powermem) đi qua shim, không gửi request thật.
Chế độ Live (``RUN_LIVE_API_TESTS=1``): gọi Memory provider thật.

Quy ước interface (core/providers/memory/base.py):
    async save_memory(msgs, session_id=None) -> str
    async query_memory(query: str) -> str

Mặc định selected_module.Memory = nomem (không bật memory, chạy local là đủ).
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


def _build_provider():
    name = get_selected_provider_name("Memory")
    if not name:
        pytest.skip("selected_module.Memory chưa được đặt")
    cfg = get_provider_config("Memory", name)
    if not cfg:
        pytest.skip(f"Memory provider '{name}' thiếu cấu hình")
    ptype = get_provider_type("Memory", name)
    if not ptype:
        pytest.skip(f"Memory '{name}' thiếu trường type")
    from core.utils.memory import create_instance

    return create_instance(ptype, cfg, None)


def test_memory_provider_constructs_from_config() -> None:
    """Từ config phải khởi tạo được một subclass của MemoryProviderBase."""
    provider = _build_provider()
    from core.providers.memory.base import MemoryProviderBase

    assert isinstance(provider, MemoryProviderBase), (
        f"Memory provider sai kiểu: {type(provider).__name__}"
    )


def test_memory_provider_exposes_save_and_query() -> None:
    """Provider phải implement save_memory / query_memory."""
    provider = _build_provider()
    assert hasattr(provider, "save_memory"), (
        f"{type(provider).__name__} thiếu phương thức save_memory"
    )
    assert hasattr(provider, "query_memory"), (
        f"{type(provider).__name__} thiếu phương thức query_memory"
    )


def test_memory_save_and_query_roundtrip() -> None:
    """Sau save thì query không được ném exception; nomem trả về None/chuỗi rỗng cũng tính là pass."""
    provider = _build_provider()
    msgs = [{"role": "user", "content": "test message"}]

    async def run():
        mid = await provider.save_memory(msgs, "test-session")
        result = await provider.query_memory("test")
        return mid, result

    mid, result = asyncio.run(run())
    # Các implementation kiểu nomem "không bật memory" có thể trả về None;
    # dịch vụ memory thật phải trả về ID dạng str
    assert mid is None or isinstance(mid, str), (
        f"save_memory phải trả về str hoặc None, được {type(mid).__name__}"
    )
    assert isinstance(result, str), "query_memory phải trả về str"
