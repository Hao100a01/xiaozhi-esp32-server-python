# LƯU Ý: NHỮNG CHỖ KHÔNG DỊCH ĐƯỢC / KHÔNG NÊN DỊCH

Repo: `Hao100a01/xiaozhi-esp32-server-python` — việt hoá phần server Python (`main/xiaozhi-server`), ngày 15/10/2026.

Nguyên tắc chung: mọi identifier code (tên biến/hàm/class, key YAML/JSON, tên model, URL, đường dẫn file, API key mẫu) được giữ nguyên tuyệt đối. Chỉ dịch comment, log message, chuỗi hiển thị/nói ra cho người dùng, và description của tool/plugin (LLM đọc).

---

## 1. Từ khóa code so khớp trực tiếp (đổi là vỡ chương trình)

| Chuỗi (giữ nguyên) | Vị trí | Lý do |
|---|---|---|
| `未知说话人` ("người nói chưa xác định") | `core/connection.py`, `core/utils/dialogue.py`, `core/utils/voiceprint_provider.py`, `agent-base-prompt.txt` | Code so sánh `==`/`!=` để nhận biết giọng nói chưa định danh; prompt template cũng phải khớp đúng giá trị hệ thống gửi |
| `你的` (trong `["你的", "placeholder", ...]`) | `performance_tester/performance_tester_*.py` | Code dùng để phát hiện API key chưa cấu hình (placeholder) |
| `无法获取详细内容` | `plugins_func/functions/get_news_from_newsnow.py`, `get_news_from_chinanews.py` | Vừa là giá trị `return` của `fetch_news_detail()`, vừa là điều kiện so sánh `==` ở caller — phải giống hệt nhau |
| `["播放音乐"]`, `f"播放音乐 {song_name}"`, `"随机播放音乐"` | `plugins_func/functions/play_music.py` | Chuỗi nội bộ: code tự dựng music_intent rồi tự parse để tách tên bài hát — đổi một chỗ mà sót chỗ kia là vỡ |
| `wakeup_words` (vd "你好小智") | `config.yaml` | Model wake-word chỉ nhận tiếng Trung; comment đã dịch sang tiếng Việt để giải thích |
| `exit_commands: ["退出", "关闭"]` | `config.yaml` | Code so khớp từ khóa để thoát; comment đã dịch sang tiếng Việt |

## 2. Giá trị mặc định là placeholder (người dùng phải tự điền)

| Vị trí | Nội dung |
|---|---|
| `config.yaml` — các `api_key:`, URL mẫu | Giữ nguyên dạng placeholder (vd "你的chat-glm web key"); comment tiếng Việt đã hướng dẫn lấy key ở đâu |
| `config_from_api.yaml` — `vision_explain`, `secret` | Đã việt hoá placeholder thành `http://IP_HOAC_TEN_MIEN:CONG/...` và `GIÁ_TRỊ_server.secret_CỦA_BẠN` để rõ là cần thay |
| `mcp_server_settings.json` | Giữ nguyên command/URL mẫu (`YOUR_HA_HOST`, `Bearer <redacted> TOKEN`); chỉ dịch các trường `des` |

## 3. Dữ liệu / tài nguyên không nên dịch

- **Tên file nhạc** `music/*.mp3` (vd `一念千年_国风版.mp3`): tên file được code quét và so khớp — không đổi tên.
- **File model nhị phân** (`models/.../*.onnx`, `*.jit`, `*.bpe.model`): chứa byte trùng vùng mã Hán nhưng là dữ liệu model, không phải text.
- **Default news sources** `"澎湃新闻;百度热搜;财联社"` (`plugins_func/functions/get_news_from_newsnow.py`, `core/providers/tools/server_plugins/plugin_executor.py`): tên các báo Trung Quốc làm nguồn mặc định cho tool tin tức. Sếp có thể đổi sang nguồn tin tiếng Việt khi cấu hình tool (qua `news_sources`).
- **Điều khoản an toàn** trong `agent-base-prompt.txt` (`<safety_compliance>` về Taiwan): nội dung tiếng Anh của repo gốc, không thuộc phạm vi việt hoá ngôn ngữ — giữ nguyên.

## 4. Đã dịch nhưng cần sếp biết (quyết định chủ đích)

