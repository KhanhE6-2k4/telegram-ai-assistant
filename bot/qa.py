"""Trả lời lệnh /ask, bằng LLM (provider bất kỳ) với hai công cụ:

- list_tasks: danh sách task (lấy từ Google Sheet, kèm ngày giao lưu trong SQLite)
- get_day: bản tóm tắt + tin nhắn chưa nén của một ngày

Model tự quyết định gọi công cụ nào, nên câu hỏi không cần ghi rõ tên task hay ngày.
"""
import asyncio
import json
import logging
from datetime import date, timedelta

from .dates import DEADLINE_RULES, WEEKDAYS, calendar_text
from .providers import LLMProvider, Tool, ToolCall, run_tool_loop
from .sheets import FIELDS, TaskSheet
from .storage import Storage

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """Bạn là bot trợ lý của nhóm chat Telegram "{title}". Hôm nay là {today} ({weekday}).
Thành viên dùng lệnh /ask để hỏi bạn về task và nội dung trao đổi trong nhóm.

- Dùng công cụ để tra cứu, không đoán. Thông tin task (nội dung, tiến độ, deadline) lấy từ
  list_tasks vì đó là dữ liệu trên Google Sheet, nguồn chính thức.
- Sheet chỉ được cập nhật theo đợt, nên các tin nhắn gần đây có thể chứa task hoặc thay đổi chưa
  lên sheet. Với câu hỏi về task, hãy gọi CẢ list_tasks VÀ get_day cho hôm nay (và ngày liên quan
  nếu câu hỏi nhắc tới), rồi kết hợp: phần recent_messages của get_day là thông tin mới nhất.
  Nếu thông tin chỉ có trong tin nhắn (chưa có trên sheet), vẫn trả lời và nói rõ là theo tin nhắn
  trong nhóm, chưa ghi lên sheet.
- Nếu câu hỏi không ghi rõ tên task hoặc ngày, hãy cố tìm task khớp nhất (theo từ khoá, người nhận,
  thời gian). Nếu có nhiều task khớp, liệt kê ngắn gọn để người hỏi chọn.
- Ngày tương đối hãy quy đổi theo ngày hôm nay (ngày mốc), dùng bảng lịch bên dưới; "hôm qua" =
  ngày mốc - 1.
{deadline_rules}
- Trả lời bằng tiếng Việt, ngắn gọn, dạng văn bản thường (không dùng Markdown).
{edit_rule}

Lịch:
{calendar}"""

EDIT_ALLOWED = """- Người hỏi CÓ quyền sửa task. Khi họ yêu cầu đổi hạn, người thực hiện, nội dung, tên hoặc tiến độ, hãy tra
  list_tasks để xác định đúng STT rồi gọi propose_task_edit. Thay đổi chỉ được ghi sau khi người hỏi
  bấm xác nhận, nên hãy nói rõ là đang chờ xác nhận. Deadline ghi dạng YYYY-MM-DD."""

EDIT_DENIED = """- Người hỏi KHÔNG có quyền sửa task. Nếu họ yêu cầu sửa, hãy từ chối lịch sự và nói rằng
  chỉ admin nhóm hoặc người được cấp quyền mới sửa được."""

EDIT_TOOL = Tool(
    name="propose_task_edit",
    description="Đề xuất sửa một task trên Google Sheet. Người hỏi sẽ được yêu cầu bấm xác nhận.",
    parameters={
        "type": "object",
        "properties": {
            "stt": {"type": "integer", "description": "STT của task trên sheet."},
            "changes": {
                "type": "object",
                "description": "Chỉ các trường cần đổi.",
                "properties": {f: {"type": "string"} for f in FIELDS},
            },
        },
        "required": ["stt", "changes"],
    },
)


