import json
import copy
from aiohttp import web
from config.logger import setup_logging
from core.api.base_handler import BaseHandler
from core.utils.util import get_vision_url, is_valid_image_file
from core.utils.vllm import create_instance
from config.config_loader import get_private_config_from_api
from core.utils.auth import AuthToken
import base64
from typing import Tuple, Optional
from plugins_func.register import Action

TAG = __name__

# Đặt kích thước file tối đa là 5MB
MAX_FILE_SIZE = 5 * 1024 * 1024


class VisionHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)
        # Khởi tạo công cụ xác thực
        self.auth = AuthToken(config["server"]["auth_key"])

    def _create_error_response(self, message: str) -> dict:
        """Tạo định dạng phản hồi lỗi thống nhất"""
        return {"success": False, "message": message}

    def _verify_auth_token(self, request) -> Tuple[bool, Optional[str]]:
        """Xác thực token xác thực"""
        # Chế độ kiểm tra: cho phép token kiểm tra cụ thể hoặc bỏ qua xác thực
        auth_header = request.headers.get("Authorization", "")
        client_id = request.headers.get("Client-Id", "")

        # Cho phép client kiểm tra bỏ qua xác thực
        if client_id == "web_test_client":
            device_id = request.headers.get("Device-Id", "test_device")
            return True, device_id

        if not auth_header.startswith("Bearer "):
            return False, None

        token = auth_header[7:]  # Bỏ tiền tố "Bearer "
        return self.auth.verify_token(token)

    async def handle_post(self, request):
        """Xử lý request MCP Vision POST"""
        response = None  # Khởi tạo biến response
        try:
            # Xác thực token
            is_valid, token_device_id = self._verify_auth_token(request)
            if not is_valid:
                response = web.Response(
                    text=json.dumps(
                        self._create_error_response("Token xác thực không hợp lệ hoặc đã hết hạn")
                    ),
                    content_type="application/json",
                    status=401,
                )
                return response

            # Lấy thông tin header của request
            device_id = request.headers.get("Device-Id", "")
            client_id = request.headers.get("Client-Id", "")
            if device_id != token_device_id:
                raise ValueError("Device ID không khớp với token")
            # Phân tích request multipart/form-data
            reader = await request.multipart()

            # Đọc trường question
            question_field = await reader.next()
            if question_field is None:
                raise ValueError("Thiếu trường câu hỏi")
            question = await question_field.text()
            self.logger.bind(tag=TAG).debug(f"Question: {question}")

            # Đọc file hình ảnh
            image_field = await reader.next()
            if image_field is None:
                raise ValueError("Thiếu file hình ảnh")

            # Đọc dữ liệu hình ảnh
            image_data = await image_field.read()
            if not image_data:
                raise ValueError("Dữ liệu hình ảnh trống")

            # Kiểm tra kích thước file
            if len(image_data) > MAX_FILE_SIZE:
                raise ValueError(
                    f"Kích thước hình ảnh vượt giới hạn, tối đa cho phép {MAX_FILE_SIZE/1024/1024}MB"
                )

            # Kiểm tra định dạng file
            if not is_valid_image_file(image_data):
                raise ValueError(
                    "Định dạng file không được hỗ trợ, vui lòng tải lên file hình ảnh hợp lệ (hỗ trợ JPEG, PNG, GIF, BMP, TIFF, WEBP)"
                )

            # Chuyển hình ảnh thành mã hoá base64
            image_base64 = base64.b64encode(image_data).decode("utf-8")

            # Nếu đã bật console quản trị thì lấy cấu hình model từ console quản trị
            current_config = copy.deepcopy(self.config)
            read_config_from_api = current_config.get("read_config_from_api", False)
            if read_config_from_api:
                current_config = await get_private_config_from_api(
                    current_config,
                    device_id,
                    client_id,
                )

            select_vllm_module = current_config["selected_module"].get("VLLM")
            if not select_vllm_module:
                raise ValueError("Bạn chưa đặt module phân tích hình ảnh mặc định")

            vllm_type = (
                select_vllm_module
                if "type" not in current_config["VLLM"][select_vllm_module]
                else current_config["VLLM"][select_vllm_module]["type"]
            )

            if not vllm_type:
                raise ValueError(f"Không tìm thấy provider tương ứng của module VLLM {vllm_type}")

            vllm = create_instance(
                vllm_type, current_config["VLLM"][select_vllm_module]
            )

            result = vllm.response(question, image_base64)

            return_json = {
                "success": True,
                "action": Action.RESPONSE.name,
                "response": result,
            }

            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        except ValueError as e:
            self.logger.bind(tag=TAG).error(f"Request MCP Vision POST bất thường: {e}")
            return_json = self._create_error_response(str(e))
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Request MCP Vision POST bất thường: {e}")
            return_json = self._create_error_response("Xảy ra lỗi khi xử lý request")
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            if response:
                self._add_cors_headers(response)
            return response

    async def handle_get(self, request):
        """Xử lý request MCP Vision GET"""
        try:
            vision_explain = get_vision_url(self.config)
            if vision_explain and len(vision_explain) > 0 and "null" != vision_explain:
                message = (
                    f"Interface MCP Vision chạy bình thường, địa chỉ interface giải thích hình ảnh là: {vision_explain}"
                )
            else:
                message = "Interface MCP Vision chạy không bình thường, hãy mở file .config.yaml trong thư mục data, tìm 【server.vision_explain】, đặt địa chỉ cho đúng"

            response = web.Response(text=message, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Request MCP Vision GET bất thường: {e}")
            return_json = self._create_error_response("Lỗi nội bộ server")
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            self._add_cors_headers(response)
            return response
