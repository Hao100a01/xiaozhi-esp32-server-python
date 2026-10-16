"""MCP client phía máy chủ"""

from __future__ import annotations

from datetime import timedelta
import asyncio
import os
import shutil
import concurrent.futures
from contextlib import AsyncExitStack
from typing import Optional, List, Dict, Any

from mcp import ClientSession, StdioServerParameters, Implementation
from mcp.client.session import SamplingFnT, ElicitationFnT, ListRootsFnT, LoggingFnT, MessageHandlerFnT
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.session import ProgressFnT

from config.logger import setup_logging
from core.utils.util import sanitize_tool_name

TAG = __name__


class ServerMCPClient:
    """MCP client phía máy chủ, dùng để kết nối và quản lý dịch vụ MCP"""

    def __init__(self, config: Dict[str, Any]):
        """Khởi tạo MCP client phía máy chủ

        Args:
            config: Dict cấu hình dịch vụ MCP
        """
        self.logger = setup_logging()
        self.config = config

        self._worker_task: Optional[asyncio.Task] = None
        self._ready_evt = asyncio.Event()
        self._shutdown_evt = asyncio.Event()

        self.session: Optional[ClientSession] = None
        self.tools: List = []  # Đối tượng công cụ gốc
        self.tools_dict: Dict[str, Any] = {}
        self.name_mapping: Dict[str, str] = {}

    async def initialize(self, read_timeout_seconds: timedelta | None = None,
             sampling_callback: SamplingFnT | None = None,
             elicitation_callback: ElicitationFnT | None = None,
             list_roots_callback: ListRootsFnT | None = None,
             logging_callback: LoggingFnT | None = None,
             message_handler: MessageHandlerFnT | None = None,
             client_info: Implementation | None = None):
        """Khởi tạo kết nối MCP client"""
        if self._worker_task:
            return

        self._worker_task = asyncio.create_task(
            self._worker(read_timeout_seconds=read_timeout_seconds,
                        sampling_callback=sampling_callback,
                        elicitation_callback=elicitation_callback,
                        list_roots_callback=list_roots_callback,
                        logging_callback=logging_callback,
                        message_handler=message_handler,
                        client_info=client_info), name="ServerMCPClientWorker"
        )
        await self._ready_evt.wait()

        self.logger.bind(tag=TAG).info(
            f"MCP client phía máy chủ đã kết nối, công cụ khả dụng: {[name for name in self.name_mapping.values()]}"
        )

    async def cleanup(self):
        """Dọn dẹp tài nguyên MCP client"""
        if not self._worker_task:
            return

        self._shutdown_evt.set()
        try:
            await asyncio.wait_for(self._worker_task, timeout=20)
        except (asyncio.TimeoutError, Exception) as e:
            self.logger.bind(tag=TAG).error(f"Lỗi đóng MCP client phía máy chủ: {e}")
        finally:
            self._worker_task = None

    def has_tool(self, name: str) -> bool:
        """Kiểm tra có chứa công cụ được chỉ định không

        Args:
            name: Tên công cụ

        Returns:
            bool: Có chứa công cụ đó không
        """
        return name in self.tools_dict

    def get_available_tools(self) -> List[Dict[str, Any]]:
        """Lấy định nghĩa mọi công cụ khả dụng

        Returns:
            List[Dict[str, Any]]: Danh sách định nghĩa công cụ
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                },
            }
            for name, tool in self.tools_dict.items()
        ]

    async def call_tool(self, name: str, arguments: dict, read_timeout_seconds: timedelta | None = None, progress_callback: ProgressFnT | None = None, *, meta: dict[str, Any] | None = None) -> Any:
        """Gọi công cụ được chỉ định

        Args:
            name: Tên công cụ
            arguments: Tham số công cụ
            read_timeout_seconds:
            progress_callback: Hàm callback tiến độ
            meta:

        Returns:
            Any: Kết quả thực thi công cụ

        Raises:
            RuntimeError: Ném ra khi client chưa được khởi tạo
        """
        if not self.session:
            raise RuntimeError("MCP client phía máy chủ chưa được khởi tạo")

        real_name = self.name_mapping.get(name, name)
        loop = self._worker_task.get_loop()
        coro = self.session.call_tool(real_name, arguments=arguments, read_timeout_seconds=read_timeout_seconds, progress_callback=progress_callback, meta=meta)

        if loop is asyncio.get_running_loop():
            return await coro

        fut: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wrap_future(fut)

    def is_connected(self) -> bool:
        """Kiểm tra MCP client có kết nối bình thường không

        Returns:
            bool: Nếu client đã kết nối và hoạt động bình thường, trả về True, ngược lại trả về False
        """
        # Kiểm tra tác vụ công việc có tồn tại không
        if self._worker_task is None:
            return False

        # Kiểm tra tác vụ công việc đã hoàn thành hay bị huỷ chưa
        if self._worker_task.done():
            return False

        # Kiểm tra phiên có tồn tại không
        if self.session is None:
            return False

        # Mọi kiểm tra đều qua, kết nối bình thường
        return True

    async def _worker(self, read_timeout_seconds: timedelta | None = None,
             sampling_callback: SamplingFnT | None = None,
             elicitation_callback: ElicitationFnT | None = None,
             list_roots_callback: ListRootsFnT | None = None,
             logging_callback: LoggingFnT | None = None,
             message_handler: MessageHandlerFnT | None = None,
             client_info: Implementation | None = None):
        """Coroutine công việc của MCP client"""
        async with AsyncExitStack() as stack:
            try:
                # Thiết lập StdioClient
                if "command" in self.config:
                    cmd = (
                        shutil.which("npx")
                        if self.config["command"] == "npx"
                        else self.config["command"]
                    )
                    env = {**os.environ, **self.config.get("env", {})}
                    params = StdioServerParameters(
                        command=cmd,
                        args=self.config.get("args", []),
                        env=env,
                    )
                    stdio_r, stdio_w = await stack.enter_async_context(
                        stdio_client(params)
                    )
                    read_stream, write_stream = stdio_r, stdio_w

                # Thiết lập SSEClient
                elif "url" in self.config:
                    headers = dict(self.config.get("headers", {}))
                    # TODO tương thích phiên bản cũ
                    if "API_ACCESS_TOKEN" in self.config:
                        headers["Authorization"] = f"Bearer {self.config['API_ACCESS_TOKEN']}"
                        self.logger.bind(tag=TAG).warning(f"Bạn đang dùng cấu hình API_ACCESS_TOKEN cũ đã lỗi thời, hãy đặt trực tiếp API_ACCESS_TOKEN trong headers của .mcp_server_settings.json, ví dụ 'Authorization': 'Bearer API_ACCESS_TOKEN'")
                   
                    # Chọn client khác nhau theo kiểu transport, mặc định là SSE
                    transport_type = self.config.get("transport", "sse")

                    if transport_type == "streamable-http" or transport_type == "http":
                        # Dùng truyền tải Streamable HTTP
                        http_r, http_w, get_session_id = await stack.enter_async_context(
                            streamablehttp_client(
                                url=self.config["url"],
                                headers=headers,
                                timeout=self.config.get("timeout", 30),
                                sse_read_timeout=self.config.get("sse_read_timeout", 60 * 5),
                                terminate_on_close=self.config.get("terminate_on_close", True)
                            )
                        )
                        read_stream, write_stream = http_r, http_w
                    else:
                        # Dùng truyền tải SSE truyền thống
                        sse_r, sse_w = await stack.enter_async_context(
                            sse_client(
                                url=self.config["url"],
                                headers=headers,
                                timeout=self.config.get("timeout", 5),
                                sse_read_timeout=self.config.get("sse_read_timeout", 60 * 5)
                            )
                        )
                        read_stream, write_stream = sse_r, sse_w

                else:
                    raise ValueError("Cấu hình MCP client phải chứa 'command' hoặc 'url'")

                self.session = await stack.enter_async_context(
                    ClientSession(
                        read_stream=read_stream,
                        write_stream=write_stream,
                        read_timeout_seconds=read_timeout_seconds,
                        sampling_callback=sampling_callback,
                        elicitation_callback=elicitation_callback,
                        list_roots_callback=list_roots_callback,
                        logging_callback=logging_callback,
                        message_handler=message_handler,
                        client_info=client_info
                    )
                )
                await self.session.initialize()

                # Lấy công cụ
                self.tools = (await self.session.list_tools()).tools
                for t in self.tools:
                    sanitized = sanitize_tool_name(t.name)
                    self.tools_dict[sanitized] = t
                    self.name_mapping[sanitized] = t.name

                self._ready_evt.set()

                # Treo chờ đóng
                await self._shutdown_evt.wait()

            except Exception as e:
                self.logger.bind(tag=TAG).error(f"Lỗi coroutine công việc của MCP client phía máy chủ: {e}")
                self._ready_evt.set()
                raise
