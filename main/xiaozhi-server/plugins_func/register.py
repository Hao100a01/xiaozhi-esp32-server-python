from config.logger import setup_logging
from enum import Enum

TAG = __name__

logger = setup_logging()


class ToolType(Enum):
    NONE = (1, "Sau khi gọi tool xong, không làm gì thêm")
    WAIT = (2, "Gọi tool, chờ hàm trả về")
    CHANGE_SYS_PROMPT = (3, "Sửa system prompt, chuyển tính cách/vai trò của nhân vật")
    SYSTEM_CTL = (
        4,
        "Điều khiển hệ thống, ảnh hưởng luồng hội thoại bình thường như thoát, phát nhạc..., cần truyền tham số conn",
    )
    IOT_CTL = (5, "Điều khiển thiết bị IOT, cần truyền tham số conn")
    MCP_CLIENT = (6, "MCP client")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class Action(Enum):
    ERROR = (-1, "Lỗi")
    NOTFOUND = (0, "Không tìm thấy hàm")
    NONE = (1, "Không làm gì")
    RESPONSE = (2, "Trả lời trực tiếp")
    REQLLM = (3, "Gọi hàm xong rồi yêu cầu LLM tạo câu trả lời")
    RECORD = (4, "Ghi lại lệnh gọi tool vào lịch sử hội thoại, không gọi LLM")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class ActionResponse:
    def __init__(self, action: Action, result=None, response=None):
        self.action = action  # Loại hành động
        self.result = result  # Kết quả do hành động tạo ra
        self.response = response  # Nội dung trả lời trực tiếp


class FunctionItem:
    def __init__(self, name, description, func, type):
        self.name = name
        self.description = description
        self.func = func
        self.type = type


class DeviceTypeRegistry:
    """Sổ đăng ký loại thiết bị, dùng để quản lý các loại thiết bị IOT và hàm của chúng"""

    def __init__(self):
        self.type_functions = {}  # type_signature -> {func_name: FunctionItem}

    def generate_device_type_id(self, descriptor):
        """Tạo type ID từ mô tả năng lực thiết bị"""
        properties = sorted(descriptor["properties"].keys())
        methods = sorted(descriptor["methods"].keys())
        # Dùng tổ hợp thuộc tính và phương thức làm định danh duy nhất của loại thiết bị
        type_signature = (
            f"{descriptor['name']}:{','.join(properties)}:{','.join(methods)}"
        )
        return type_signature

    def get_device_functions(self, type_id):
        """Lấy mọi hàm tương ứng với loại thiết bị"""
        return self.type_functions.get(type_id, {})

    def register_device_type(self, type_id, functions):
        """Đăng ký loại thiết bị và các hàm của nó"""
        if type_id not in self.type_functions:
            self.type_functions[type_id] = functions


# Khởi tạo dict đăng ký hàm
all_function_registry = {}
# Map tên module -> danh sách tên hàm, dùng để mở rộng tên plugin cấp module thành tên hàm cụ thể
module_func_map = {}


def register_function(name, desc, type=None):
    """Decorator đăng ký hàm vào dict đăng ký hàm"""

    def decorator(func):
        all_function_registry[name] = FunctionItem(name, desc, func, type)
        # Ghi lại map tên module sang tên hàm, dùng để expand cấu hình plugin cấp module
        module_name = func.__module__.split(".")[-1]
        module_func_map.setdefault(module_name, []).append(name)
        logger.bind(tag=TAG).debug(f"Hàm '{name}' đã được load, có thể đăng ký dùng")
        return func

    return decorator


def register_device_function(name, desc, type=None):
    """Decorator đăng ký hàm cấp thiết bị vào dict đăng ký hàm"""

    def decorator(func):
        logger.bind(tag=TAG).debug(f"Hàm thiết bị '{name}' đã được load")
        return func

    return decorator


class FunctionRegistry:
    def __init__(self):
        self.function_registry = {}
        self.logger = setup_logging()

    def register_function(self, name, func_item=None):
        # Nếu đã cung cấp func_item, đăng ký trực tiếp
        if func_item:
            self.function_registry[name] = func_item
            self.logger.bind(tag=TAG).debug(f"Hàm '{name}' đăng ký trực tiếp thành công")
            return func_item

        # Ngược lại tìm trong all_function_registry
        func = all_function_registry.get(name)
        if not func:
            self.logger.bind(tag=TAG).error(f"Không tìm thấy hàm '{name}'")
            return None
        self.function_registry[name] = func
        self.logger.bind(tag=TAG).debug(f"Hàm '{name}' đăng ký thành công")
        return func

    def unregister_function(self, name):
        # Hủy đăng ký hàm, kiểm tra có tồn tại không
        if name not in self.function_registry:
            self.logger.bind(tag=TAG).error(f"Không tìm thấy hàm '{name}'")
            return False
        self.function_registry.pop(name, None)
        self.logger.bind(tag=TAG).info(f"Hàm '{name}' hủy đăng ký thành công")
        return True

    def get_function(self, name):
        return self.function_registry.get(name)

    def get_all_functions(self):
        return self.function_registry

    def get_all_function_desc(self):
        return [func.description for _, func in self.function_registry.items()]
