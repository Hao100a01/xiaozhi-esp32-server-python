import os
import asyncio
from config.config_loader import read_config, get_project_dir, load_config


default_config_file = "config.yaml"
config_file_valid = False


def check_config_file():
    global config_file_valid
    if config_file_valid:
        return
    """
    Kiểm tra cấu hình đơn giản, chỉ thông báo tình trạng dùng file cấu hình
    """
    custom_config_file = get_project_dir() + "data/." + default_config_file
    if not os.path.exists(custom_config_file):
        raise FileNotFoundError(
            "Không tìm thấy file data/.config.yaml, vui lòng theo hướng dẫn kiểm tra file cấu hình này có tồn tại không"
        )

    # Kiểm tra có đọc cấu hình từ API không
    config = asyncio.run(load_config())
    if config.get("read_config_from_api", False):
        print("Đọc cấu hình từ API")
        old_config_origin = read_config(custom_config_file)
        if old_config_origin.get("selected_module") is not None:
            error_msg = "File cấu hình của bạn dường như chứa cả cấu hình 智控台 lẫn cấu hình local:\n"
            error_msg += "\nGợi ý:\n"
            error_msg += "1. Copy file config_from_api.yaml ở thư mục gốc vào thư mục data, đổi tên thành .config.yaml\n"
            error_msg += "2. Theo hướng dẫn cấu hình địa chỉ API và khóa bí mật\n"
            raise ValueError(error_msg)
    config_file_valid = True
