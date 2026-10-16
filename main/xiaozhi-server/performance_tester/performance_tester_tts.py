import asyncio
import logging
import os
import time
import threading
from typing import Dict
import yaml
from tabulate import tabulate

# Đảm bảo import create_tts_instance từ core.utils.tts
from core.utils.tts import create_instance as create_tts_instance
from config.settings import load_config

# Đặt log level toàn cục là WARNING
logging.basicConfig(level=logging.WARNING)

description = "Test hiệu năng tổng hợp giọng nói không streaming"


class TTSPerformanceTester:
    def __init__(self, config):
        self.config = config
        self.test_sentences = self.config.get("module_test", {}).get(
            "test_sentences",
            [
                "Năm Vĩnh Hòa thứ chín, năm Quý Sửu, đầu cuối mùa xuân;",
                "Người ta giao du với nhau, trong một đời ngẩng cúi, có người gửi gắm tâm tình, trò chuyện trong một gian phòng; có người phóng khoáng, thả hồn ngoài hình hài. Dù sở thích muôn vẻ, tính tình khác nhau,",
                "Mỗi khi xét nguyên do cảm hứng của người xưa, như cùng một mối, chưa từng không cảm khái than thở trước văn chương, không sao tỏ hết nỗi lòng. Mới biết coi sống chết là một là hư vọng, lấy Bành Tổ ngang với chết yểu là ngụy tạo.",
            ],
        )
        self.results = {}

    async def _test_tts(self, tts_name: str, config: Dict) -> Dict:
        """Test hiệu năng của một module TTS"""
        try:
            token_fields = ["access_token", "api_key", "token"]
            if any(
                field in config
                and any(x in config[field] for x in ["你的", "placeholder"])
                for field in token_fields
            ):
                print(f"TTS {tts_name} chưa cấu hình access_token/api_key, đã bỏ qua")
                return {"name": tts_name, "errors": 1}

            module_type = config.get("type", tts_name)
            tts = create_tts_instance(module_type, config, delete_audio_file=True)

            # Đặt object mock conn, tránh việc implementation TTS truy cập self.conn.sample_rate bị None
            class MockConn:
                sample_rate = 16000
                audio_format = "pcm"
                stop_event = threading.Event()  # Cần là object Event thật
                client_abort = False
                headers = {}
            tts.conn = MockConn()

            # Đặt mock opus_encoder, tránh một số TTS truy cập self.opus_encoder bị None
            class MockOpusEncoder:
                pass
            if not hasattr(tts, 'opus_encoder') or tts.opus_encoder is None:
                tts.opus_encoder = MockOpusEncoder()

            print(f"Test TTS: {tts_name}")

            # Test kết nối
            tmp_file = tts.generate_filename()
            await tts.text_to_speak("Test kết nối", tmp_file)

            if not tmp_file or not os.path.exists(tmp_file):
                print(f"{tts_name} kết nối thất bại")
                return {"name": tts_name, "errors": 1}

            total_time = 0
            test_count = len(self.test_sentences[:3])

            for i, sentence in enumerate(self.test_sentences[:2], 1):
                start = time.time()
                tmp_file = tts.generate_filename()
                await tts.text_to_speak(sentence, tmp_file)
                duration = time.time() - start
                total_time += duration

                if tmp_file and os.path.exists(tmp_file):
                    print(f"{tts_name} [{i}/{test_count}] test thành công")
                else:
                    print(f"{tts_name} [{i}/{test_count}] test thất bại")
                    return {"name": tts_name, "errors": 1}

            return {
                "name": tts_name,
                "avg_time": total_time / test_count,
                "errors": 0,
            }

        except Exception as e:
            print(f"{tts_name} test thất bại: {str(e)}")
            return {"name": tts_name, "errors": 1}

    def _print_results(self):
        """In kết quả test"""
        if not self.results:
            print("Không có kết quả test TTS hợp lệ")
            return

        headers = ["Module TTS", "Thời gian TB (giây)", "Số câu test", "Trạng thái"]
        table_data = []

        # Thu thập mọi dữ liệu và phân loại
        valid_results = []
        error_results = []

        for name, data in self.results.items():
            if data["errors"] == 0:
                # Kết quả bình thường
                avg_time = f"{data['avg_time']:.3f}"
                test_count = len(self.test_sentences[:3])
                status = "✅ Bình thường"
                
                # Lưu giá trị dùng để sắp xếp
                valid_results.append({
                    "name": name,
                    "avg_time": avg_time,
                    "test_count": test_count,
                    "status": status,
                    "sort_key": data['avg_time']
                })
            else:
                # Kết quả lỗi
                avg_time = "-"
                test_count = "0/3"
                
                # Loại lỗi mặc định là lỗi mạng
                error_type = "Lỗi mạng"
                status = f"❌ {error_type}"
                
                error_results.append([name, avg_time, test_count, status])

        # Sắp xếp tăng dần theo thời gian TB
        valid_results.sort(key=lambda x: x["sort_key"])

        # Chuyển kết quả hợp lệ đã sắp xếp thành dữ liệu bảng
        for result in valid_results:
            table_data.append([
                result["name"],
                result["avg_time"],
                result["test_count"],
                result["status"]
            ])

        # Thêm kết quả lỗi vào cuối dữ liệu bảng
        table_data.extend(error_results)

        print("\nKết quả test hiệu năng TTS:")
        print(
            tabulate(
                table_data,
                headers=headers,
                tablefmt="grid",
                colalign=("left", "right", "right", "left"),
            )
        )
        print("\nGiải thích test:")
        print("- Kiểm soát timeout: mỗi request chờ tối đa 10 giây")
        print("- Xử lý lỗi: không kết nối được và timeout được xếp là lỗi mạng")
        print("- Quy tắc sắp xếp: theo thời gian TB từ nhanh đến chậm")

    async def run(self):
        """Thực hiện test"""
        print("Bắt đầu test hiệu năng TTS...")

        if not self.config.get("TTS"):
            print("Không tìm thấy cấu hình TTS trong file cấu hình")
            return

        # Duyệt mọi cấu hình TTS
        tasks = []
        for tts_name, config in self.config.get("TTS", {}).items():
            tasks.append(self._test_tts(tts_name, config))

        # Thực hiện test đồng thời
        results = await asyncio.gather(*tasks)

        # Lưu mọi kết quả, gồm cả lỗi
        for result in results:
            self.results[result["name"]] = result

        # In kết quả
        self._print_results()


# Để đáp ứng nhu cầu gọi của performance_tester.py
async def main():
    config = await load_config()
    tester = TTSPerformanceTester(config)
    await tester.run()


if __name__ == "__main__":
    asyncio.run(main())
