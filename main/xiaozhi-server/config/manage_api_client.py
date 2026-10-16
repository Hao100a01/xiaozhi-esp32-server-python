import os
import base64
import asyncio
import functools
import threading
import traceback
from typing import Optional, Dict

import httpx

TAG = __name__

# Độ mịn slice của _retry_wait: sau safe_close kết thúc trong ~100ms, chi phí polling không đáng kể
_RETRY_WAIT_SLICE = 0.1


class DeviceNotFoundException(Exception):
    pass


class DeviceBindException(Exception):
    def __init__(self, bind_code):
        self.bind_code = bind_code
        super().__init__(f"Ngoại lệ binding thiết bị, mã binding: {bind_code}")


class ManageApiShutdownError(Exception):
    """Ngoại lệ kết thúc sau khi safe_close ngắt chờ retry, __cause__ giữ nguyên nhân thất bại cuối cùng."""


class ManageApiClient:
    _instance = None
    _instance_lock = threading.Lock()  # Bảo vệ việc đọc/ghi _instance và _closed
    _secret = None
    _closed = False  # Đặt True sau safe_close(), sau khi đóng không tạo kết nối mới nữa

    def __new__(cls, config):
        """Singleton đảm bảo instance duy nhất toàn cục, và hỗ trợ truyền tham số cấu hình"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._init_client(config)
            return cls._instance

    @classmethod
    def _init_client(cls, config):
        """Khởi tạo cấu hình (tạo client lazy)"""
        cls.config = config.get("manager-api")

        if not cls.config:
            raise Exception("Lỗi cấu hình manager-api")

        if not cls.config.get("url") or not cls.config.get("secret"):
            raise Exception("Cấu hình url hoặc secret của manager-api bị sai")

        if "你" in cls.config.get("secret"):
            raise Exception("Vui lòng cấu hình secret của manager-api trước")

        cls._secret = cls.config.get("secret")
        cls.max_retries = cls.config.get("max_retries", 6)  # Số lần retry tối đa
        cls.retry_delay = cls.config.get("retry_delay", 10)  # Delay retry ban đầu (giây)
        # Không tạo AsyncClient ở đây, hoãn đến khi dùng thực tế mới tạo
        cls._closed = False

    @classmethod
    def _build_client(cls) -> httpx.AsyncClient:
        """Dựng AsyncClient chuyên dụng cho một request nghiệp vụ (gồm retry).

        Ràng buộc: do caller đóng bằng ``async with`` trong cùng một loop, cấm cache xuyên request; việc dựng thực hiện ngoài lock, tránh các request đồng thời block event loop.
        """
        with cls._instance_lock:
            if cls._closed:
                raise Exception("ManageApiClient đã đóng, không tạo HTTP client mới nữa")
            base_url = cls.config.get("url")
            secret = cls._secret
            timeout = cls.config.get("timeout", 30)
        return httpx.AsyncClient(
            base_url=base_url,
            headers={
                "User-Agent": f"PythonClient/2.0 (PID:{os.getpid()})",
                "Accept": "application/json",
                "Authorization": "Bearer " + secret,
            },
            timeout=timeout,
            # Tái dùng kết nối chỉ giới hạn trong request này; keepalive_expiry ghi đè động retry_delay,
            # ngăn kết nối trong pool hết hạn trong lúc chờ retry.
            limits=httpx.Limits(
                max_keepalive_connections=1,
                keepalive_expiry=max(30, cls.retry_delay + 5),
            ),
            trust_env=False,
            # verify=True tạo context ngay lúc này: httpcore khi thiết lập kết nối sẽ ghi đè tại chỗ context được truyền vào
            # (không khóa), singleton dùng chung khi handshake đồng thời sẽ có race.
            verify=True,
        )

    @classmethod
    async def _async_request(
        cls, client: httpx.AsyncClient, method: str, endpoint: str, **kwargs
    ) -> Dict:
        """Thực hiện một request duy nhất trên client đã cho; sau khi đọc response thì giải phóng ngay về connection pool."""
        endpoint = endpoint.lstrip("/")
        response = None
        try:
            response = await client.request(method, endpoint, **kwargs)
            response.raise_for_status()
            result = response.json()
            if result.get("code") == 10041:
                raise DeviceNotFoundException(result.get("msg"))
            elif result.get("code") == 10042:
                raise DeviceBindException(result.get("msg"))
            elif result.get("code") != 0:
                raise Exception(f"API trả về lỗi: {result.get('msg', 'Lỗi không xác định')}")
            return result.get("data")
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    # Ràng buộc: phải nuốt exception khi giải phóng, tránh đè lên exception nghiệp vụ, phá vỡ phán đoán retry
                    pass

    @classmethod
    def _should_retry(cls, exception: Exception) -> bool:
        """Đánh giá exception có nên retry không"""
        # Lỗi liên quan kết nối mạng
        if isinstance(
            exception, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)
        ):
            return True

        # RPE chỉ retry biến thể "without sending a response" (response chưa bắt đầu, an toàn);
        # biến thể bị cắt có thể đã xử lý request và sẽ submit trùng, khi văn bản thay đổi thì thoái hóa thành không retry.
        if isinstance(exception, httpx.RemoteProtocolError):
            return "without sending a response" in str(exception)

        # Lỗi mã trạng thái HTTP
        if isinstance(exception, httpx.HTTPStatusError):
            status_code = exception.response.status_code
            return status_code in [408, 429, 500, 502, 503, 504]

        return False

    @classmethod
    async def _retry_wait(cls):
        """Chờ retry có thể bị safe_close() ngắt (polling _closed theo slice 100ms).

        Ràng buộc: không được ngủ một mạch hết retry_delay, nếu không sau khi đóng, việc kết thúc sẽ kéo dài hết cả chu kỳ retry.
        """
        remaining = cls.retry_delay
        while remaining > 0 and not cls._closed:
            step = min(_RETRY_WAIT_SLICE, remaining)
            await asyncio.sleep(step)
            remaining -= step

    @classmethod
    async def _execute_async_request(cls, method: str, endpoint: str, **kwargs) -> Dict:
        """Bộ thực thi request nghiệp vụ có cơ chế retry.

        Ràng buộc: client chỉ được dựng một lần trong toàn bộ quá trình retry, sau khi xong do ``async with`` đóng; safe_close không hủy attempt đã bắt đầu, chỉ ngắt chờ retry (ném ManageApiShutdownError).
        """
        client = cls._build_client()
        async with client:
            retry_count = 0

            while retry_count <= cls.max_retries:
                try:
                    # Thực hiện yêu cầu async
                    return await cls._async_request(client, method, endpoint, **kwargs)
                except Exception as e:
                    # Đánh giá có nên retry không
                    if retry_count < cls.max_retries and cls._should_retry(e):
                        retry_count += 1
                        print(
                            f"Yêu cầu async {method} {endpoint} thất bại, sẽ retry lần {retry_count} sau {cls.retry_delay:.1f} giây"
                        )
                        await cls._retry_wait()
                        if cls._closed:
                            # Việc kết thúc phải phân biệt được: raise trần sẽ ngụy trang việc thoát sớm do shutdown
                            # thành jitter mạng, che giấu tín hiệu service offline
                            raise ManageApiShutdownError(
                                f"safe_close đã ngắt chờ retry: {e}") from e
                        continue
                    else:
                        # Không retry, ném exception trực tiếp
                        raise

    @classmethod
    def _get_instance(cls) -> Optional["ManageApiClient"]:
        """Lấy tham chiếu instance singleton một cách thread-safe.

        Ràng buộc: caller phải giữ tham chiếu local được trả về, cấm đọc lại _instance sau khi check null (cửa sổ TOCTOU).
        """
        with cls._instance_lock:
            return cls._instance

    @classmethod
    def safe_close(cls):
        """Đóng cổng yêu cầu; các request đang bay sẽ giải phóng client trong event loop của chúng.

        Request đã bắt đầu không bị hủy; chờ retry bị ngắt, bỏ qua các retry còn lại (xem _retry_wait).
        """
        with cls._instance_lock:
            cls._closed = True
            cls._instance = None


def api_guard(error_msg: str = None, raise_when_closed: bool = False):
    """Decorator: thống nhất việc lấy instance singleton, check null và đỡ exception

    - Lấy một lần tham chiếu local của instance qua _get_instance(), inject thành
      tham số đầu tiên của hàm được decorate, loại bỏ cửa sổ race TOCTOU với safe_close();
    - Khi instance chưa khởi tạo hoặc đã đóng: raise_when_closed=True ném exception rõ ràng
      (dùng cho hàm đường khởi động), ngược lại lặng lẽ trả về None (dùng cho đường daemon thread);
    - Khi error_msg khác None thì bắt exception của request, in log và trả về None;
      khi là None thì exception được ném nguyên vẹn, do caller xử lý.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            instance = ManageApiClient._get_instance()
            if instance is None:
                if raise_when_closed:
                    raise Exception("ManageApiClient chưa khởi tạo hoặc đã đóng")
                return None
            if error_msg is None:
                return await func(instance, *args, **kwargs)
            try:
                return await func(instance, *args, **kwargs)
            except Exception as e:
                # Ràng buộc: exception httpx/parse thì in full stack; exception ngữ nghĩa nghiệp vụ chỉ in
                # message (message tự mang context, stack là nhiễu tần suất cao)
                if isinstance(e, (httpx.HTTPError, ValueError)):
                    print(f"{error_msg}: {e}\n{traceback.format_exc()}")
                else:
                    print(f"{error_msg}: {e}")
                return None

        return wrapper

    return decorator


