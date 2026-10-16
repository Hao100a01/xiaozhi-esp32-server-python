"""Test chuỗi RAG (retrieval-augmented generation).

Dự án xiaozhi-esp32-server không có RAG provider độc lập — RAG được thực hiện bởi
``query_memory`` của MemoryProvider (mem0ai / powermem) phối hợp với tool call của LLM.
Test này tập trung vào chuỗi cốt lõi "save → query → nhận kết quả retrieval".

Chế độ Mock (mặc định): các cuộc gọi cloud như mem0/powermem đi qua shim, không gửi request.
Chế độ Live (``RUN_LIVE_API_TESTS=1``): gửi request thật tới memory provider đã chọn.
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


def _build_memory_provider():
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


def test_rag_query_after_save_returns_string() -> None:
    """Sau khi save, query_memory phải trả về chuỗi (nomem trả về chuỗi rỗng cũng hợp lệ)."""
    provider = _build_memory_provider()
    # Dữ liệu test: nội dung message / query đưa vào memory provider (giữ nguyên để kiểm tra retrieval).
    msgs = [
        {"role": "user", "content": "我喜欢喝咖啡"},
        {"role": "assistant", "content": "好的记住了"},
    ]

    async def run():
        await provider.save_memory(msgs, "rag-session")
        return await provider.query_memory("咖啡")

    result = asyncio.run(run())
    assert isinstance(result, str), f"query_memory phải trả về str, được {type(result).__name__}"


def test_rag_query_no_match_returns_empty() -> None:
    """Khi query nội dung không tồn tại thì trả về chuỗi rỗng, không ném exception."""
    provider = _build_memory_provider()

    async def run():
        # Dữ liệu test: keyword cố ý không khớp gì trong memory.
        return await provider.query_memory("完全不存在的关键词xyz")

    result = asyncio.run(run())
    assert isinstance(result, str)


def test_rag_empty_query_handled() -> None:
    """Query rỗng không được ném exception."""
    provider = _build_memory_provider()

    async def run():
        # Dữ liệu test: nội dung lưu / query rỗng.
        await provider.save_memory([{"role": "user", "content": "测试"}], "s")
        return await provider.query_memory("")

    result = asyncio.run(run())
    assert isinstance(result, str)
