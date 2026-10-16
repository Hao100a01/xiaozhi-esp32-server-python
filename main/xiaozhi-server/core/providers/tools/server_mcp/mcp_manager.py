"""Trình quản lý MCP phía máy chủ"""

import asyncio
import os
import json
from typing import Dict, Any, List

from mcp.types import LoggingMessageNotificationParams

from config.config_loader import get_project_dir
from config.logger import setup_logging
from .mcp_client import ServerMCPClient

TAG = __name__
logger = setup_logging()


class ServerMCPManager:
    """Trình quản lý tập trung quản lý nhiều dịch vụ MCP phía máy chủ"""

    def __init__(self, conn) -> None:
        """Khởi tạo trình quản lý MCP"""
        self.conn = conn
        self.config_path = get_project_dir() + "data/.mcp_server_settings.json"
        if not os.path.exists(self.config_path):
            self.config_path = ""
            logger.bind(tag=TAG).warning(
                f"Vui lòng kiểm tra tệp cấu hình dịch vụ mcp: data/.mcp_server_settings.json"
            )
        self.clients: Dict[str, ServerMCPClient] = {}
        self.tools = []
        self._init_lock = asyncio.Lock()

    def load_config(self) -> Dict[str, Any]:
        """Tải cấu hình dịch vụ MCP"""
        if len(self.config_path) == 0:
            return {}

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("mcpServers", {})
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Error loading MCP config from {self.config_path}: {e}"
            )
            return {}

    async def _init_server(self, name: str, srv_config: Dict[str, Any]):
        """Khởi tạo một dịch vụ MCP"""
        client = None
        try:
            # Khởi tạo MCP client phía máy chủ
            logger.bind(tag=TAG).info(f"Khởi tạo MCP client phía máy chủ: {name}")
            client = ServerMCPClient(srv_config)
            # Đặt thời gian chờ 10 giây
            await asyncio.wait_for(client.initialize(logging_callback=self.logging_callback), timeout=10)

            # Dùng khoá để bảo vệ việc sửa trạng thái dùng chung
            async with self._init_lock:
                self.clients[name] = client
                client_tools = client.get_available_tools()
                self.tools.extend(client_tools)

        except asyncio.TimeoutError:
            logger.bind(tag=TAG).error(
                f"Failed to initialize MCP server {name}: Timeout"
            )
            if client:
                await client.cleanup()
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Failed to initialize MCP server {name}: {e}"
            )
            if client:
                await client.cleanup()

    async def initialize_servers(self) -> None:
        """Khởi tạo mọi dịch vụ MCP"""
        config = self.load_config()
        tasks = []
        for name, srv_config in config.items():
            if not srv_config.get("command") and not srv_config.get("url"):
                logger.bind(tag=TAG).warning(
                    f"Skipping server {name}: neither command nor url specified"
                )
                continue
            
            tasks.append(self._init_server(name, srv_config))
        
        if tasks:
            await asyncio.gather(*tasks)

        # Xuất danh sách công cụ MCP phía máy chủ hiện được hỗ trợ
        if hasattr(self.conn, "func_handler") and self.conn.func_handler:
            # Làm mới bộ đệm công cụ để đảm bảo công cụ MCP phía máy chủ được tải đúng
            if hasattr(self.conn.func_handler, "tool_manager"):
                self.conn.func_handler.tool_manager.refresh_tools()
            self.conn.func_handler.current_support_functions()

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Lấy định nghĩa function công cụ của mọi dịch vụ"""
        return self.tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Kiểm tra có phải công cụ MCP không"""
        for tool in self.tools:
            if (
                tool.get("function") is not None
                and tool["function"].get("name") == tool_name
            ):
                return True
        return False

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Thực thi lệnh gọi công cụ, khi thất bại sẽ thử kết nối lại"""
        logger.bind(tag=TAG).info(f"Thực thi công cụ MCP phía máy chủ {tool_name}, tham số: {arguments}")

        max_retries = 3  # Số lần thử lại tối đa
        retry_interval = 2  # Khoảng cách thử lại (giây)

        # Tìm client tương ứng
        client_name = None
        target_client = None
        for name, client in self.clients.items():
            if client.has_tool(tool_name):
                client_name = name
                target_client = client
                break

        if not target_client:
            raise ValueError(f"Không tìm thấy công cụ {tool_name} trong bất kỳ dịch vụ MCP nào")

        # Lệnh gọi công cụ có cơ chế thử lại
        for attempt in range(max_retries):
            try:
                return await target_client.call_tool(tool_name, arguments, progress_callback=self.progress_callback)
            except Exception as e:
                # Khi lần thử cuối cùng thất bại thì ném ngoại lệ trực tiếp
                if attempt == max_retries - 1:
                    raise

                logger.bind(tag=TAG).warning(
                    f"Thực thi công cụ {tool_name} thất bại (lần thử {attempt+1}/{max_retries}): {e}"
                )

                # Thử kết nối lại
                logger.bind(tag=TAG).info(
                    f"Thử kết nối lại MCP client {client_name} trước khi thử lại"
                )
                try:
                    # Đóng kết nối cũ
                    await target_client.cleanup()

                    # Khởi tạo lại client
                    config = self.load_config()
                    if client_name in config:
                        client = ServerMCPClient(config[client_name])
                        await client.initialize(logging_callback=self.logging_callback)
                        self.clients[client_name] = client
                        target_client = client
                        logger.bind(tag=TAG).info(
                            f"Kết nối lại MCP client thành công: {client_name}"
                        )
                    else:
                        logger.bind(tag=TAG).error(
                            f"Cannot reconnect MCP client {client_name}: config not found"
                        )
                except Exception as reconnect_error:
                    logger.bind(tag=TAG).error(
                        f"Failed to reconnect MCP client {client_name}: {reconnect_error}"
                    )

                # Chờ một lúc rồi thử lại
                await asyncio.sleep(retry_interval)

    async def cleanup_all(self) -> None:
        """Đóng mọi MCP client"""
        for name, client in list(self.clients.items()):
            try:
                if hasattr(client, "cleanup"):
                    await asyncio.wait_for(client.cleanup(), timeout=20)
                logger.bind(tag=TAG).info(f"MCP client phía máy chủ đã đóng: {name}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.bind(tag=TAG).error(f"Lỗi khi đóng MCP client phía máy chủ {name}: {e}")
        self.clients.clear()

    # Phương thức callback tuỳ chọn

    async def logging_callback(self, params: LoggingMessageNotificationParams):
        logger.bind(tag=TAG).info(f"[Server Log - {params.level.upper()}] {params.data}")

    async def progress_callback(self, progress: float, total: float | None, message: str | None) -> None:
        logger.bind(tag=TAG).info(f"[Progress {progress}/{total}]: {message}")