- `config.yaml` → `prompt:`: viết lại persona thành trợ lý AI nói tiếng Việt, thân thiện, ngắn gọn (thay persona "gái Đài Loan" mặc định của repo gốc).
- `agent-base-prompt.txt` → cơ chế exit: giữ từ khóa tiếng Trung + **thêm** "tạm biệt", "bye", "chào nhé", "ngủ ngon" để nhận diện tiếng Việt; các ví dụ phong cách (banned clichés, colloquialisms, tool-call examples) đã chuyển sang tương đương tiếng Việt.
- `core/connection.py` → few-shot examples dạy LLM gọi tool: dịch sang tiếng Việt cho nhất quán (giữ nguyên tên tool/param và cấu trúc JSON).
- `config.yaml` → `server.websocket`, `vision_explain`: comment đã dịch; **sếp nhớ thay IP LAN của server khi deploy**.
- `TTS.EdgeTTS.voice` mặc định vẫn là `zh-CN-XiaoxiaoNeural` (giọng Trung) — khi deploy cho người Việt, đổi thành `vi-VN-HoaiMyNeural` (xem bộ deploy trong `workspace/your_files/xiaozhi-deploy-python/` đã chỉnh sẵn).

## 5. Ngoài phạm vi (không đụng tới)

`docs/`, `manager-web/`, `manager-api/`, `manager-mobile/` — theo yêu cầu, console web đã có tiếng Việt hoàn chỉnh.

## 6. File test — chuỗi tiếng Trung là test data (dịch sẽ làm test fail)
- `tests/core/providers/asr/test_lang_tag_filter.py`: input/expected của `lang_tag_filter` (vd `"你好啊，测试测试。"`) — không sửa.
- `tests/core/utils/test_current_time.py`: `"星期三"`, `"年"` là output thật của code đang được assert — không sửa.
- `tests/core/utils/test_textUtils.py`: `"你"`, `"，"`, `"你好"` là input/expected của hàm xử lý chuỗi — không sửa.
- `tests/core/utils/test_instance_creators.py`: `assert "不支持" in str(exc_info.value)` khớp message lỗi thật từ source — không sửa.
- Test data trong các file đã dịch (giữ nguyên, có comment `# Dữ liệu test:` đánh dấu): input hội thoại giả lập LLM (`"你是助手"`, `"介绍一下北京"`), RAG (`"我喜欢喝咖啡"`, `"咖啡"`), TTS (`"测试一下"`), chuỗi Nhật/Trung kiểm tra cắt câu trong `test_quote_safe_segment.py`.
- Pattern placeholder `"你的"` trong `tests/conftest.py`, `tests/test_llm.py`: code dùng để phát hiện key chưa cấu hình — giữ nguyên.
- `assertRaisesRegex(Exception, '已关闭')` trong `tests/test_manage_api_client_lifecycle.py`: khớp message lỗi thật từ `manage_api_client.py` — giữ nguyên, đã có comment tiếng Việt giải thích tại chỗ.

## 7. Bổ sung từ config.yaml (1175 dòng, đã việt hoá toàn bộ comment)

- `module_test.test_sentences`: câu test mẫu tiếng Trung — là dữ liệu test, giữ nguyên.
- `default_location: "广州"`, `news_sources: "澎湃新闻;百度热搜;财联社"`: identifier truyền cho API thời tiết/tin tức — giữ nguyên (sếp đổi khi cấu hình).
- `home_assistant.devices` mẫu, `voiceprint.speakers` mẫu (vd "张三"): dữ liệu ví dụ — giữ nguyên.
- `FishSpeech.reference_text`: text mẫu khớp với file audio `config/assets/wakeup_words.wav` — giữ nguyên (đổi text mà không đổi audio mẫu sẽ lệch voice clone).
- Tên riêng giữ nguyên trong comment đã dịch: `智控台` (dịch kèm: "Bảng điều khiển thông minh (智控台)"), `秘塔` (Metaso), `湾湾小何` (tên giọng TTS), ví dụ pinyin `"处理/(chu3)(li3)"`.

## 8. Bổ sung từ core/providers/ (83/87 file đã dịch)

- `"你的" in mcp_endpoint_url` (`core/providers/tools/mcp_endpoint/mcp_endpoint_handler.py:16`, `unified_tool_handler.py:91`): sentinel kiểm tra user đã thay placeholder MCP endpoint trong config chưa — giữ nguyên.
- `default_pronunciation_dict = {"tone": ["处理/(chu3)(li3)", "危险/dangerous"]}` (`core/providers/tts/minimax_httpstream.py`): dữ liệu phát âm TTS (từ → pinyin) — giữ nguyên.
- `「今日のこと」` trong comment `core/providers/asr/utils.py`: ví dụ tiếng Nhật, không phải tiếng Trung — giữ nguyên.
- `core/providers/vllm/openai.py`: đổi `(请使用中文回复)` → `(vui lòng trả lời bằng tiếng Việt)` — **chủ đích đổi hành vi runtime**: VLLM giờ được yêu cầu trả lời tiếng Việt thay vì tiếng Trung, phù hợp bản việt hoá.

