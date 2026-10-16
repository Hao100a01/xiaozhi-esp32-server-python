import time
import asyncio
import logging
import statistics
import base64
from typing import Dict
from tabulate import tabulate
from core.utils.vllm import create_instance
from config.settings import load_config

# Đặt log level toàn cục là WARNING, nén log level INFO
logging.basicConfig(level=logging.WARNING)

description = "Test hiệu năng model nhận dạng hình ảnh"


class AsyncVisionPerformanceTester:
    def __init__(self, config):
        self.config = config

        self.test_images = [
            "../../docs/images/demo1.png",
            "../../docs/images/demo2.png",
        ]
        self.test_questions = [
            "Trong bức ảnh này có gì?",
            "Vui lòng mô tả chi tiết nội dung của bức ảnh này",
        ]

        # Load ảnh test
        self.results = {"vllm": {}}

    async def _test_vllm(self, vllm_name: str, config: Dict) -> Dict:
        """Test async hiệu năng một model vision lớn"""
        try:
            # Kiểm tra cấu hình API key
            if "api_key" in config and any(
                x in config["api_key"] for x in ["你的", "placeholder", "sk-xxx"]
            ):
                print(f"⏭️  VLLM {vllm_name} chưa cấu hình api_key, đã bỏ qua")
                return {"name": vllm_name, "type": "vllm", "errors": 1}

            # Lấy type thực tế (tương thích cấu hình cũ)
            module_type = config.get("type", vllm_name)
            vllm = create_instance(module_type, config)

            print(f"🖼️ Test VLLM: {vllm_name}")

            # Tạo mọi task test
            test_tasks = []
            for question in self.test_questions:
                for image in self.test_images:
                    test_tasks.append(
                        self._test_single_vision(vllm_name, vllm, question, image)
                    )

            # Thực hiện đồng thời mọi test
            test_results = await asyncio.gather(*test_tasks)

            # Xử lý kết quả
            valid_results = [r for r in test_results if r is not None]
            if not valid_results:
                print(f"⚠️  {vllm_name} không có dữ liệu hợp lệ, có thể lỗi cấu hình")
                return {"name": vllm_name, "type": "vllm", "errors": 1}

            response_times = [r["response_time"] for r in valid_results]

            # Lọc dữ liệu bất thường
            mean = statistics.mean(response_times)
            stdev = statistics.stdev(response_times) if len(response_times) > 1 else 0
            filtered_times = [t for t in response_times if t <= mean + 3 * stdev]

            if len(filtered_times) < len(test_tasks) * 0.5:
                print(f"⚠️  {vllm_name} dữ liệu hợp lệ không đủ, mạng có thể không ổn định")
                return {"name": vllm_name, "type": "vllm", "errors": 1}

            return {
                "name": vllm_name,
                "type": "vllm",
                "avg_response": sum(response_times) / len(response_times),
                "std_response": (
                    statistics.stdev(response_times) if len(response_times) > 1 else 0
                ),
                "errors": 0,
            }

        except Exception as e:
            print(f"⚠️ VLLM {vllm_name} test thất bại: {str(e)}")
            return {"name": vllm_name, "type": "vllm", "errors": 1}

    async def _test_single_vision(
        self, vllm_name: str, vllm, question: str, image: str
    ) -> Dict:
        """Test hiệu năng của một câu hỏi vision"""
        try:
            print(f"📝 {vllm_name} bắt đầu test: {question[:20]}...")
            start_time = time.time()

            # Đọc ảnh và chuyển sang base64
            with open(image, "rb") as image_file:
                image_data = image_file.read()
                image_base64 = base64.b64encode(image_data).decode("utf-8")

            # Lấy response trực tiếp
            response = vllm.response(question, image_base64)
            response_time = time.time() - start_time
            print(f"✓ {vllm_name} hoàn tất response: {response_time:.3f}s")

            return {
                "name": vllm_name,
                "type": "vllm",
                "response_time": response_time,
            }
        except Exception as e:
            print(f"⚠️ {vllm_name} test thất bại: {str(e)}")
            return None

    def _print_results(self):
        """In kết quả test"""
        vllm_table = []
        for name, data in self.results["vllm"].items():
            if data["errors"] == 0:
                stability = data["std_response"] / data["avg_response"]
                vllm_table.append(
                    [
                        name,
                        f"{data['avg_response']:.3f} giây",
                        f"{stability:.3f}",
                    ]
                )

        if vllm_table:
            print("\nXếp hạng hiệu năng model vision lớn:\n")
            print(
                tabulate(
                    vllm_table,
                    headers=["Tên model", "Thời gian phản hồi", "Độ ổn định"],
                    tablefmt="github",
                    colalign=("left", "right", "right"),
                    disable_numparse=True,
                )
            )
        else:
            print("\n⚠️ Không có model vision lớn khả dụng để test.")

    async def run(self):
        """Thực hiện test async toàn lượng"""
        print("🔍 Bắt đầu lọc các model vision lớn khả dụng...")

        if not self.test_images:
            print(f"\n⚠️  Trong đường dẫn {self.image_root} không có file ảnh, không thể test")
            return

        # Tạo mọi task test
        all_tasks = []

        # Task test VLLM
        if self.config.get("VLLM") is not None:
            for vllm_name, config in self.config.get("VLLM", {}).items():
                if "api_key" in config and any(
                    x in config["api_key"] for x in ["你的", "placeholder", "sk-xxx"]
                ):
                    print(f"⏭️  VLLM {vllm_name} chưa cấu hình api_key, đã bỏ qua")
                    continue
                print(f"🖼️ Thêm task test VLLM: {vllm_name}")
                all_tasks.append(self._test_vllm(vllm_name, config))

        print(f"\n✅ Tìm thấy {len(all_tasks)} model vision lớn khả dụng")
        print(f"✅ Dùng {len(self.test_images)} ảnh test")
        print(f"✅ Dùng {len(self.test_questions)} câu hỏi test")
        print("\n⏳ Bắt đầu test đồng thời mọi model...\n")

        # Thực hiện đồng thời mọi task test
        all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # Xử lý kết quả
        for result in all_results:
            if isinstance(result, dict) and result["errors"] == 0:
                self.results["vllm"][result["name"]] = result

        # In kết quả
        print("\n📊 Đang tạo báo cáo test...")
        self._print_results()


async def main():
    config = await load_config()
    tester = AsyncVisionPerformanceTester(config)
    await tester.run()


if __name__ == "__main__":
    asyncio.run(main())
