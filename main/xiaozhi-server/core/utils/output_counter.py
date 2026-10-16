import datetime
from typing import Dict, Tuple

# Dict toàn cục, lưu số từ output mỗi ngày của từng thiết bị
_device_daily_output: Dict[Tuple[str, datetime.date], int] = {}
# Ghi lại ngày kiểm tra gần nhất
_last_check_date: datetime.date = None


def reset_device_output():
    """
    Đặt lại số từ output mỗi ngày của mọi thiết bị
    Gọi hàm này lúc 0 giờ mỗi ngày
    """
    _device_daily_output.clear()


def get_device_output(device_id: str) -> int:
    """
    Lấy số từ output trong ngày của thiết bị
    """
    current_date = datetime.datetime.now().date()
    return _device_daily_output.get((device_id, current_date), 0)


def add_device_output(device_id: str, char_count: int):
    """
    Tăng số từ output của thiết bị
    """
    current_date = datetime.datetime.now().date()
    global _last_check_date

    # Nếu là lần gọi đầu tiên hoặc ngày đã thay đổi, xóa bộ đếm
    if _last_check_date is None or _last_check_date != current_date:
        _device_daily_output.clear()
        _last_check_date = current_date

    current_count = _device_daily_output.get((device_id, current_date), 0)
    _device_daily_output[(device_id, current_date)] = current_count + char_count


def check_device_output_limit(device_id: str, max_output_size: int) -> bool:
    """
    Kiểm tra thiết bị có vượt giới hạn output không
    :return: True nếu vượt giới hạn, False nếu chưa vượt
    """
    if not device_id:
        return False
    current_output = get_device_output(device_id)
    return current_output >= max_output_size