@api_guard(raise_when_closed=True)
async def get_server_config(instance) -> Optional[Dict]:
    """Lấy cấu hình cơ bản của server"""
    return await instance._execute_async_request("POST", "/config/server-base")


@api_guard(raise_when_closed=True)
async def get_agent_models(
    instance, mac_address: str, client_id: str, selected_module: Dict
) -> Optional[Dict]:
    """Lấy cấu hình model của agent"""
    return await instance._execute_async_request(
        "POST",
        "/config/agent-models",
        json={
            "macAddress": mac_address,
            "clientId": client_id,
            "selectedModule": selected_module,
        },
    )


@api_guard("Lấy từ thay thế thất bại")
async def get_correct_words(instance, mac_address: str) -> Optional[Dict]:
    """Lấy từ thay thế của Trợ lý AI"""
    return await instance._execute_async_request(
        "POST", "/config/correct-words",
        json={"macAddress": mac_address}
    )


@api_guard("Tạo và lưu tóm tắt lịch sử chat thất bại")
async def generate_and_save_chat_summary(instance, session_id: str) -> Optional[Dict]:
    """Tạo và lưu tóm tắt lịch sử chat (gọi trong daemon thread, khi service đã đóng thì lặng lẽ trả về None)"""
    return await instance._execute_async_request(
        "POST",
        f"/agent/chat-summary/{session_id}/save",
    )


