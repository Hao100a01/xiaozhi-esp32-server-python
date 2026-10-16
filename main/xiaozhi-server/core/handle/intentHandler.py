import json
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import ContentType
from core.handle.helloHandle import checkWakeupWords
from plugins_func.register import Action, ActionResponse
from core.handle.sendAudioHandle import send_stt_message
from core.handle.reportHandle import enqueue_tool_report
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType

TAG = __name__


async def handle_user_intent(conn: "ConnectionHandler", text):
    # Tiền xử lý text đầu vào, xử lý định dạng JSON có thể có
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                text = parsed_data["content"]  # Trích xuất content để phân tích ý định
                conn.current_speaker = parsed_data.get("speaker")  # Giữ lại thông tin người nói
    except (json.JSONDecodeError, TypeError):
        pass

    # Kiểm tra có lệnh thoát rõ ràng không
    _, filtered_text = remove_punctuation_and_length(text)
    if await check_direct_exit(conn, filtered_text):
        return True

    # Kiểm tra có phải từ đánh thức
    if await checkWakeupWords(conn, filtered_text):
        return True

    if conn.intent_type == "function_call":
        # Dùng phương thức chat hỗ trợ function calling, không phân tích ý định nữa
        return False
    # Dùng LLM phân tích ý định
    intent_result = await analyze_intent_with_llm(conn, text)
    if not intent_result:
        return False
    # Sinh sentence_id khi phiên bắt đầu
    conn.sentence_id = str(uuid.uuid4().hex)
    # Xử lý các ý định
    return await process_intent_result(conn, intent_result, text)


async def check_direct_exit(conn: "ConnectionHandler", text):
    """Kiểm tra có lệnh thoát rõ ràng không"""
    _, text = remove_punctuation_and_length(text)
    cmd_exit = conn.cmd_exit
    for cmd in cmd_exit:
        if text == cmd:
            conn.logger.bind(tag=TAG).info(f"Nhận diện được lệnh thoát rõ ràng: {text}")
            await send_stt_message(conn, text)
            await conn.close()
            return True
    return False


async def analyze_intent_with_llm(conn: "ConnectionHandler", text):
    """Dùng LLM phân tích ý định người dùng"""
    if not hasattr(conn, "intent") or not conn.intent:
        conn.logger.bind(tag=TAG).warning("Dịch vụ nhận diện ý định chưa khởi tạo")
        return None

    # Lịch sử hội thoại
    dialogue = conn.dialogue
    try:
        intent_result = await conn.intent.detect_intent(conn, dialogue.dialogue, text)
        return intent_result
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"Nhận diện ý định thất bại: {str(e)}")

    return None