TOOLS = [
    Tool(
        name="list_tasks",
        description="Liệt kê các task của nhóm (STT, ngày giao, tên, người thực hiện, nội dung, tiến độ, deadline). "
        "Có thể lọc theo khoảng ngày giao.",
        parameters={
            "type": "object",
            "properties": {
                "from_day": {"type": "string", "description": "YYYY-MM-DD, mặc định 30 ngày trước."},
                "to_day": {"type": "string", "description": "YYYY-MM-DD, mặc định hôm nay."},
            },
        },
    ),
    Tool(
        name="get_day",
        description="Lấy bản tóm tắt hội thoại và các tin nhắn gần nhất chưa tóm tắt của một ngày.",
        parameters={
            "type": "object",
            "properties": {"day": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["day"],
        },
    ),
]


class QA:
    def __init__(self, storage: Storage, llm: LLMProvider, sheets):
        self.storage = storage
        self.llm = llm
        self.sheets = sheets

    # ---- công cụ ----

    def _sheet(self, chat_id: int) -> TaskSheet:
        return TaskSheet(self.sheets.worksheet_for(chat_id), self.storage, chat_id)

    def _list_tasks(self, chat_id: int, today: str, from_day: str | None, to_day: str | None) -> list[dict]:
        """Task trên sheet có ngày giao hoặc ngày cập nhật nằm trong khoảng (task không rõ ngày luôn được lấy)."""
        from_day = from_day or (date.fromisoformat(today) - timedelta(days=30)).isoformat()
        to_day = to_day or today
        tasks = []
        for t in self._sheet(chat_id).rows():
            created, updated = t.pop("created_day"), t.pop("updated_day")
            if created and not (from_day <= created <= to_day or from_day <= updated <= to_day):
                continue
            tasks.append({"stt": t.pop("stt"), "day": created, **t})
        return tasks

    def _get_day(self, chat_id: int, day: str) -> dict:
        s = self.storage.get_summary(chat_id, day)
        recent = self.storage.pending_messages(chat_id, day, s.compacted_until)
        return {
            "day": day,
            "summary": s.summary,
            "recent_messages": [m.format_line() for m in recent[-200:]],
        }

    def _propose_edit(self, chat_id: int, args: dict, proposals: list[dict]) -> dict:
        stt = args["stt"]
        changes = {k: v for k, v in args.get("changes", {}).items() if k in FIELDS}
        current = self._sheet(chat_id).get(stt)
        if current is None:
            return {"error": f"Không có task STT {stt}"}
        if not changes:
            return {"error": "Không có trường nào để đổi"}
        proposals.append({"stt": stt, "changes": changes, "current": current})
        return {"status": "Đã gửi yêu cầu xác nhận tới người hỏi", "current": current}

    async def _run_tool(self, name: str, args: dict, chat_id: int, today: str, proposals: list[dict]):
        if name == "list_tasks":
            return await asyncio.to_thread(
                self._list_tasks, chat_id, today, args.get("from_day"), args.get("to_day")
            )
        if name == "get_day":
            return self._get_day(chat_id, args["day"])
        if name == "propose_task_edit" and proposals is not None:
            return await asyncio.to_thread(self._propose_edit, chat_id, args, proposals)
        return {"error": f"Không có công cụ {name}"}

    # ---- vòng hỏi đáp ----

    async def answer(
        self, chat_id: int, chat_title: str, today: str, asker: str, question: str, can_edit: bool = False
    ) -> tuple[str, list[dict]]:
        """Trả về (câu trả lời, danh sách đề xuất sửa task đang chờ xác nhận)."""
        proposals: list[dict] = []
        tools = TOOLS + [EDIT_TOOL] if can_edit else TOOLS
        system = SYSTEM_PROMPT.format(
            title=chat_title,
            today=today,
            weekday=WEEKDAYS[date.fromisoformat(today).weekday()],
            deadline_rules=DEADLINE_RULES,
            edit_rule=EDIT_ALLOWED if can_edit else EDIT_DENIED,
            calendar=calendar_text(today),
        )

        async def execute(call: ToolCall) -> str:
            try:
                out = await self._run_tool(call.name, call.arguments, chat_id, today, proposals if can_edit else None)
            except Exception as e:
                log.exception("Công cụ %s lỗi", call.name)
                out = {"error": str(e)}
            return json.dumps(out, ensure_ascii=False)

        text = await run_tool_loop(
            self.llm,
            system=system,
            prompt=f"{asker} hỏi: {question}",
            tools=tools,
            execute=execute,
            max_rounds=MAX_TOOL_ROUNDS,
            max_tokens=2000,
        )
        if text is None:
            return "Xin lỗi, mình chưa tìm được câu trả lời. Bạn thử hỏi cụ thể hơn nhé.", proposals
        return text, proposals
