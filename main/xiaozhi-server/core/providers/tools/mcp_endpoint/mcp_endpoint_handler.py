"""Bộ xử lý điểm truy cập MCP"""

import json
import asyncio
import re
import websockets
from config.logger import setup_logging
from .mcp_endpoint_client import MCPEndpointClient

TAG = __name__
logger = setup_logging()


async def connect_mcp_endpoint(mcp_endpoint_url: str, conn=None) -> MCPEndpointClient:
    """Kết nối tới điểm truy cập MCP"""
    if not mcp_endpoint_url or "你的" in mcp_endpoint_url or mcp_endpoint_url == "null":
        return None

    try:
        websocket = await websockets.connect(mcp_endpoint_url)

        mcp_client = MCPEndpointClient(conn)
        mcp_client.set_websocket(websocket)

        # Khởi động trình lắng nghe thông điệp
        asyncio.create_task(_message_listener(mcp_client))

        # Gửi thông điệp khởi tạo
        await send_mcp_endpoint_initialize(mcp_client)

        # Gửi thông báo hoàn tất khởi tạo
        await send_mcp_endpoint_notification(mcp_client, "notifications/initialized")

        # Lấy danh sách công cụ
        await send_mcp_endpoint_tools_list(mcp_client)

        logger.bind(tag=TAG).info("Kết nối điểm truy cập MCP thành công")
        return mcp_client

    except Exception as e:
        logger.bind(tag=TAG).error(f"Kết nối điểm truy cập MCP thất bại: {e}")
        return None


async def _message_listener(mcp_client: MCPEndpointClient):
    """Lắng nghe thông điệp điểm truy cập MCP"""
    try:
        async for message in mcp_client.websocket:
            await handle_mcp_endpoint_message(mcp_client, message)
    except websockets.exceptions.ConnectionClosed:
        logger.bind(tag=TAG).info("Kết nối điểm truy cập MCP đã đóng")
    except Exception as e:
        logger.bind(tag=TAG).error(f"Lỗi trình lắng nghe thông điệp điểm truy cập MCP: {e}")
    finally:
        await mcp_client.set_ready(False)


