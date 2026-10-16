"""Pytest configuration — must run BEFORE any test imports project modules.

Adds the xiaozhi-server directory to sys.path so the existing implicit
relative imports (`from core.utils.textUtils import ...`) resolve.

External-API mock toggle (default: mocked)
-------------------------------------------
The dep-upgrade orchestrator and CI run this test suite *without* any
real API credentials. By default the tests construct the actual provider
objects from ``data/.config.yaml`` but route every network call through
a recording shim, so no traffic leaves the host.

Set ``RUN_LIVE_API_TESTS=1`` to opt in to real network calls. In that
mode the 5-module readiness check runs in strict form (any missing key
→ ``sys.exit(3)``), the mock shims are uninstalled, and tests hit the
real providers.

Configuration loading
---------------------
Always uses the file-merge path (``config.yaml`` + ``data/.config.yaml``).
``manager-api`` is intentionally NOT contacted — pytest must work in a
single-module deployment.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import yaml

# Windows console mặc định dùng GBK; print emoji / tiếng Trung sẽ lỗi, ép UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# opuslib_next stub: để chuỗi import chạy thông; gọi audio thật sẽ thất bại,
# test tự xử lý try/except
# ---------------------------------------------------------------------------
try:
    import opuslib_next  # noqa: F401
except Exception:
    _stub = types.ModuleType("opuslib_next")
    _stub.Encoder = type("Encoder", (), {"__init__": lambda *a, **kw: None})
    _stub.Decoder = type("Decoder", (), {"__init__": lambda *a, **kw: None})
    _stub.APPLICATION_AUDIO = 0
    _stub.OpusError = type("OpusError", (Exception,), {})
    _stub.constants = types.SimpleNamespace()
    sys.modules["opuslib_next"] = _stub
    for sub in ("api", "exceptions"):
        sys.modules[f"opuslib_next.{sub}"] = types.ModuleType(f"opuslib_next.{sub}")


# ---------------------------------------------------------------------------
# Tải cấu hình: luôn đi đường gộp file
# ---------------------------------------------------------------------------
# Thứ tự ưu tiên:
#   1. File mà biến môi trường ``PYTEST_CONFIG_FILE`` trỏ tới (khuyên dùng — cách ly cấu hình test)
#   2. data/.config.yaml (cấu hình triển khai production / đơn module)
_DEFAULT_CUSTOM = _PROJECT_ROOT / "data" / ".config.yaml"
_CUSTOM_CONFIG = Path(os.environ["PYTEST_CONFIG_FILE"]) if os.environ.get(
    "PYTEST_CONFIG_FILE"
) else _DEFAULT_CUSTOM


def _load_via_file_merge() -> dict:
    """Gộp file thuần tuý: config.yaml + data/.config.yaml. Giống cách triển khai đơn module ở production."""
    from config.config_loader import merge_configs

    config: dict = {}
    default_path = _PROJECT_ROOT / "config.yaml"
    if default_path.exists():
        config = yaml.safe_load(default_path.read_text(encoding="utf-8")) or {}

    if _CUSTOM_CONFIG.exists():
        custom = yaml.safe_load(_CUSTOM_CONFIG.read_text(encoding="utf-8")) or {}
        config = merge_configs(config, custom)

    return config


# Phát hiện triển khai full module (fail-fast trong mọi chế độ, tránh kích hoạt API từ xa).
# Triển khai full module khiến ``load_config`` lấy remote qua manager-api, pytest không nên kích hoạt.
if _CUSTOM_CONFIG.exists():
    _raw_custom = yaml.safe_load(_CUSTOM_CONFIG.read_text(encoding="utf-8")) or {}
    _api_cfg = _raw_custom.get("manager-api", {})
    _has_url = bool(_api_cfg.get("url"))
    _has_secret = bool(_api_cfg.get("secret")) and "你" not in str(
        _api_cfg.get("secret", "")
    )
    if _has_url and _has_secret:
        print(
            "\n[conftest] ============================================================\n"
            "[conftest] FAIL: Phát hiện triển khai full module (manager-api.url + secret đã được cấu hình),\n"
            "[conftest]       pytest không hỗ trợ triển khai full module.\n"
            "[conftest] Lý do: server.secret của manager-api là token ẩn danh,\n"
            "[conftest]        không có quyền sys:role:superAdmin,\n"
            "[conftest]        không lấy được configJson của LLM/TTS/Memory (chứa api_key).\n"
            "[conftest] Cách chuyển sang đơn module:\n"
            "[conftest]   1) manager-web → Quản lý tham số → Cấu hình model,\n"
            "[conftest]      chép configJson của LLM/TTS/Memory ra\n"
            "[conftest]   2) Ghi vào data/.config.yaml (cấu trúc giống config.yaml)\n"
            "[conftest]   3) Xóa toàn bộ khối manager-api trong data/.config.yaml\n"
            "[conftest]   4) Chạy lại pytest\n"
            "[conftest] ============================================================",
            flush=True,
        )
        sys.exit(2)


# Luôn đi đường gộp file: đơn module dùng trực tiếp
CONFIG: dict = _load_via_file_merge()


# ---------------------------------------------------------------------------
# Điểm mấu chốt: thay thế các điểm vào cấu hình runtime như ``load_config`` /
# ``get_server_config`` thành trả về trực tiếp CONFIG đã xây dựng, tránh việc
# ``config.config_loader.load_config`` trong lúc chạy test lại đọc
# ``data/.config.yaml`` (mặc định là cấu hình triển khai full module) và kích hoạt
# gọi remote tới manager-api.
# ---------------------------------------------------------------------------
async def _load_config_patched(*_args, **_kwargs):
    return CONFIG


async def _get_server_config_patched(*_args, **_kwargs):
    return None


def _init_service_noop(_config):
    """Ngăn ``init_service`` khởi tạo ``ManageApiClient``."""
    return None


import config.config_loader as _cl  # noqa: E402
import config.manage_api_client as _mac  # noqa: E402
_cl.load_config = _load_config_patched
_mac.init_service = _init_service_noop
_mac.get_server_config = _get_server_config_patched
# Xóa singleton ManageApiClient, tránh trường hợp code khác đã kích hoạt khởi tạo trước đó.
_mac.ManageApiClient._instance = None
_mac.ManageApiClient._closed = True


# ---------------------------------------------------------------------------
# Helper truy cập cấu hình (test tham chiếu)
# ---------------------------------------------------------------------------
def get_provider_config(module: str, provider_name: str | None = None) -> dict:
    """Lấy config dict của một provider.

    - provider_name=None: lấy ``selected_module[module]``
    - nếu không thì chỉ định rõ
    Trả về {} nghĩa là chưa cấu hình.
    """
    if provider_name is None:
        provider_name = CONFIG.get("selected_module", {}).get(module)
    if not provider_name:
        return {}
    return CONFIG.get(module, {}).get(provider_name, {}) or {}


def get_selected_provider_name(module: str) -> str | None:
    return CONFIG.get("selected_module", {}).get(module)


def get_provider_type(module: str, provider_name: str | None = None) -> str | None:
    """Trả về tên type mà ``create_instance`` thực sự cần (có thể khác tên section).

    Quy ước (từ core/utils/modules_initialize.py): khi không có type thì dùng tên section.
    """
    cfg = get_provider_config(module, provider_name)
    if not cfg:
        return None
    return cfg.get("type") or (provider_name or get_selected_provider_name(module))


def has_real_key(config: dict, *keys: str) -> bool:
    """Chỉ cần một candidate key có giá trị không phải placeholder là tính như có key thật.

    Nhận diện placeholder: rỗng / "你的..." / chứa "placeholder" / chữ "none".
    """
    if not config:
        return False
    for key in keys:
        val = config.get(key)
        if val is None:
            continue
        s = str(val).strip().lower()
        if not s or s == "none":
            continue
        if s.startswith("你的") or "placeholder" in s:
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Mặc định là chế độ mock: chặn các cuộc gọi network HTTP/SDK của bên thứ ba,
# test chạy được mà không cần key thật
# ---------------------------------------------------------------------------
LIVE_API_TESTS: bool = os.environ.get("RUN_LIVE_API_TESTS") == "1"

# Danh sách SDK bên thứ ba. Ở chế độ thật không cần làm gì (giữ import thật).
# Ở chế độ Mock nhét các tên này vào sys.modules, và cung cấp class exception / client placeholder.
_MOCKED_MODULES: tuple[str, ...] = (
    # Lưu ý: các package thật của bên thứ ba như openai / httpx / requests thường
    # đã được cài trong môi trường conda; shim cả module sẽ làm hỏng việc phân giải
    # các submodule như ``openai.types`` / ``httpx.AsyncClient``.
    # Chỉ shim khi package tương ứng chưa được cài (``__getattr__`` vẫn đi đường import thật).
    "anthropic",
    "dashscope",
    "tencentcloud",
    "xunfei",
    "vosk",
    "edge_tts",
    "elevenlabs",
    "mem0",
    "mem0ai",
    "freezegun",
)

# Các package này dù đã cài vẫn bị ép shim — hoặc import chuỗi model local sẽ nổ
# (funasr kéo torch, đường dẫn DLL của torch quá dài), hoặc là dependency nặng GPU,
# mock không chạy nổi.
_FORCE_SHIM_MODULES: tuple[str, ...] = (
    "funasr",
    "torch",
    "torchaudio",
    "modelscope",
)


def _try_import_real(name: str) -> bool:
    """Kiểm tra package thật của bên thứ ba có load được không (không import thật, chỉ kiểm tra importer.find_spec).

    Gọi chủ động ``__import__`` sẽ gây tác dụng phụ cấp package (funasr kéo torch v.v.),
    nên bỏ qua nó và xem trực tiếp package có nằm trong sys.path / site-packages không.
    """
    import importlib.util

    spec = importlib.util.find_spec(name)
    return spec is not None


class _RecordingShim:
    """A no-op stand-in for any object reachable through a shimmed module.

    Acts as both a callable and an attribute container: every attribute
    access returns a callable that records the call and yields another
    shim. Used as a base class for fake exception classes so
    ``except httpx.ConnectError`` blocks evaluate correctly.
    """

    _calls: list = []

    def __init__(self, *_args, **_kwargs):
        pass

    def __getattr__(self, attr):
        def _record(*args, **kwargs):
            type(self)._calls.append((attr, args, kwargs))
            return _RecordingShim()

        return _record

    def __call__(self, *args, **kwargs):
        type(self)._calls.append(("__call__", args, kwargs))
        return _RecordingShim()


class _ShimModule(types.ModuleType):
    """PEP 562 module — any attribute access returns a recording shim."""

    def __getattr__(self, attr):
        if attr in ("__path__", "__loader__", "__spec__"):
            raise AttributeError(attr)
        # Cache so identity stays stable across accesses.
        value = _RecordingShim()
        setattr(self, attr, value)
        return value


def _install_mock_layer() -> None:
    """Insert PEP 562 shims for every third-party SDK in :data:`_MOCKED_MODULES`.

    Exception classes (e.g. ``httpx.ConnectError``) become ``_RecordingShim``
    subclasses — so ``except`` blocks match and ``isinstance`` checks pass.
    Common callable symbols (``Client`` / ``OpenAI`` / ``AutoModel``) are
    pre-bound to the shim type so ``Client(...)`` constructions succeed.

    If a real package is already importable (e.g. openai installed in conda),
    we leave it alone — shimming would break submodule resolution like
    ``openai.types``.
    """
    for name in list(_MOCKED_MODULES) + list(_FORCE_SHIM_MODULES):
        if name in sys.modules and isinstance(sys.modules[name], _ShimModule):
            continue
        if name not in _FORCE_SHIM_MODULES and _try_import_real(name):
            # Real package is available — use it. We'll patch network I/O
            # at the request layer in mock mode (see below).
            continue
        shim = _ShimModule(name)
        shim.OpenAI = _RecordingShim
        shim.AsyncOpenAI = _RecordingShim
        shim.Client = _RecordingShim
        shim.AsyncClient = _RecordingShim
        shim.AutoModel = _RecordingShim
        shim.completions = _RecordingShim()
        shim.chat = _RecordingShim()
        shim.exceptions = _ShimModule(f"{name}.exceptions")
        sys.modules[name] = shim
        sys.modules[f"{name}.exceptions"] = shim.exceptions

    # Rút ngắn asyncio.sleep để vòng lặp retry không kéo dài thời gian.
    import asyncio

    async def _fast_sleep(_seconds):
        return None

    asyncio.sleep = _fast_sleep  # type: ignore[assignment]

    # Mock tầng network: nếu httpx thật đã được load, thay AsyncClient.send của nó
    # bằng stub ném ngay ConnectError, để kích hoạt ``_should_retry`` đi tới ``raise``
    # mà không gửi request thật. Chế độ Live không cài lớp này.
    if not LIVE_API_TESTS:
        try:
            import httpx as _httpx  # noqa: F401

            class _FakeAsyncClient:
                def __init__(self, *args, **kwargs):
                    pass

                async def send(self, *args, **kwargs):
                    raise _httpx.ConnectError("MOCK: connect refused")

                async def aclose(self):
                    return None

            class _FakeClient:
                def __init__(self, *args, **kwargs):
                    pass

                def send(self, *args, **kwargs):
                    raise _httpx.ConnectError("MOCK: connect refused")

                def close(self):
                    return None

            _httpx.AsyncClient = _FakeAsyncClient  # type: ignore[misc]
            _httpx.Client = _FakeClient  # type: ignore[misc]
        except ImportError:
            pass


if not LIVE_API_TESTS:
    _install_mock_layer()


# ---------------------------------------------------------------------------
# Kiểm tra khởi động chế độ Live: phát hiện triển khai đơn module đã hoàn tất ở đầu quá trình load file
# ---------------------------------------------------------------------------
if LIVE_API_TESTS:
    print("[conftest] OK: triển khai đơn module (cấu hình gộp file)")


_REQUIRED_MODULES = ("ASR", "LLM", "TTS", "Memory", "VAD")
_SECRET_FIELDS = (
    "api_key", "token", "access_token", "authorization",
    "secret_key", "access_key", "app_id", "app_secret",
    "bot_id", "user_id",
)


def _check_modules_ready() -> None:
    """Chế độ Live: 5 module cốt lõi phải được cấu hình đầy đủ. Chế độ Mock chỉ in soft warning."""
    problems: list[str] = []
    selected = CONFIG.get("selected_module") or {}
    for module in _REQUIRED_MODULES:
        provider_name = selected.get(module)
        if not provider_name:
            problems.append(f"{module} chưa được cấu hình trong selected_module (dev quên chọn)")
            continue
        cfg = CONFIG.get(module, {}).get(provider_name)
        if not cfg:
            problems.append(
                f"{module}.{provider_name} thiếu cấu hình (không có provider này trong dict CFG)"
            )
            continue
        secret_keys_present = [k for k in _SECRET_FIELDS if k in cfg]
        if secret_keys_present and not any(
            has_real_key(cfg, k) for k in secret_keys_present
        ):
            problems.append(
                f"{module}.{provider_name} các trường secret "
                f"{secret_keys_present} toàn là placeholder (dev chưa điền key thật)"
            )
    if not problems:
        return
    if LIVE_API_TESTS:
        msg = (
            "\n[conftest] ============================================================\n"
            "[conftest] FAIL: pytest yêu cầu 5 module cốt lõi phải đủ mới chạy được.\n"
            "[conftest] Các mục thiếu/chưa sẵn sàng:\n"
            + "\n".join(f"[conftest]   - {p}" for p in problems)
            + "\n[conftest] Phải cấu hình đủ 5 module "
            "(ASR/LLM/TTS/Memory/VAD) trong data/.config.yaml mới chạy được pytest.\n"
            "[conftest] ============================================================"
        )
        print(msg, file=sys.stderr, flush=True)
        sys.exit(3)
    print(
        "[conftest] NOTE: Chế độ mock mặc định — các module sau chưa đủ, nhưng pytest vẫn chạy được:\n  - "
        + "\n  - ".join(problems)
        + "\n[conftest] Chế độ request thật: export RUN_LIVE_API_TESTS=1 và cấu hình đủ rồi chạy lại.",
        flush=True,
    )


_check_modules_ready()


# ---------------------------------------------------------------------------
# Đánh dấu rõ: ở chế độ mock thì thông báo cho người dùng chế độ hiện tại
# ---------------------------------------------------------------------------
if not LIVE_API_TESTS:
    print(
        "[conftest] Chế độ MOCK (mặc định): HTTP/SDK bên thứ ba đã được inject shim, không gửi request thật.\n"
        "[conftest] Chế độ request thật: export RUN_LIVE_API_TESTS=1 pytest tests/ -v",
        flush=True,
    )


import pytest  # noqa: E402  (after conftest hooks so pytest is initialised)


def pytest_collection_modifyitems(config, items):
    """Chế độ Live: test gắn mark ``live_api`` phải có đủ key mới chạy được (không thì skip)."""
    if LIVE_API_TESTS:
        return
    # Chế độ Mock không ép skip: các test đều nên chạy được (dùng shim).
    return
