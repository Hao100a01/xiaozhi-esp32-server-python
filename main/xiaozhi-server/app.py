import sys
import uuid
import signal
import asyncio
from aioconsole import ainput
from config.settings import load_config
from config.logger import setup_logging
from core.utils.util import get_local_ip, validate_mcp_endpoint
from core.http_server import SimpleHttpServer
from core.websocket_server import WebSocketServer
from core.utils.util import check_ffmpeg_installed
from core.utils.gc_manager import get_gc_manager

TAG = __name__
logger = setup_logging()


async def wait_for_exit() -> None:
    """
    Chặn cho đến khi nhận Ctrl‑C / SIGTERM.
    - Unix: dùng add_signal_handler
    - Windows: dựa vào KeyboardInterrupt
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if sys.platform != "win32":  # Unix / macOS
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        # Windows: await một future pending mãi mãi,
        # để KeyboardInterrupt nổi bọt lên asyncio.run, nhằm loại bỏ vấn đề tiến trình thoát bị chặn do thread thường còn sót
        try:
            await asyncio.Future()
        except KeyboardInterrupt:  # Ctrl‑C
            pass


async def monitor_stdin():
    """Giám sát stdin, tiêu thụ phím Enter"""
    while True:
        await ainput()  # Chờ input bất đồng bộ, tiêu thụ phím Enter


async def main():
    check_ffmpeg_installed()
    config = await load_config()

    # Thứ tự ưu tiên auth_key: file cấu hình server.auth_key > manager-api.secret > tự sinh
    # auth_key dùng cho xác thực JWT, ví dụ: xác thực JWT của API phân tích hình ảnh, sinh token của API OTA và xác thực websocket
    # Lấy auth_key trong file cấu hình
    auth_key = config["server"].get("auth_key", "")
    
    # Kiểm tra auth_key, nếu không hợp lệ thì thử dùng manager-api.secret
    if not auth_key or len(auth_key) == 0 or "你" in auth_key:
        auth_key = config.get("manager-api", {}).get("secret", "")
        # Kiểm tra secret, nếu không hợp lệ thì sinh khóa ngẫu nhiên
        if not auth_key or len(auth_key) == 0 or "你" in auth_key:
            auth_key = str(uuid.uuid4().hex)
    
    config["server"]["auth_key"] = auth_key

    # Thêm task giám sát stdin
    stdin_task = asyncio.create_task(monitor_stdin())

    # Khởi động GC manager toàn cục (dọn 5 phút một lần)
    gc_manager = get_gc_manager(interval_seconds=300)
    await gc_manager.start()

    # Khởi động WebSocket server
    ws_server = WebSocketServer(config)
    ws_task = asyncio.create_task(ws_server.start())
    # Khởi động Simple HTTP server
    ota_server = SimpleHttpServer(config)
    ota_task = asyncio.create_task(ota_server.start())

    read_config_from_api = config.get("read_config_from_api", False)
    port = int(config["server"].get("http_port", 8003))
    if not read_config_from_api:
        logger.bind(tag=TAG).info(
            "API OTA là\t\thttp://{}:{}/xiaozhi/ota/",
            get_local_ip(),
            port,
        )
    logger.bind(tag=TAG).info(
        "API phân tích hình ảnh là\thttp://{}:{}/mcp/vision/explain",
        get_local_ip(),
        port,
    )
    mcp_endpoint = config.get("mcp_endpoint", None)
    if mcp_endpoint is not None and "你" not in mcp_endpoint:
        # Kiểm tra định dạng MCP endpoint
        if validate_mcp_endpoint(mcp_endpoint):
            logger.bind(tag=TAG).info("MCP endpoint là\t{}", mcp_endpoint)
            # Chuyển địa chỉ MCP endpoint thành địa chỉ gọi
            mcp_endpoint = mcp_endpoint.replace("/mcp/", "/call/")
            config["mcp_endpoint"] = mcp_endpoint
        else:
            logger.bind(tag=TAG).error("MCP endpoint không đúng chuẩn")
            config["mcp_endpoint"] = "你的接入点 websocket地址"

    # Lấy cấu hình WebSocket, dùng giá trị mặc định an toàn
    websocket_port = 8000
    server_config = config.get("server", {})
    if isinstance(server_config, dict):
        websocket_port = int(server_config.get("port", 8000))

    logger.bind(tag=TAG).info(
        "Địa chỉ Websocket là\tws://{}:{}/xiaozhi/v1/",
        get_local_ip(),
        websocket_port,
    )

    logger.bind(tag=TAG).info(
        "=======Địa chỉ trên là địa chỉ giao thức websocket, đừng dùng trình duyệt truy cập======="
    )
    logger.bind(tag=TAG).info(
        "Muốn test websocket hãy khởi động module digital-human, mở trình duyệt test tương tác"
    )
    logger.bind(tag=TAG).info(
        "=============================================================\n"
    )

    try:
        await wait_for_exit()  # Chặn cho đến khi nhận tín hiệu thoát
    except asyncio.CancelledError:
        print("Task bị hủy, đang dọn dẹp tài nguyên...")
    finally:
        # Dừng GC manager toàn cục
        await gc_manager.stop()

        # Hủy mọi task (điểm fix quan trọng)
        stdin_task.cancel()
        ws_task.cancel()
        if ota_task:
            ota_task.cancel()

        # Chờ task kết thúc (bắt buộc thêm timeout)
        await asyncio.wait(
            [stdin_task, ws_task, ota_task] if ota_task else [stdin_task, ws_task],
            timeout=3.0,
            return_when=asyncio.ALL_COMPLETED,
        )
        print("Server đã đóng, chương trình thoát.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Ngắt thủ công, chương trình kết thúc.")