async def handle_mcp_endpoint_message(mcp_client: MCPEndpointClient, message: str):
    """Xử lý thông điệp điểm truy cập MCP"""
    try:
        payload = json.loads(message)
        logger.bind(tag=TAG).debug(f"Đã nhận thông điệp điểm truy cập MCP: {payload}")

        if not isinstance(payload, dict):
            logger.bind(tag=TAG).error("Định dạng thông điệp điểm truy cập MCP sai")
            return

        # Handle result
        if "result" in payload:
            result = payload["result"]
            # Lấy an toàn ID thông điệp, nếu là None thì dùng 0
            msg_id_raw = payload.get("id")
            msg_id = int(msg_id_raw) if msg_id_raw is not None else 0

            # Check for tool call response first
            if msg_id in mcp_client.call_results:
                logger.bind(tag=TAG).debug(
                    f"Đã nhận phản hồi gọi công cụ, ID: {msg_id}, kết quả: {result}"
                )
                await mcp_client.resolve_call_result(msg_id, result)
                return

            if msg_id == 1:  # mcpInitializeID
                logger.bind(tag=TAG).debug("Đã nhận phản hồi khởi tạo điểm truy cập MCP")
                if result is not None and isinstance(result, dict):
                    server_info = result.get("serverInfo")
                    if isinstance(server_info, dict):
                        name = server_info.get("name")
                        version = server_info.get("version")
                        logger.bind(tag=TAG).info(
                            f"Thông tin máy chủ điểm truy cập MCP: name={name}, version={version}"
                        )
                else:
                    logger.bind(tag=TAG).warning(
                        "Kết quả phản hồi khởi tạo điểm truy cập MCP trống hoặc sai định dạng"
                    )
                return

            elif msg_id == 2:  # mcpToolsListID
                logger.bind(tag=TAG).debug("Đã nhận phản hồi danh sách công cụ điểm truy cập MCP")
                if (
                    result is not None
                    and isinstance(result, dict)
                    and "tools" in result
                ):
                    tools_data = result["tools"]
                    if not isinstance(tools_data, list):
                        logger.bind(tag=TAG).error("Định dạng danh sách công cụ sai")
                        return

                    logger.bind(tag=TAG).info(
                        f"Số lượng công cụ điểm truy cập MCP hỗ trợ: {len(tools_data)}"
                    )

                    for i, tool in enumerate(tools_data):
                        if not isinstance(tool, dict):
                            continue

                        name = tool.get("name", "")
                        description = tool.get("description", "")
                        input_schema = {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        }

                        if "inputSchema" in tool and isinstance(
                            tool["inputSchema"], dict
                        ):
                            schema = tool["inputSchema"]
                            input_schema["type"] = schema.get("type", "object")
                            input_schema["properties"] = schema.get("properties", {})
                            input_schema["required"] = [
                                s
                                for s in schema.get("required", [])
                                if isinstance(s, str)
                            ]

                        new_tool = {
                            "name": name,
                            "description": description,
                            "inputSchema": input_schema,
                        }
                        await mcp_client.add_tool(new_tool)
                        logger.bind(tag=TAG).debug(f"Công cụ điểm truy cập MCP #{i+1}: {name}")

                    # Thay thế tên công cụ trong mọi mô tả công cụ
                    for tool_data in mcp_client.tools.values():
                        if "description" in tool_data:
                            description = tool_data["description"]
                            # Duyệt mọi tên công cụ để thay thế
                            for (
                                sanitized_name,
                                original_name,
                            ) in mcp_client.name_mapping.items():
                                description = description.replace(
                                    original_name, sanitized_name
                                )
                            tool_data["description"] = description

                    next_cursor = (
                        result.get("nextCursor", "") if result is not None else ""
                    )
                    if next_cursor:
                        logger.bind(tag=TAG).info(
                            f"Còn công cụ khác, nextCursor: {next_cursor}"
                        )
                        await send_mcp_endpoint_tools_list_continue(
                            mcp_client, next_cursor
                        )
                    else:
                        await mcp_client.set_ready(True)
                        logger.bind(tag=TAG).info(
                            "Đã lấy mọi công cụ điểm truy cập MCP, client đã sẵn sàng"
                        )

                        # Làm mới bộ đệm công cụ, đảm bảo công cụ điểm truy cập MCP được chứa trong danh sách hàm
                        if (
                            hasattr(mcp_client, "conn")
                            and mcp_client.conn
                            and hasattr(mcp_client.conn, "func_handler")
                            and mcp_client.conn.func_handler
                        ):
                            mcp_client.conn.func_handler.tool_manager.refresh_tools()
                            mcp_client.conn.func_handler.current_support_functions()

                        logger.bind(tag=TAG).info(
                            f"Lấy công cụ điểm truy cập MCP hoàn tất, tổng cộng {len(mcp_client.tools)} công cụ"
                        )
                else:
                    logger.bind(tag=TAG).warning(
                        "Kết quả phản hồi danh sách công cụ điểm truy cập MCP trống hoặc sai định dạng"
                    )
                return

        # Handle method calls (requests from the endpoint)
        elif "method" in payload:
            method = payload["method"]
            logger.bind(tag=TAG).info(f"Đã nhận yêu cầu điểm truy cập MCP: {method}")

        elif "error" in payload:
            error_data = payload["error"]
            error_msg = error_data.get("message", "Lỗi không xác định")
            logger.bind(tag=TAG).error(f"Đã nhận phản hồi lỗi điểm truy cập MCP: {error_msg}")

            # Lấy an toàn ID thông điệp, nếu là None thì dùng 0
            msg_id_raw = payload.get("id")
            msg_id = int(msg_id_raw) if msg_id_raw is not None else 0

            if msg_id in mcp_client.call_results:
                await mcp_client.reject_call_result(
                    msg_id, Exception(f"Lỗi điểm truy cập MCP: {error_msg}")
                )

    except json.JSONDecodeError as e:
        logger.bind(tag=TAG).error(f"Phân tích JSON thông điệp điểm truy cập MCP thất bại: {e}")
    except Exception as e:
        logger.bind(tag=TAG).error(f"Lỗi khi xử lý thông điệp điểm truy cập MCP: {e}")
        import traceback

        logger.bind(tag=TAG).error(f"Chi tiết lỗi: {traceback.format_exc()}")


