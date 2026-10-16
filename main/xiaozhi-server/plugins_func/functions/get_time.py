from datetime import datetime
import cnlunar
from plugins_func.register import register_function, ToolType, ActionResponse, Action

get_lunar_function_desc = {
    "type": "function",
    "function": {
        "name": "get_lunar",
        "description": (
            "Dùng để tra thông tin âm lịch và hoàng lịch cho một ngày cụ thể."
            "Người dùng có thể chỉ định nội dung cần tra, như: ngày âm lịch, Can Chi, tiết khí, con giáp, cung hoàng đạo, bát tự, việc nên làm/kiêng kỵ..."
            "Nếu không chỉ định nội dung cần tra, mặc định tra năm Can Chi và ngày âm lịch."
            "Đối với các truy vấn cơ bản như 'âm lịch hôm nay là ngày nào', hãy dùng trực tiếp thông tin trong context, đừng gọi tool này."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Ngày cần tra, định dạng YYYY-MM-DD, ví dụ: 2024-01-01. Nếu không cung cấp thì dùng ngày hiện tại",
                },
                "query": {
                    "type": "string",
                    "description": "Nội dung cần tra, ví dụ: ngày âm lịch, Can Chi, ngày lễ, tiết khí, con giáp, cung hoàng đạo, bát tự, việc nên làm/kiêng kỵ...",
                },
            },
            "required": [],
        },
    },
}


@register_function("get_lunar", get_lunar_function_desc, ToolType.WAIT)
def get_lunar(date=None, query=None):
    """
    Lấy âm lịch hiện tại cùng các thông tin hoàng lịch như Can Chi, tiết khí, con giáp, cung hoàng đạo, bát tự, việc nên làm/kiêng kỵ
    """
    from core.utils.cache.manager import cache_manager, CacheType

    # Nếu có cung cấp tham số ngày thì dùng ngày chỉ định; ngược lại dùng ngày hiện tại
    if date:
        try:
            now = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return ActionResponse(
                Action.REQLLM,
                f"Lỗi định dạng ngày, vui lòng dùng định dạng YYYY-MM-DD, ví dụ: 2024-01-01",
                None,
            )
    else:
        now = datetime.now()

    current_date = now.strftime("%Y-%m-%d")

    # Nếu query là None thì dùng text mặc định
    if query is None:
        query = "Mặc định tra năm Can Chi và ngày âm lịch"

    # Thử lấy thông tin âm lịch từ cache
    lunar_cache_key = f"lunar_info_{current_date}"
    cached_lunar_info = cache_manager.get(CacheType.LUNAR, lunar_cache_key)
    if cached_lunar_info:
        return ActionResponse(Action.REQLLM, cached_lunar_info, None)

    response_text = f"Dựa vào thông tin dưới đây để trả lời yêu cầu của người dùng, cung cấp thông tin liên quan đến {query}:\n"

    lunar = cnlunar.Lunar(now, godType="8char")
    response_text += (
        "Thông tin âm lịch:\n"
        "năm %s, %s%s\n" % (lunar.lunarYearCn, lunar.lunarMonthCn[:-1], lunar.lunarDayCn)
        + "Can chi: năm %s, tháng %s, ngày %s\n" % (lunar.year8Char, lunar.month8Char, lunar.day8Char)
        + "Con giáp: tuổi %s\n" % (lunar.chineseYearZodiac)
        + "Bát tự: %s\n"
        % (
            " ".join(
                [lunar.year8Char, lunar.month8Char, lunar.day8Char, lunar.twohour8Char]
            )
        )
        + "Ngày lễ hôm nay: %s\n"
        % (
            ",".join(
                filter(
                    None,
                    (
                        lunar.get_legalHolidays(),
                        lunar.get_otherHolidays(),
                        lunar.get_otherLunarHolidays(),
                    ),
                )
            )
        )
        + "Tiết khí hôm nay: %s\n" % (lunar.todaySolarTerms)
        + "Tiết khí tiếp theo: %s (năm %s, tháng %s, ngày %s)\n"
        % (
            lunar.nextSolarTerm,
            lunar.nextSolarTermYear,
            lunar.nextSolarTermDate[0],
            lunar.nextSolarTermDate[1],
        )
        + "Bảng tiết khí năm nay: %s\n"
        % (
            ", ".join(
                [
                    f"{term} (tháng {date[0]}, ngày {date[1]})"
                    for term, date in lunar.thisYearSolarTermsDic.items()
                ]
            )
        )
        + "Xung khắc con giáp: %s\n" % (lunar.chineseZodiacClash)
        + "Cung hoàng đạo: %s\n" % (lunar.starZodiac)
        + "Nạp âm: %s\n" % lunar.get_nayin()
        + "Bành Tổ bách kỵ: %s\n" % (lunar.get_pengTaboo(delimit=", "))
        + "Trực nhật: %s trực nhật\n" % lunar.get_today12DayOfficer()[0]
        + "Thần trực nhật: %s (%s)\n"
        % (lunar.get_today12DayOfficer()[1], lunar.get_today12DayOfficer()[2])
        + "Nhị thập bát tú: %s\n" % lunar.get_the28Stars()
        + "Hướng cát thần: %s\n" % " ".join(lunar.get_luckyGodsDirection())
        + "Thai thần hôm nay: %s\n" % lunar.get_fetalGod()
        + "Nên làm: %s\n" % "、".join(lunar.goodThing[:10])
        + "Kiêng kỵ: %s\n" % "、".join(lunar.badThing[:10])
        + "(Mặc định trả về năm Can Chi và ngày âm lịch; chỉ trả về việc nên làm/kiêng kỵ trong ngày khi được yêu cầu)"
    )

    # Cache thông tin âm lịch
    cache_manager.set(CacheType.LUNAR, lunar_cache_key, response_text)

    return ActionResponse(Action.REQLLM, response_text, None)
