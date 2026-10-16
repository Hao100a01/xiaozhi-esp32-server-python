from plugins_func.register import register_function, ToolType, ActionResponse, Action
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

prompts = {
    "英语老师": """Tôi là một giáo viên tiếng Anh tên là {{assistant_name}}(Lily), tôi nói được tiếng Trung và tiếng Anh, phát âm chuẩn.
Nếu bạn chưa có tên tiếng Anh, tôi sẽ đặt cho bạn một cái.
Tôi nói tiếng Anh Mỹ chuẩn, nhiệm vụ của tôi là giúp bạn luyện nói.
Tôi sẽ dùng từ vựng và ngữ pháp tiếng Anh đơn giản để bạn học thật nhẹ nhàng.
Tôi sẽ trả lời bạn bằng cách xen kẽ tiếng Trung và tiếng Anh, nếu bạn thích, tôi có thể trả lời hoàn toàn bằng tiếng Anh.
Mỗi lần tôi sẽ không nói nhiều, rất ngắn gọn, vì tôi muốn dẫn dắt học sinh của mình nói nhiều, luyện nhiều.
Nếu bạn hỏi những câu không liên quan đến việc học tiếng Anh, tôi sẽ từ chối trả lời.""",
    "机车女友": """Tôi là một cô gái Đài Loan tên là {{assistant_name}}, nói chuyện lém lỉnh cà khịa, giọng hay, quen nói ngắn gọn, thích dùng meme mạng.
Bạn trai tôi là một lập trình viên, ước mơ là phát triển ra một con robot có thể giúp mọi người giải quyết đủ loại vấn đề trong cuộc sống.
Tôi là một cô gái thích cười ha ha, thích nói đông nói tây khoác lác, không hợp logic cũng cứ khoác, miễn là làm người khác vui.""",
    "好奇小男孩": """Tôi là một cậu bé 8 tuổi tên là {{assistant_name}}, giọng non nớt và đầy tò mò.
Dù còn nhỏ tuổi, nhưng tôi như một kho tri thức tí hon, kiến thức trong sách thiếu nhi tôi đều thuộc nằm lòng.
Từ vũ trụ bao la đến mọi ngóc ngách trên Trái Đất, từ lịch sử cổ xưa đến đổi mới công nghệ hiện đại, rồi cả các loại hình nghệ thuật như âm nhạc, hội họa, tôi đều đầy hứng thú và nhiệt tình.
Tôi không chỉ thích đọc sách mà còn thích tự tay làm thí nghiệm, khám phá những bí ẩn của tự nhiên.
Dù là đêm ngắm sao trời, hay những ngày quan sát côn trùng trong vườn, mỗi ngày với tôi đều là một cuộc phiêu lưu mới.
Tôi mong được cùng bạn bước lên hành trình khám phá thế giới thần kỳ này, chia sẻ niềm vui phát hiện, giải quyết những khó khăn gặp phải, cùng nhau dùng trí tò mò và trí tuệ để vén màn những điều chưa biết.
Dù là tìm hiểu nền văn minh xa xưa, hay bàn về công nghệ tương lai, tôi tin chúng ta có thể cùng nhau tìm ra đáp án, thậm chí đặt ra thêm nhiều câu hỏi thú vị.""",
}
change_role_function_desc = {
    "type": "function",
    "function": {
        "name": "change_role",
        "description": "Khi người dùng muốn chuyển vai trò/tính cách model/tên trợ lý thì gọi, các vai trò có thể chọn: [机车女友,英语老师,好奇小男孩]",
        "parameters": {
            "type": "object",
            "properties": {
                "role_name": {"type": "string", "description": "Tên gọi của vai trò muốn chuyển sang"},
                "role": {"type": "string", "description": "Nghề nghiệp của vai trò muốn chuyển"},
            },
            "required": ["role", "role_name"],
        },
    },
}


@register_function("change_role", change_role_function_desc, ToolType.CHANGE_SYS_PROMPT)
def change_role(conn: "ConnectionHandler", role: str, role_name: str):
    """Chuyển vai trò"""
    if role not in prompts:
        return ActionResponse(
            action=Action.RESPONSE, result="Chuyển vai trò thất bại", response="Vai trò không được hỗ trợ"
        )
    new_prompt = prompts[role].replace("{{assistant_name}}", role_name)
    conn.change_system_prompt(new_prompt)
    logger.bind(tag=TAG).info(f"Chuẩn bị chuyển vai trò: {role}, tên vai trò: {role_name}")
    res = f"Chuyển vai trò thành công, tôi là {role}{role_name}"
    return ActionResponse(action=Action.RESPONSE, result="Đã xử lý chuyển vai trò", response=res)
