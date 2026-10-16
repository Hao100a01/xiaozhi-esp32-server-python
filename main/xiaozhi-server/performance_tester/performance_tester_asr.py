import asyncio
import logging
import os
import time
import concurrent.futures
from typing import Dict, Optional
import aiohttp
from tabulate import tabulate
from core.utils.asr import create_instance as create_stt_instance

# Đặt log level toàn cục là WARNING, nén log level INFO
logging.basicConfig(level=logging.WARNING)

description = "Test hiệu năng model nhận dạng giọng nói"

class ASRPerformanceTester:
    def __init__(self):
        self.config = self._load_config_from_data_dir()
        self.test_wav_list = self._load_test_wav_files()
        self.results = {"stt": {}}
        
        # Log debug
        print(f"[DEBUG] Cấu hình ASR đã load: {self.config.get('ASR', {})}")
        print(f"[DEBUG] Số lượng file audio: {len(self.test_wav_list)}")

    def _load_config_from_data_dir(self) -> Dict:
        """Load cấu hình của mọi file .config.yaml trong thư mục data"""
        config = {"ASR": {}}
        data_dir = os.path.join(os.getcwd(), "data")
        print(f"[DEBUG] Quét thư mục file cấu hình: {data_dir}")

        for root, _, files in os.walk(data_dir):
            for file in files:
                if file.endswith(".config.yaml"):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            import yaml
                            file_config = yaml.safe_load(f)
                            # Tương thích cấu hình ASR/asr hoa/thường
                            asr_config = file_config.get("ASR") or file_config.get("asr")
                            if asr_config:
                                config["ASR"].update(asr_config)
                                print(f"[DEBUG] Load cấu hình ASR từ {file_path} thành công")
                    except Exception as e:
                        print(f" Load file cấu hình {file_path} thất bại: {str(e)}")
        return config

    def _load_test_wav_files(self) -> list:
        """Load file audio dùng để test (thêm debug đường dẫn)"""
        wav_root = os.path.join(os.getcwd(), "config", "assets")
        print(f"[DEBUG] Thư mục file audio: {wav_root}")
        test_wav_list = []
        
        if os.path.exists(wav_root):
            file_list = os.listdir(wav_root)
            print(f"[DEBUG] Tìm thấy file audio: {file_list}")
            for file_name in file_list:
                file_path = os.path.join(wav_root, file_name)
                if os.path.getsize(file_path) > 300 * 1024:  # 300KB
                    with open(file_path, "rb") as f:
                        test_wav_list.append(f.read())
        else:
            print(f" Thư mục không tồn tại: {wav_root}")
        return test_wav_list

    async def _test_single_audio(self, stt_name: str, stt, audio_data: bytes) -> Optional[float]:
        """Test hiệu năng của một file audio"""
        try:
            start_time = time.time()
            text, _ = await stt.speech_to_text_wrapper([audio_data], "1", stt.audio_format)
            if text is None:
                return None
            
            duration = time.time() - start_time
            
            # Phát hiện thời gian bất thường 0.000s
            if abs(duration) < 0.001:  # Nhỏ hơn 1ms coi là bất thường
                print(f"{stt_name} phát hiện thời gian bất thường: {duration:.6f}s (coi là lỗi)")
                return None
                
            return duration
        except Exception as e:
            error_msg = str(e).lower()
            if "502" in error_msg or "bad gateway" in error_msg:
                print(f"{stt_name} gặp lỗi 502")
                return None
            return None

    async def _test_stt_with_timeout(self, stt_name: str, config: Dict) -> Dict:
        """Test async hiệu năng một STT, có kiểm soát timeout"""
        try:
            # Kiểm tra tính hợp lệ của cấu hình
            token_fields = ["access_token", "api_key", "token"]
            if any(
                field in config
                and str(config[field]).lower() in ["你的", "placeholder", "none", "null", ""]
                for field in token_fields
            ):
                print(f"  STT {stt_name} chưa cấu hình access_token/api_key hợp lệ, đã bỏ qua")
                return {
                    "name": stt_name,
                    "type": "stt",
                    "errors": 1,
                    "error_type": "Lỗi cấu hình"
                }

            module_type = config.get("type", stt_name)
            stt = create_stt_instance(module_type, config, delete_audio_file=True)
            stt.audio_format = "pcm"

            print(f" Test STT: {stt_name}")

            # Dùng thread pool và kiểm soát timeout
            loop = asyncio.get_event_loop()
            
            # Test file audio đầu tiên để kiểm tra kết nối
            try:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        lambda: asyncio.run(self._test_single_audio(stt_name, stt, self.test_wav_list[0]))
                    )
                    first_result = await asyncio.wait_for(
                        asyncio.wrap_future(future), timeout=10.0
                    )
                    
                    if first_result is None:
                        print(f" {stt_name} kết nối thất bại")
                        return {
                            "name": stt_name,
                            "type": "stt",
                            "errors": 1,
                            "error_type": "Lỗi mạng"
                        }
            except asyncio.TimeoutError:
                print(f" {stt_name} kết nối timeout (10 giây), bỏ qua")
                return {
                    "name": stt_name,
                    "type": "stt",
                    "errors": 1,
                    "error_type": "Timeout kết nối"
                }
            except Exception as e:
                error_msg = str(e).lower()
                if "502" in error_msg or "bad gateway" in error_msg:
                    print(f" {stt_name} gặp lỗi 502, bỏ qua")
                    return {
                        "name": stt_name,
                        "type": "stt",
                        "errors": 1,
                        "error_type": "Lỗi mạng 502"
                    }
                print(f" {stt_name} ngoại lệ kết nối: {str(e)}")
                return {
                    "name": stt_name,
                    "type": "stt",
                    "errors": 1,
                    "error_type": "Lỗi mạng"
                }

                       # Test toàn lượng, có kiểm soát timeout
            total_time = 0
            valid_tests = 0
            test_count = len(self.test_wav_list)
            
            for i, audio_data in enumerate(self.test_wav_list, 1):
                try:
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(
                            lambda: asyncio.run(self._test_single_audio(stt_name, stt, audio_data))
                        )
                        duration = await asyncio.wait_for(
                            asyncio.wrap_future(future), timeout=10.0
                        )
                        
                        if duration is not None and duration > 0.001:  
                            total_time += duration
                            valid_tests += 1
                            print(f" {stt_name} [{i}/{test_count}] tốn: {duration:.2f}s")
                        else:
                            print(f" {stt_name} [{i}/{test_count}] test thất bại (gồm bất thường 0.000s)")
                            
                except asyncio.TimeoutError:
                    print(f" {stt_name} [{i}/{test_count}] timeout (10 giây), bỏ qua")
                    continue
                except Exception as e:
                    error_msg = str(e).lower()
                    if "502" in error_msg or "bad gateway" in error_msg:
                        print(f" {stt_name} [{i}/{test_count}] lỗi 502, bỏ qua")
                        return {
                            "name": stt_name,
                            "type": "stt",
                            "errors": 1,
                            "error_type": "Lỗi mạng 502"
                        }
                    print(f" {stt_name} [{i}/{test_count}] ngoại lệ: {str(e)}")
                    continue
            # Kiểm tra số lượng test hợp lệ
            if valid_tests < test_count * 0.3:  # Ít nhất 30% thành công
                print(f" {stt_name} test thành công quá ít ({valid_tests}/{test_count}), mạng có thể không ổn định")
                return {
                    "name": stt_name,
                    "type": "stt",
                    "errors": 1,
                    "error_type": "Lỗi mạng"
                }

            if valid_tests == 0:
                return {
                    "name": stt_name,
                    "type": "stt",
                    "errors": 1,
                    "error_type": "Lỗi mạng"
                }

            avg_time = total_time / valid_tests
            return {
                "name": stt_name,
                "type": "stt",
                "avg_time": avg_time,
                "success_rate": f"{valid_tests}/{test_count}",
                "errors": 0,
            }

        except Exception as e:
            error_msg = str(e).lower()
            if "502" in error_msg or "bad gateway" in error_msg:
                error_type = "Lỗi mạng 502"
            elif "timeout" in error_msg:
                error_type = "Timeout kết nối"
            else:
                error_type = "Lỗi mạng"
            print(f"⚠️ {stt_name} test thất bại: {str(e)}")
            return {
                "name": stt_name,
                "type": "stt",
                "errors": 1,
                "error_type": error_type
            }

    def _print_results(self):
        """In kết quả test, sắp xếp theo thời gian phản hồi"""
        print("\n" + "=" * 50)
        print("Kết quả test hiệu năng ASR")
        print("=" * 50)

        if not self.results.get("stt"):
            print("Không có kết quả test khả dụng")
            return

        headers = ["Tên model", "Thời gian TB (s)", "Tỷ lệ thành công", "Trạng thái"]
        table_data = []

        # Thu thập mọi dữ liệu và phân loại
        valid_results = []
        error_results = []

        for name, data in self.results["stt"].items():
            if data["errors"] == 0:
                # Kết quả bình thường
                avg_time = f"{data['avg_time']:.3f}"
                success_rate = data.get("success_rate", "N/A")
                status = "✅ Bình thường"
                
                # Lưu giá trị dùng để sắp xếp
                sort_key = data["avg_time"]
                
                valid_results.append({
                    "name": name,
                    "avg_time": avg_time,
                    "success_rate": success_rate,
                    "status": status,
                    "sort_key": sort_key,
                })
            else:
                # Kết quả lỗi
                avg_time = "-"
                success_rate = "0/N"
                
                # Lấy loại lỗi cụ thể
                error_type = data.get("error_type", "Lỗi mạng")
                status = f"❌ {error_type}"
                
                error_results.append([name, avg_time, success_rate, status])

        # Sắp xếp tăng dần theo thời gian phản hồi (nhanh đến chậm)
        valid_results.sort(key=lambda x: x["sort_key"])

        # Chuyển kết quả hợp lệ đã sắp xếp thành dữ liệu bảng
        for result in valid_results:
            table_data.append([
                result["name"],
                result["avg_time"],
                result["success_rate"],
                result["status"],
            ])

        # Thêm kết quả lỗi vào cuối dữ liệu bảng
        table_data.extend(error_results)

        print(tabulate(table_data, headers=headers, tablefmt="grid"))
        print("\nGiải thích test:")
        print("- Kiểm soát timeout: mỗi audio chờ tối đa 10 giây")
        print("- Xử lý lỗi: tự động bỏ qua model gặp lỗi 502, timeout và ngoại lệ mạng")
        print("- Tỷ lệ thành công: số audio nhận dạng thành công/tổng số audio test")
        print("- Quy tắc sắp xếp: theo thời gian TB từ nhanh đến chậm, model lỗi xếp cuối")
        print("\nTest hoàn tất!")

    async def run(self):
        """Thực hiện test async toàn lượng""" 
        print("Bắt đầu lọc các module ASR khả dụng...")
        if not self.config.get("ASR"):
            print("Không tìm thấy module ASR trong cấu hình")
            return

        all_tasks = []
        for stt_name, config in self.config["ASR"].items():
            # Kiểm tra tính hợp lệ của cấu hình
            token_fields = ["access_token", "api_key", "token"]
            if any(
                field in config
                and str(config[field]).lower() in ["你的", "placeholder", "none", "null", ""]
                for field in token_fields
            ):
                print(f"ASR {stt_name} chưa cấu hình access_token/api_key hợp lệ, đã bỏ qua")
                continue
            
            print(f"Thêm task test ASR: {stt_name}")
            all_tasks.append(self._test_stt_with_timeout(stt_name, config))

        if not all_tasks:
            print("Không có module ASR khả dụng để test.")
            return

        print(f"\nTìm thấy {len(all_tasks)} module ASR khả dụng")
        print("\nBắt đầu test đồng thời mọi module ASR...")
        all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # Xử lý kết quả
        for result in all_results:
            if isinstance(result, dict) and result.get("type") == "stt":
                self.results["stt"][result["name"]] = result

        # In kết quả
        self._print_results()


async def main():
    tester = ASRPerformanceTester()
    await tester.run()


if __name__ == "__main__":
    asyncio.run(main())