async def send_mcp_endpoint_initialize(mcp_client: MCPEndpointClient):
    """Gửi thông điệp khởi tạo điểm truy cập MCP"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,  # mcpInitializeID
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
            },
            "clientInfo": {
                "name": "XiaozhiMCPEndpointClient",
                "version": "1.0.0",
            },
        },
    }
    message = json.dumps(payload)
    logger.bind(tag=TAG).info("Gửi thông điệp khởi tạo điểm truy cập MCP")
    await mcp_client.send_message(message)


async def send_mcp_endpoint_notification(mcp_client: MCPEndpointClient, method: str):
    """Gửi thông điệp thông báo điểm truy cập MCP"""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": {},
    }
    message = json.dumps(payload)
    logger.bind(tag=TAG).debug(f"Gửi thông báo điểm truy cập MCP: {method}")
    await mcp_client.send_message(message)


async def send_mcp_endpoint_tools_list(mcp_client: MCPEndpointClient):
    """Gửi yêu cầu danh sách công cụ điểm truy cập MCP"""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID
        "method": "tools/list",
    }
    message = json.dumps(payload)
    logger.bind(tag=TAG).debug("Gửi yêu cầu danh sách công cụ điểm truy cập MCP")
    await mcp_client.send_message(message)


async def send_mcp_endpoint_tools_list_continue(
    mcp_client: MCPEndpointClient, cursor: str
):
    """Gửi yêu cầu danh sách công cụ điểm truy cập MCP kèm cursor"""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID (same ID for continuation)
        "method": "tools/list",
        "params": {"cursor": cursor},
    }
    message = json.dumps(payload)
    logger.bind(tag=TAG).info(f"Gửi yêu cầu danh sách công cụ điểm truy cập MCP kèm cursor: {cursor}")
    await mcp_client.send_message(message)


async def call_mcp_endpoint_tool(
    mcp_client: MCPEndpointClient, tool_name: str, args: str = "{}", timeout: int = 30
):
    """
    Gọi công cụ điểm truy cập MCP được chỉ định, và chờ phản hồi
    """
    if not await mcp_client.is_ready():
        raise RuntimeError("Client điểm truy cập MCP chưa sẵn sàng")

    if not mcp_client.has_tool(tool_name):
        raise ValueError(f"Công cụ {tool_name} không tồn tại")

    tool_call_id = await mcp_client.get_next_id()
    result_future = asyncio.Future()
    await mcp_client.register_call_result_future(tool_call_id, result_future)

    # Xử lý tham số
    try:
        if isinstance(args, str):
            # Đảm bảo chuỗi là JSON hợp lệ
            if not args.strip():
                arguments = {}
            else:
                try:
                    # Thử phân tích trực tiếp
                    arguments = json.loads(args)
                except json.JSONDecodeError:
                    # Nếu phân tích thất bại, thử gộp nhiều đối tượng JSON
                    try:
                        # Dùng biểu thức chính quy để khớp mọi đối tượng JSON
                        json_objects = re.findall(r"\{[^{}]*\}", args)
                        if len(json_objects) > 1:
                            # Gộp mọi đối tượng JSON
                            merged_dict = {}
                            for json_str in json_objects:
                                try:
                                    obj = json.loads(json_str)
                                    if isinstance(obj, dict):
                                        merged_dict.update(obj)
                                except json.JSONDecodeError:
                                    continue
                            if merged_dict:
                                arguments = merged_dict
                            else:
                                raise ValueError(f"Không thể phân tích bất kỳ đối tượng JSON hợp lệ nào: {args}")
                        else:
                            raise ValueError(f"Phân tích JSON tham số thất bại: {args}")
                    except Exception as e:
                        logger.bind(tag=TAG).error(
                            f"Phân tích JSON tham số thất bại: {str(e)}, tham số gốc: {args}"
                        )
                        raise ValueError(f"Phân tích JSON tham số thất bại: {str(e)}")
        elif isinstance(args, dict):
            arguments = args
        else:
            raise ValueError(f"Kiểu tham số sai, mong đợi chuỗi hoặc dict, kiểu thực tế: {type(args)}")

        # Đảm bảo tham số là kiểu dict
        if not isinstance(arguments, dict):
            raise ValueError(f"Tham số phải là kiểu dict, kiểu thực tế: {type(arguments)}")

    except Exception as e:
        if not isinstance(e, ValueError):
            raise ValueError(f"Xử lý tham số thất bại: {str(e)}")
        raise e

    actual_name = mcp_client.name_mapping.get(tool_name, tool_name)
    payload = {
        "jsonrpc": "2.0",
        "id": tool_call_id,
        "method": "tools/call",
        "params": {"name": actual_name, "arguments": arguments},
    }

    message = json.dumps(payload)
    logger.bind(tag=TAG).info(
        f"Gửi yêu cầu gọi công cụ điểm truy cập MCP: {actual_name}, tham số: {json.dumps(arguments, ensure_ascii=False)}"
    )
    await mcp_client.send_message(message)

    try:
        # Wait for response or timeout
        raw_result = await asyncio.wait_for(result_future, timeout=timeout)
        logger.bind(tag=TAG).info(
            f"Gọi công cụ điểm truy cập MCP {actual_name} thành công, kết quả gốc: {raw_result}"
        )

        if isinstance(raw_result, dict):
            if raw_result.get("isError") is True:
                error_msg = raw_result.get(
                    "error", "Lệnh gọi công cụ trả về lỗi, nhưng không cung cấp thông tin lỗi cụ thể"
                )
                raise RuntimeError(f"Lỗi gọi công cụ: {error_msg}")

            content = raw_result.get("content")
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    # Trả về trực tiếp nội dung văn bản, không phân tích JSON
                    return content[0]["text"]
        # Nếu kết quả không đúng định dạng mong đợi, chuyển nó thành chuỗi
        return str(raw_result)
    except asyncio.TimeoutError:
        await mcp_client.cleanup_call_result(tool_call_id)
        raise TimeoutError("Yêu cầu gọi công cụ hết thời gian chờ")
    except Exception as e:
        await mcp_client.cleanup_call_result(tool_call_id)
        raise e