@api_guard("Tạo và lưu tiêu đề chat thất bại")
async def generate_and_save_chat_title(instance, session_id: str) -> Optional[Dict]:
    """Tạo và lưu tiêu đề chat (gọi trong daemon thread, khi service đã đóng thì lặng lẽ trả về None)"""
    return await instance._execute_async_request(
        "POST",
        f"/agent/chat-title/{session_id}/generate",
    )


@api_guard("Báo cáo TTS thất bại")
async def report(
    instance, mac_address: str, session_id: str, chat_type: int, content: str, audio, report_time
) -> Optional[Dict]:
    """Báo cáo lịch sử chat bất đồng bộ"""
    if not content:
        return None
    return await instance._execute_async_request(
        "POST",
        f"/agent/chat-history/report",
        json={
            "macAddress": mac_address,
            "sessionId": session_id,
            "chatType": chat_type,
            "content": content,
            "reportTime": report_time,
            "audioBase64": (
                base64.b64encode(audio).decode("utf-8") if audio else None
            ),
        },
    )


@api_guard("Tra danh bạ thất bại")
async def lookup_address_book(instance, caller_mac: str, nickname: str) -> Optional[Dict]:
    """Tìm thiết bị đích theo nickname"""
    return await instance._execute_async_request(
        "GET",
        f"/device/address-book/lookup?callerMac={caller_mac}&nickname={nickname}",
    )


def init_service(config):
    ManageApiClient(config)


def manage_api_http_safe_close():
    ManageApiClient.safe_close()
