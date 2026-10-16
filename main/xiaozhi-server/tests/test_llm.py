"""Test chuỗi LLM.

Chế độ Mock (mặc định): lấy cấu hình từ data/.config.yaml để khởi tạo provider thật,
các cuộc gọi network đi qua shim được conftest inject —— không gửi request nào.
Chế độ Live (``RUN_LIVE_API_TESTS=1``): gọi LLM provider thật, tham khảo mẫu
ThreadPoolExecutor + timeout 10s của ``performance_tester_llm.py``.

Quy ước interface (core/providers/llm/base.py):
    response(session_id, dialogue) -> Iterator[str], generator stream đồng bộ
    response_no_stream(system_prompt, user_content) -> str

Xử lý đặc biệt:
- CozeLLM xem bot_id / user_id chứ không phải api_key
- Ollama cần model_name + dịch vụ local khả dụng
- Phát hiện placeholder ("你的" / "placeholder" / "xxx" / "sk-xxx")
- Vì LLM là generator đồng bộ, không thể asyncio.wait_for trực tiếp → dùng thread pool
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

import pytest

from tests.conftest import (
    CONFIG,
    LIVE_API_TESTS,
    get_provider_config,
    get_provider_type,
    get_selected_provider_name,
    has_real_key,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _build_provider():
    name = get_selected_provider_name("LLM")
    if not name:
        pytest.skip("selected_module.LLM chưa được đặt")
    cfg = get_provider_config("LLM", name)
    if not cfg:
        pytest.skip(f"LLM provider '{name}' thiếu cấu hình")

    # Mọi chế độ: key là placeholder → skip. Request thật cần key thật; ở chế độ
    # mock nếu không có key thật mà chạy tiếp cũng sẽ va vào network stack của SDK
    # thật (openai/edge_tts) và báo lỗi.
    if name == "CozeLLM":
        if any(x in cfg.get("bot_id", "") for x in ["你的"]) or any(
            x in cfg.get("user_id", "") for x in ["你的"]
        ):
            pytest.skip("LLM 'CozeLLM' chưa cấu hình bot_id/user_id")
    elif name == "Ollama":
        if not cfg.get("model_name"):
            pytest.skip("LLM 'Ollama' chưa cấu hình model_name")
    elif not has_real_key(cfg, "api_key"):
        if LIVE_API_TESTS:
            pytest.skip(f"LLM '{name}' chưa cấu hình api_key")
        else:
            pytest.skip(
                f"LLM '{name}' api_key là placeholder (chế độ mock sẽ không gửi request thật)."
                "Chế độ request thật: export RUN_LIVE_API_TESTS=1 và điền key thật rồi chạy lại."
            )

    ptype = get_provider_type("LLM", name)
    if not ptype:
        pytest.skip(f"LLM '{name}' thiếu trường type")
    from core.utils.llm import create_instance

    return create_instance(ptype, cfg)


def _collect_with_timeout(provider, session_id: str, dialogue, timeout: float = 10.0):
    """Gọi generator đồng bộ response(), dùng thread pool với timeout 10s."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: list(provider.response(session_id, dialogue)))
        return future.result(timeout=timeout)


def test_llm_response_streams_non_empty() -> None:
    """response(session_id, dialogue) phải sinh ra luồng token không rỗng."""
    provider = _build_provider()
    # Dữ liệu test: nội dung dialogue đưa vào LLM provider.
    dialogue = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "用一句话说 hi"},
    ]
    chunks = _collect_with_timeout(provider, "test-session", dialogue, timeout=10.0)

    full = "".join(chunks).strip()
    assert isinstance(full, str), f"LLM chunks join phải là str, được {type(full).__name__}"
    assert full, f"LLM trả về response rỗng (provider={type(provider).__name__})"


def test_llm_response_no_stream_returns_string() -> None:
    """response_no_stream phải trả về chuỗi không rỗng."""
    provider = _build_provider()
    # Dữ liệu test: system prompt / user content đưa vào provider.
    text = provider.response_no_stream("你是助手", "介绍一下北京").strip()

    assert isinstance(text, str), f"response_no_stream phải trả về str, được {type(text).__name__}"
    assert text, "response_no_stream trả về chuỗi rỗng"


def test_llm_dialogue_min_length_one_user() -> None:
    """Dialogue có ít nhất một user message phải chạy thông."""
    provider = _build_provider()
    dialogue = [{"role": "user", "content": "ping"}]
    chunks = _collect_with_timeout(provider, "s1", dialogue, timeout=10.0)

    assert "".join(chunks).strip(), "Một user message phải trả về nội dung"