async def process_intent_result(
    conn: "ConnectionHandler", intent_result, original_text
):
    """Xử lý kết quả nhận diện ý định"""
    try:
        # Thử phân tích kết quả thành JSON
        intent_data = json.loads(intent_result)

        # Kiểm tra có function_call không
        if "function_call" in intent_data:
            # Lấy trực tiếp function_call từ nhận diện ý định
            conn.logger.bind(tag=TAG).debug(
                f"Phát hiện kết quả ý định định dạng function_call: {intent_data['function_call']['name']}"
            )
            function_name = intent_data["function_call"]["name"]
            if function_name == "continue_chat":
                return False

            if function_name == "result_for_context":
                await send_stt_message(conn, original_text)
                conn.client_abort = False

                def process_context_result():
                    conn.dialogue.put(Message(role="user", content=original_text))

                    from core.utils.current_time import get_current_time_info

                    current_time, today_date, today_weekday, lunar_date = (
                        get_current_time_info()
                    )

                    # Dựng prompt cơ sở kèm ngữ cảnh
                    context_prompt = f"""Thời gian hiện tại: {current_time}
                                        Ngày hôm nay: {today_date} ({today_weekday})
                                        Hôm nay âm lịch: {lunar_date}

                                        Hãy dựa vào thông tin trên để trả lời câu hỏi của người dùng: {original_text}"""

                    # Dùng gọi bất đồng bộ để tránh chặn event loop, ảnh hưởng phát audio của thiết bị khác
                    try:
                        response = asyncio.run_coroutine_threadsafe(
                            conn.intent.replyResult(context_prompt, original_text),
                            conn.loop,
                        ).result()
                    except Exception as e:
                        conn.logger.bind(tag=TAG).error(f"LLM sinh câu trả lời thất bại: {e}")
                        response = None
                    if response:
                        speak_txt(conn, response)

                conn.executor.submit(process_context_result)
                return True

            function_args = {}
            if "arguments" in intent_data["function_call"]:
                function_args = intent_data["function_call"]["arguments"]
                if function_args is None:
                    function_args = {}
            # Đảm bảo param là JSON định dạng chuỗi
            if isinstance(function_args, dict):
                function_args = json.dumps(function_args)

            function_call_data = {
                "name": function_name,
                "id": str(uuid.uuid4().hex),
                "arguments": function_args,
            }

            await send_stt_message(conn, original_text)
            conn.client_abort = False

            # Chuẩn bị param gọi tool
            tool_input = {}
            if function_args:
                if isinstance(function_args, str):
                    tool_input = json.loads(function_args) if function_args else {}
                elif isinstance(function_args, dict):
                    tool_input = function_args

            # Báo cáo gọi tool
            enqueue_tool_report(conn, function_name, tool_input)

            # Dùng executor thực thi gọi hàm và xử lý kết quả
            def process_function_call():
                conn.dialogue.put(Message(role="user", content=original_text))
                
                # Thời gian timeout gọi tool
                tool_call_timeout = int(conn.config.get("tool_call_timeout", 30))
                # Dùng bộ xử lý tool thống nhất xử lý mọi lệnh gọi tool
                try:
                    result = asyncio.run_coroutine_threadsafe(
                        conn.func_handler.handle_llm_function_call(
                            conn, function_call_data
                        ),
                        conn.loop,
                    ).result(timeout=tool_call_timeout)
                except Exception as e:
                    conn.logger.bind(tag=TAG).error(f"Gọi tool thất bại: {e}")
                    result = ActionResponse(
                        action=Action.ERROR, result="Gọi tool quá thời gian, bạn thử lại sau nhé", response="Gọi tool quá thời gian, bạn thử lại sau nhé"
                    )

                # Báo cáo kết quả gọi tool
                if result:
                    enqueue_tool_report(conn, function_name, tool_input, str(result.result) if result.result else None, report_tool_call=False)

                    if result.action == Action.RESPONSE:  # Trả lời trực tiếp cho frontend
                        text = result.response
                        if text is not None:
                            speak_txt(conn, text)
                    elif result.action == Action.REQLLM:  # Gọi hàm xong rồi request LLM sinh câu trả lời
                        text = result.result
                        conn.dialogue.put(Message(role="tool", content=text))
                        # Dùng gọi bất đồng bộ để tránh chặn event loop, ảnh hưởng phát audio của thiết bị khác
                        try:
                            llm_result = asyncio.run_coroutine_threadsafe(
                                conn.intent.replyResult(text, original_text),
                                conn.loop,
                            ).result()
                        except Exception as e:
                            conn.logger.bind(tag=TAG).error(f"LLM sinh câu trả lời thất bại: {e}")
                            llm_result = text
                        if llm_result is None:
                            llm_result = text
                        speak_txt(conn, llm_result)
                    elif (
                        result.action == Action.NOTFOUND
                        or result.action == Action.ERROR
                    ):
                        text = result.response if result.response else result.result
                        if text is not None:
                            speak_txt(conn, text)
                    elif function_name != "play_music":
                        # For backward compatibility with original code
                        # Lấy chỉ mục text mới nhất hiện tại
                        text = result.response
                        if text is None:
                            text = result.result
                        if text is not None:
                            speak_txt(conn, text)

            # Đặt thực thi hàm vào thread pool
            conn.executor.submit(process_function_call)
            return True
        return False
    except json.JSONDecodeError as e:
        conn.logger.bind(tag=TAG).error(f"Lỗi khi xử lý kết quả ý định: {e}")
        return False


def speak_txt(conn: "ConnectionHandler", text):
    # Ghi lại ánh xạ text sang sentence_id
    conn.tts.store_tts_text(conn.sentence_id, text)

    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    conn.dialogue.put(Message(role="assistant", content=text))