## 9. Bổ sung từ core/ (trừ providers) — 46/46 file đã dịch

- `"你"` trong `http_server.py:30` (`"你" not in websocket_config`), `util.py:182` (`"你" in modelKey`): marker nhận biết placeholder trong phép so sánh — giữ nguyên.
- `"你的"` trong `ota_handler.py:138`, `util.py:586`: tương tự — giữ nguyên.
- `connection.py:284` `"智能体未找到"`: chuỗi lỗi của hệ console/API ngoài, trích dẫn trong comment để đối chiếu — giữ nguyên văn.
- `connection.py:1807,1812`: ví dụ ký tự rác JSON cho hàm cleaner — là data, giữ byte-identical.
- `textUtils.py` / `tts.py`: literal dấu câu (`，` `。` `！` `、` …) là data trong set xử lý chuỗi — chỉ dịch comment.
- `current_time.py`: tên năm/tháng/ngày âm lịch do **thư viện cnlunar** trả về (data tiếng Trung) — không đổi được.
- `prompt_manager.py:282`: default TTS language `or "中文"` → `or "Tiếng Việt"` — chỉ render vào prompt template, **không bị code so khớp**, phù hợp mục tiêu việt hoá.
- `WEEKDAY_MAP` (current_time.py, prompt_manager.py): Thứ Hai…Chủ Nhật — key lookup (`"Monday"`…) giữ nguyên nên code chạy bình thường; test `tests/core/utils/test_current_time.py` đã cập nhật expectation sang `"Thứ Tư"`.
- `core/utils/util.py`: `system_error_response` mặc định trong code → `"Chủ nhân ơi, Tiểu Trí giờ hơi bận, mình thử lại sau nhé."` ("Tiểu Trí" là phiên âm để TTS đọc được; bản trong `config.yaml` vẫn ưu tiên khi có file config).

## 10. Bổ sung từ plugins_func / config / performance_tester (28 file đã dịch)

- `app.py`, `config/manage_api_client.py`: `"你" in auth_key/secret`, `"你" not in mcp_endpoint`, `"你的接入点 websocket地址"` — sentinel phát hiện cấu hình chưa điền — giữ nguyên.
- `performance_tester_stream_asr.py`, `performance_tester_stream_tts.py`: status `"成功(...)"` → `"Thành công (...)"` **kèm cập nhật đồng bộ** điều kiện lọc `"成功" in r["status"]` → `"Thành công" in r["status"]` (2 file, cả nhánh success/failed) — nếu không, mọi kết quả thành công sẽ bị xếp loại thất bại.
- `performance_tester_llm.py`: `"异常"/"错误" in chunk_str` — keyword dò lỗi trên output thô của LLM — giữ nguyên.
- `change_role.py`: key dict `英语老师/机车女友/好奇小男孩` — **identifier**, LLM phải emit đúng key để `prompts[role]` khớp; persona đã dịch sang tiếng Việt. Muốn đổi tên vai trò sang tiếng Việt thì phải đổi đồng thời key + description.
- `get_news_from_newsnow.py`: key `CHANNEL_MAP`, `DEFAULT_NEWS_SOURCES`, default `source="澎湃新闻"` — identifier khớp config `news_sources`; `get_news_from_chinanews.py`: key `category_map` (社会/国际/财经...) map sang key config RSS — giữ nguyên.
- `get_weather.py`: `WEATHER_CODE_MAP` dịch toàn bộ **value** (晴→Nắng, 多云→Nhiều mây...) — key là mã số nên an toàn; `"杭州"` trong ví dụ description, `"广州"` (default_location), `"lang=zh"`, `"zh_CN"` — API-facing, giữ nguyên.
- Brand trong tester dịch sang tên tiếng Anh chuẩn: 豆包→Doubao, 通义千问→Qwen, 讯飞→iFlytek, 火山引擎→Volcengine (chỉ là chuỗi hiển thị).
- `config/settings.py`: `智控台` — tên sản phẩm console quản trị, giữ nguyên.

---
*(Các subagent còn lại báo cáo bổ sung sẽ được tổng hợp vào đây khi hoàn tất.)*
