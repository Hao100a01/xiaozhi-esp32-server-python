import importlib
import pkgutil
from config.logger import setup_logging

TAG = __name__

logger = setup_logging()

def auto_import_modules(package_name):
    """
    Tự động import mọi module trong package chỉ định.

    Args:
        package_name (str): Tên package, ví dụ 'functions'.
    """
    # Lấy đường dẫn của package
    package = importlib.import_module(package_name)
    package_path = package.__path__

    # Duyệt mọi module trong package
    for _, module_name, _ in pkgutil.iter_modules(package_path):
        # Import module
        full_module_name = f"{package_name}.{module_name}"
        importlib.import_module(full_module_name)
        #logger.bind(tag=TAG).info(f"Module '{full_module_name}' đã được load")