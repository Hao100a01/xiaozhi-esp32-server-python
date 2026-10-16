import httpx
from config.logger import setup_logging
from plugins_func.functions.hass_init import initialize_hass_handler
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

hass_get_state_function_desc = {
    "type": "function",
    "function": {
        "name": "hass_get_state",
        "description": "Lấy trạng thái thiết bị trong Home Assistant, bao gồm tra độ sáng, màu sắc, nhiệt độ màu của đèn; âm lượng của trình phát media; các thao tác tạm dừng, tiếp tục của thiết bị",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "ID thiết bị cần điều khiển, entity_id trong Home Assistant",
                }
            },
            "required": ["entity_id"],
        },
    },
}

hass_set_state_function_desc = {
    "type": "function",
    "function": {
        "name": "hass_set_state",
        "description": "Đặt trạng thái thiết bị trong Home Assistant, bao gồm bật, tắt; điều chỉnh độ sáng, màu sắc, nhiệt độ màu của đèn; điều chỉnh âm lượng của trình phát; các thao tác tạm dừng, tiếp tục, tắt tiếng của thiết bị",
        "parameters": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Hành động cần thực hiện: bật thiết bị: turn_on, tắt thiết bị: turn_off, tăng độ sáng: brightness_up, giảm độ sáng: brightness_down, đặt độ sáng: brightness_value, tăng âm lượng: volume_up, giảm âm lượng: volume_down, đặt âm lượng: volume_set, đặt nhiệt độ màu: set_kelvin, đặt màu sắc: set_color, tạm dừng thiết bị: pause, tiếp tục thiết bị: continue, tắt tiếng/bỏ tắt tiếng: volume_mute",
                        },
                        "input": {
                            "type": "integer",
                            "description": "Chỉ cần khi đặt âm lượng, đặt độ sáng; giá trị hợp lệ 1-100, tương ứng 1%-100% âm lượng và độ sáng",
                        },
                        "is_muted": {
                            "type": "string",
                            "description": "Chỉ cần khi thực hiện thao tác tắt tiếng; khi đặt tắt tiếng giá trị này là true, khi bỏ tắt tiếng là false",
                        },
                        "rgb_color": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Chỉ cần khi đặt màu sắc; điền giá trị rgb của màu mục tiêu vào đây",
                        },
                    },
                    "required": ["type"],
                },
                "entity_id": {
                    "type": "string",
                    "description": "ID thiết bị cần điều khiển, entity_id trong Home Assistant",
                },
            },
            "required": ["state", "entity_id"],
        },
    },
}


@register_function("hass_get_state", hass_get_state_function_desc, ToolType.SYSTEM_CTL)
async def hass_get_state(conn: "ConnectionHandler", entity_id=""):
    try:
        ha_response = await handle_hass_get_state(conn, entity_id)
        return ActionResponse(Action.REQLLM, ha_response, None)
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("Lấy trạng thái Home Assistant bị timeout")
        return ActionResponse(Action.ERROR, "Yêu cầu timeout", None)
    except Exception as e:
        error_msg = f"Thực hiện thao tác Home Assistant thất bại"
        logger.bind(tag=TAG).error(error_msg)
        return ActionResponse(Action.ERROR, error_msg, None)


@register_function("hass_set_state", hass_set_state_function_desc, ToolType.SYSTEM_CTL)
async def hass_set_state(conn: "ConnectionHandler", entity_id="", state=None):
    if state is None:
        state = {}
    try:
        ha_response = await handle_hass_set_state(conn, entity_id, state)
        return ActionResponse(Action.REQLLM, ha_response, None)
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("Đặt trạng thái Home Assistant bị timeout")
        return ActionResponse(Action.ERROR, "Yêu cầu timeout", None)
    except Exception as e:
        error_msg = f"Thực hiện thao tác Home Assistant thất bại"
        logger.bind(tag=TAG).error(error_msg)
        return ActionResponse(Action.ERROR, error_msg, None)


