"""Bộ thực thi công cụ plugin phía máy chủ"""

import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import all_function_registry, module_func_map, Action, ActionResponse


class ServerPluginExecutor(ToolExecutor):
    """Bộ thực thi công cụ plugin phía máy chủ"""

    def __init__(self, conn: "ConnectionHandler"):
        self.conn = conn
        self.config = conn.config

    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """Thực thi công cụ plugin phía máy chủ"""
        func_item = all_function_registry.get(tool_name)
        if not func_item:
            return ActionResponse(
                action=Action.NOTFOUND, response=f"Hàm plugin {tool_name} không tồn tại"
            )

        try:
            # Quyết định cách gọi theo kiểu công cụ
            if hasattr(func_item, "type"):
                func_type = func_item.type
                if func_type.code in [4, 5]:  # SYSTEM_CTL, IOT_CTL (cần tham số conn)
                    result = func_item.func(conn, **arguments)
                elif func_type.code == 2:  # WAIT
                    result = func_item.func(**arguments)
                elif func_type.code == 3:  # CHANGE_SYS_PROMPT
                    result = func_item.func(conn, **arguments)
                else:
                    result = func_item.func(**arguments)
            else:
                # Mặc định không truyền tham số conn
                result = func_item.func(**arguments)

            # Tương thích hàm công cụ async def
            if asyncio.iscoroutine(result):
                result = await result

            return result

        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                response=str(e),
            )

    def _expand_plugin_names(self, config_functions):
        """Mở rộng tên plugin cấp module thành tên hàm cụ thể.

        Trong một tệp function có thể đăng ký nhiều @register_function,
        Nếu trong cấu hình dùng tên module (tên tệp), cần mở rộng thành danh sách tên hàm cụ thể.
        """
        if not isinstance(config_functions, list):
            try:
                config_functions = list(config_functions)
            except TypeError:
                return []

        expanded = []
        for name in config_functions:
            if name in module_func_map:
                # Tên module, mở rộng thành mọi tên hàm đã đăng ký dưới module đó
                expanded.extend(module_func_map[name])
            elif name in all_function_registry:
                # Khớp chính xác tên hàm, giữ lại trực tiếp
                expanded.append(name)
            else:
                # Tên không rõ, giữ nguyên giá trị (có thể là MCP hoặc công cụ khác)
                expanded.append(name)
        return expanded

    def _get_plugin_description(self, func_name):
        """Lấy mô tả hàm plugin, ưu tiên khớp chính xác tên hàm, sau đó khớp tên module."""
        plugins = self.config.get("plugins", {})
        # Khớp chính xác tên hàm
        if func_name in plugins:
            return plugins[func_name].get("description", "")
        # Tra ngược tên module qua module_func_map
        for module_name, func_names in module_func_map.items():
            if func_name in func_names and module_name in plugins:
                return plugins[module_name].get("description", "")
        return ""

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """Lấy mọi công cụ plugin phía máy chủ đã đăng ký"""
        tools = {}

        # Lấy các hàm cần thiết
        necessary_functions = ["handle_exit_intent", "get_lunar"]

        # Lấy hàm trong cấu hình
        config_functions = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ].get("functions", [])

        # Mở rộng tên plugin cấp module thành tên hàm cụ thể (cơ chế dự phòng)
        config_functions = self._expand_plugin_names(config_functions)

        # Gộp mọi hàm cần thiết
        all_required_functions = list(set(necessary_functions + config_functions))

        for func_name in all_required_functions:
            func_item = all_function_registry.get(func_name)
            if func_item:
                # Lấy mô tả từ đăng ký hàm (hỗ trợ tra hai lớp tên module và tên hàm)
                fun_description = self._get_plugin_description(func_name)
                if fun_description is not None and len(fun_description) > 0:
                    if "function" in func_item.description and isinstance(
                        func_item.description["function"], dict
                    ):
                        func_item.description["function"][
                            "description"
                        ] = fun_description

                # Plugin tin tức: cập nhật mô tả tham số nguồn tin theo cấu hình
                if func_name == "get_news_from_newsnow":
                    self._init_news_source_description(func_item, func_name)

                tools[func_name] = ToolDefinition(
                    name=func_name,
                    description=func_item.description,
                    tool_type=ToolType.SERVER_PLUGIN,
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """Kiểm tra có công cụ plugin phía máy chủ được chỉ định không"""
        return tool_name in all_function_registry

    def _init_news_source_description(self, func_item, func_name):
        """Khởi tạo mô tả tham số của công cụ tin tức theo cấu hình kết nối"""
        news_sources = (
            self.config.get("plugins", {})
            .get(func_name, {})
            .get("news_sources", "")
        )
        if not news_sources:
            news_sources = "澎湃新闻;百度热搜;财联社"
        sources_str = news_sources.replace(";", "、")
        try:
            func_item.description["function"]["parameters"]["properties"]["source"][
                "description"
            ] = f"Tên tiếng Trung chuẩn của nguồn tin tức, ví dụ {sources_str}, v.v. Tham số tuỳ chọn, nếu không cung cấp thì dùng nguồn tin tức mặc định"
        except (KeyError, TypeError):
            pass
