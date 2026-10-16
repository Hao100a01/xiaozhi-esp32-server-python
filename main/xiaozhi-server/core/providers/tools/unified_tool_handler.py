"""Bộ xử lý công cụ thống nhất"""

import json
from typing import Dict, List, Any, Optional
from config.logger import setup_logging
from plugins_func.loadplugins import auto_import_modules

from .base import ToolType
from plugins_func.register import Action, ActionResponse
from .unified_tool_manager import ToolManager
from .server_plugins import ServerPluginExecutor
from .server_mcp import ServerMCPExecutor
from .device_iot import DeviceIoTExecutor
from .device_mcp import DeviceMCPExecutor
from .mcp_endpoint import MCPEndpointExecutor
from core.handle.sendAudioHandle import send_display_message


class UnifiedToolHandler:
    """Bộ xử lý công cụ thống nhất"""

    def __init__(self, conn):
        self.conn = conn
        self.config = conn.config
        self.logger = setup_logging()

        # Tạo trình quản lý công cụ
        self.tool_manager = ToolManager(conn)

        # Tạo các loại bộ thực thi
        self.server_plugin_executor = ServerPluginExecutor(conn)
        self.server_mcp_executor = ServerMCPExecutor(conn)
        self.device_iot_executor = DeviceIoTExecutor(conn)
        self.device_mcp_executor = DeviceMCPExecutor(conn)
        self.mcp_endpoint_executor = MCPEndpointExecutor(conn)

        # Đăng ký bộ thực thi
        self.tool_manager.register_executor(
            ToolType.SERVER_PLUGIN, self.server_plugin_executor
        )
        self.tool_manager.register_executor(
            ToolType.SERVER_MCP, self.server_mcp_executor
        )
        self.tool_manager.register_executor(
            ToolType.DEVICE_IOT, self.device_iot_executor
        )
        self.tool_manager.register_executor(
            ToolType.DEVICE_MCP, self.device_mcp_executor
        )
        self.tool_manager.register_executor(
            ToolType.MCP_ENDPOINT, self.mcp_endpoint_executor
        )

        # Cờ khởi tạo
        self.finish_init = False

    async def _initialize(self):
        """Khởi tạo bất đồng bộ"""
        try:
            # Tự động nhập module plugin
            auto_import_modules("plugins_func.functions")

            # Khởi tạo MCP phía máy chủ
            await self.server_mcp_executor.initialize()

            # Khởi tạo điểm truy cập MCP
            await self._initialize_mcp_endpoint()

            # Khởi tạo Home Assistant (nếu cần)
            self._initialize_home_assistant()

            self.finish_init = True
            self.logger.debug("Khởi tạo bộ xử lý công cụ thống nhất hoàn tất")

            # Xuất danh sách mọi công cụ hiện được hỗ trợ
            self.current_support_functions()

        except Exception as e:
            self.logger.error(f"Khởi tạo bộ xử lý công cụ thống nhất thất bại: {e}")

    async def _initialize_mcp_endpoint(self):
        """Khởi tạo điểm truy cập MCP"""
        try:
            from .mcp_endpoint import connect_mcp_endpoint

            # Lấy URL điểm truy cập MCP từ cấu hình
            mcp_endpoint_url = self.config.get("mcp_endpoint", "")

            if (
                mcp_endpoint_url
                and "你的" not in mcp_endpoint_url
                and mcp_endpoint_url != "null"
            ):
                self.logger.info(f"Đang khởi tạo điểm truy cập MCP: {mcp_endpoint_url}")
                mcp_endpoint_client = await connect_mcp_endpoint(
                    mcp_endpoint_url, self.conn
                )

                if mcp_endpoint_client:
                    # Lưu client điểm truy cập MCP vào đối tượng kết nối
                    self.conn.mcp_endpoint_client = mcp_endpoint_client
                    self.logger.info("Khởi tạo điểm truy cập MCP thành công")
                else:
                    self.logger.warning("Khởi tạo điểm truy cập MCP thất bại")

        except Exception as e:
            self.logger.error(f"Khởi tạo điểm truy cập MCP thất bại: {e}")

    def _initialize_home_assistant(self):
        """Khởi tạo prompt Home Assistant"""
        try:
            from plugins_func.functions.hass_init import append_devices_to_prompt

            append_devices_to_prompt(self.conn)
        except ImportError:
            pass  # Bỏ qua lỗi nhập
        except Exception as e:
            self.logger.error(f"Khởi tạo Home Assistant thất bại: {e}")

    def get_functions(self) -> List[Dict[str, Any]]:
        """Lấy mô tả hàm của mọi công cụ"""
        return self.tool_manager.get_function_descriptions()

    def current_support_functions(self) -> List[str]:
        """Lấy danh sách tên hàm hiện được hỗ trợ"""
        func_names = self.tool_manager.get_supported_tool_names()
        self.logger.info(f"Danh sách hàm hiện được hỗ trợ: {func_names}")
        return func_names

    def upload_functions_desc(self):
        """Làm mới danh sách mô tả hàm"""
        self.tool_manager.refresh_tools()
        self.logger.info("Danh sách mô tả hàm đã được làm mới")

    def has_tool(self, tool_name: str) -> bool:
        """Kiểm tra có công cụ được chỉ định không"""
        return self.tool_manager.has_tool(tool_name)

    async def handle_llm_function_call(
        self, conn, function_call_data: Dict[str, Any]
    ) -> Optional[ActionResponse]:
        """Xử lý lệnh gọi hàm của LLM"""
        try:
            # Xử lý gọi nhiều hàm
            if "function_calls" in function_call_data:
                responses = []
                for call in function_call_data["function_calls"]:
                    result = await self.tool_manager.execute_tool(
                        call["name"], call.get("arguments", {})
                    )
                    responses.append(result)
                return self._combine_responses(responses)

            # Xử lý gọi một hàm
            function_name = function_call_data["name"]
            arguments = function_call_data.get("arguments", {})

            # Nếu arguments là chuỗi, thử phân tích thành JSON
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    self.logger.error(f"Không thể phân tích tham số hàm: {arguments}")
                    return ActionResponse(
                        action=Action.ERROR,
                        response="Không thể phân tích tham số hàm",
                    )

            self.logger.debug(f"Gọi hàm: {function_name}, tham số: {arguments}")

            # Gửi thông điệp hiển thị gọi công cụ tới thiết bị
            try:
                await send_display_message(self.conn, f"% {function_name}")
            except Exception as e:
                self.logger.warning(f"Gửi thông điệp hiển thị gọi công cụ thất bại: {e}")

            # Thực thi lệnh gọi công cụ
            result = await self.tool_manager.execute_tool(function_name, arguments)
            return result

        except Exception as e:
            self.logger.error(f"Lỗi xử lý function call: {e}")
            return ActionResponse(action=Action.ERROR, response=str(e))

    def _combine_responses(self, responses: List[ActionResponse]) -> ActionResponse:
        """Gộp phản hồi của nhiều lệnh gọi hàm"""
        if not responses:
            return ActionResponse(action=Action.NONE, response="Không có phản hồi")

        # Nếu có bất kỳ lỗi nào, trả về lỗi đầu tiên
        for response in responses:
            if response.action == Action.ERROR:
                return response

        # Gộp mọi phản hồi thành công
        contents = []
        responses_text = []

        for response in responses:
            if response.content:
                contents.append(response.content)
            if response.response:
                responses_text.append(response.response)

        # Xác định kiểu hành động cuối cùng
        final_action = Action.RESPONSE
        for response in responses:
            if response.action == Action.REQLLM:
                final_action = Action.REQLLM
                break

        return ActionResponse(
            action=final_action,
            result="; ".join(contents) if contents else None,
            response="; ".join(responses_text) if responses_text else None,
        )

    async def register_iot_tools(self, descriptors: List[Dict[str, Any]]):
        """Đăng ký công cụ thiết bị IoT"""
        self.device_iot_executor.register_iot_tools(descriptors)
        self.tool_manager.refresh_tools()
        self.logger.info(f"Đã đăng ký công cụ của {len(descriptors)} thiết bị IoT")

    def get_tool_statistics(self) -> Dict[str, int]:
        """Lấy thông tin thống kê công cụ"""
        return self.tool_manager.get_tool_statistics()

    async def cleanup(self):
        """Dọn dẹp tài nguyên"""
        try:
            await self.server_mcp_executor.cleanup()

            # Dọn dẹp kết nối điểm truy cập MCP
            if (
                hasattr(self.conn, "mcp_endpoint_client")
                and self.conn.mcp_endpoint_client
            ):
                await self.conn.mcp_endpoint_client.close()

            self.logger.info("Dọn dẹp bộ xử lý công cụ hoàn tất")
        except Exception as e:
            self.logger.error(f"Dọn dẹp bộ xử lý công cụ thất bại: {e}")