async def handle_hass_get_state(conn: "ConnectionHandler", entity_id):
    ha_config = initialize_hass_handler(conn)
    api_key = ha_config.get("api_key")
    base_url = ha_config.get("base_url")
    url = f"{base_url}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
        response = await client.get(url, headers=headers)

    if response.status_code == 200:
        responsetext = "Trạng thái thiết bị:" + response.json()["state"] + " "
        logger.bind(tag=TAG).info(f"Nội dung API trả về: {response.json()}")

        if "media_title" in response.json()["attributes"]:
            responsetext = (
                responsetext
                + "Đang phát:"
                + str(response.json()["attributes"]["media_title"])
                + " "
            )
        if "volume_level" in response.json()["attributes"]:
            responsetext = (
                responsetext
                + "Âm lượng:"
                + str(response.json()["attributes"]["volume_level"])
                + " "
            )
        if "color_temp_kelvin" in response.json()["attributes"]:
            responsetext = (
                responsetext
                + "Nhiệt độ màu:"
                + str(response.json()["attributes"]["color_temp_kelvin"])
                + " "
            )
        if "rgb_color" in response.json()["attributes"]:
            responsetext = (
                responsetext
                + "Màu rgb:"
                + str(response.json()["attributes"]["rgb_color"])
                + " "
            )
        if "brightness" in response.json()["attributes"]:
            responsetext = (
                responsetext
                + "Độ sáng:"
                + str(response.json()["attributes"]["brightness"])
                + " "
            )
        logger.bind(tag=TAG).info(f"Nội dung tra cứu trả về: {responsetext}")
        return responsetext
    else:
        return f"Chuyển đổi thất bại, mã lỗi: {response.status_code}"


async def handle_hass_set_state(conn: "ConnectionHandler", entity_id, state):
    ha_config = initialize_hass_handler(conn)
    api_key = ha_config.get("api_key")
    base_url = ha_config.get("base_url")
    """
    state = { "type":"brightness_up","input":"80","is_muted":"true"}
    """
    domains = entity_id.split(".")
    if len(domains) > 1:
        domain = domains[0]
    else:
        return "Thực hiện thất bại, ID thiết bị không đúng"
    action = ""
    arg = ""
    value = ""
    if state["type"] == "turn_on":
        description = "Thiết bị đã bật"
        if domain == "cover":
            action = "open_cover"
        elif domain == "vacuum":
            action = "start"
        else:
            action = "turn_on"
    elif state["type"] == "turn_off":
        description = "Thiết bị đã tắt"
        if domain == "cover":
            action = "close_cover"
        elif domain == "vacuum":
            action = "stop"
        else:
            action = "turn_off"
    elif state["type"] == "brightness_up":
        description = "Đèn đã sáng hơn"
        action = "turn_on"
        arg = "brightness_step_pct"
        value = 10
    elif state["type"] == "brightness_down":
        description = "Đèn đã tối hơn"
        action = "turn_on"
        arg = "brightness_step_pct"
        value = -10
    elif state["type"] == "brightness_value":
        description = f"Độ sáng đã chỉnh thành {state['input']}"
        action = "turn_on"
        arg = "brightness_pct"
        value = state["input"]
    elif state["type"] == "set_color":
        description = f"Màu sắc đã chỉnh thành {state['rgb_color']}"
        action = "turn_on"
        arg = "rgb_color"
        value = state["rgb_color"]
    elif state["type"] == "set_kelvin":
        description = f"Nhiệt độ màu đã chỉnh thành {state['input']}K"
        action = "turn_on"
        arg = "kelvin"
        value = state["input"]
    elif state["type"] == "volume_up":
        description = "Âm lượng đã tăng"
        action = state["type"]
    elif state["type"] == "volume_down":
        description = "Âm lượng đã giảm"
        action = state["type"]
    elif state["type"] == "volume_set":
        description = f"Âm lượng đã chỉnh thành {state['input']}"
        action = state["type"]
        arg = "volume_level"
        value = state["input"]
        if state["input"] >= 1:
            value = state["input"] / 100
    elif state["type"] == "volume_mute":
        description = f"Thiết bị đã tắt tiếng"
        action = state["type"]
        arg = "is_volume_muted"
        value = state["is_muted"]
    elif state["type"] == "pause":
        description = f"Thiết bị đã tạm dừng"
        action = state["type"]
        if domain == "media_player":
            action = "media_pause"
        if domain == "cover":
            action = "stop_cover"
        if domain == "vacuum":
            action = "pause"
    elif state["type"] == "continue":
        description = f"Thiết bị đã tiếp tục"
        if domain == "media_player":
            action = "media_play"
        if domain == "vacuum":
            action = "start"
    else:
        return f"Chức năng {state['type']} của {domain} chưa được hỗ trợ"

    if arg == "":
        data = {
            "entity_id": entity_id,
        }
    else:
        data = {"entity_id": entity_id, arg: value}
    url = f"{base_url}/api/services/{domain}/{action}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
        response = await client.post(url, headers=headers, json=data)

    logger.bind(tag=TAG).info(
        f"Đặt trạng thái: {description}, url: {url}, return_code: {response.status_code}"
    )
    if response.status_code == 200:
        return description
    else:
        return f"Đặt thất bại, mã lỗi: {response.status_code}"
