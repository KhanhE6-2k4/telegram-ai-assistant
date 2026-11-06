"""Trả lời câu hỏi (tag @bot trong nhóm, lệnh riêng của chủ bot), bằng LLM (provider bất kỳ) với các công cụ:

- list_tasks: danh sách task (lấy từ Google Sheet, kèm ngày giao lưu trong SQLite)
- get_day: bản tóm tắt + tin nhắn chưa xử lý của một ngày (xem chi tiết một ngày)
- get_period: bản tóm tắt của mọi ngày có tin trong một khoảng ngày (câu hỏi về tuần / tháng), một lần gọi
- propose_task_edit: đề xuất sửa task (chỉ người có quyền)

Kết quả list_tasks mặc định (30 ngày), get_day của hôm nay và phạm vi dữ liệu tin nhắn được nạp sẵn,
gửi kèm câu hỏi, nên đa số câu hỏi chỉ cần một lượt gọi LLM. Model chỉ gọi công cụ khi cần dữ liệu khác
(ngày khác, khoảng khác) hoặc để đề xuất sửa task; câu hỏi không cần ghi rõ tên task hay ngày.
"""
import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import date, timedelta

from .dates import DEADLINE_RULES, PAST_RULES, calendar_text, check_day, day_label
from .providers import ChatMessage, LLMProvider, Tool, ToolCall, run_tool_loop
from .sheets import FIELDS, TaskSheet
from .storage import Storage
from .timing import timed

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
# get_period đọc tối đa bấy nhiêu ngày một lần (khoảng 3 tháng), để kết quả không quá dài
MAX_PERIOD_DAYS = 91

SYSTEM_PROMPT = """Bạn là bot trợ lý của cuộc trò chuyện Telegram "{title}" (nhóm chat hoặc chat riêng).
Hôm nay là {today_label} ({today}). Người dùng hỏi bạn về task và nội dung trao đổi trong cuộc trò chuyện này.
Đầu mỗi câu hỏi có [thứ, ngày/tháng/năm]: ngày người hỏi gửi câu đó, là ngày mốc để quy đổi ngày tương đối
trong câu đó.

- Tra cứu dữ liệu, không đoán. Thông tin task (nội dung, tiến độ, deadline) lấy từ list_tasks vì đó
  là dữ liệu trên Google Sheet, nguồn chính thức.
- Ngay sau câu hỏi có sẵn "Dữ liệu có sẵn": phạm vi dữ liệu tin nhắn (coverage), kết quả list_tasks mặc định
  (task giao hoặc cập nhật trong 30 ngày gần nhất) và get_day của hôm nay. Dùng luôn dữ liệu này, KHÔNG
  gọi lại công cụ cho đúng phần đó. Chỉ gọi công cụ khi cần dữ liệu khác, hoặc khi phần có sẵn báo lỗi.
- Câu hỏi về một khoảng thời gian (tuần, tháng, nhiều ngày): gọi get_period MỘT lần cho cả khoảng, không
  gọi get_day cho từng ngày. Chỉ gọi get_day khi cần chi tiết của một ngày cụ thể (tin nhắn nguyên văn
  chưa xử lý), ví dụ ngày có unprocessed > 0 trong kết quả get_period. Không gọi công cụ cho ngày trước
  coverage.data_from.
- Sheet chỉ được cập nhật theo đợt, nên các tin nhắn gần đây có thể chứa task hoặc thay đổi chưa
  lên sheet. Với câu hỏi về task, hãy kết hợp danh sách task với tin nhắn: phần recent_messages của
  get_day là thông tin mới nhất. Nếu thông tin chỉ có trong tin nhắn (chưa có trên sheet), vẫn trả
  lời và nói rõ là theo tin nhắn trong cuộc trò chuyện, chưa ghi lên sheet.
- Nếu câu hỏi không ghi rõ tên task hoặc ngày, hãy cố tìm task khớp nhất (theo từ khoá, người nhận,
  thời gian). Nếu có nhiều task khớp, liệt kê ngắn gọn để người hỏi chọn.
- Ngày tương đối hãy quy đổi theo ngày mốc, tra bảng lịch bên dưới, không tự tính thứ.
{past_rules}
{deadline_rules}
- Phạm vi dữ liệu (coverage): bot có đủ tin nhắn từ ngày data_from (sớm hơn ngày bắt đầu theo dõi
  tracked_since, vì lúc bắt đầu theo dõi bot lấy thêm vài ngày lịch sử). Từ data_from trở đi, ngày không có
  tin là ngày không ai nhắn; trước data_from bot không biết. Nếu khoảng thời gian được hỏi bắt đầu trước
  data_from, hãy nói rõ là chỉ có dữ liệu từ ngày nào.
- Trả lời bằng tiếng Việt, ngắn gọn, dạng văn bản thường (không dùng Markdown).
{edit_rule}

Lịch:
{calendar}"""

EDIT_ALLOWED = """- Người hỏi CÓ quyền sửa task. Khi họ yêu cầu đổi hạn, người thực hiện, nội dung, tên hoặc tiến độ, hãy dùng
  danh sách task có sẵn (hoặc gọi list_tasks nếu task không có trong đó) để xác định đúng STT rồi gọi
  propose_task_edit. Thay đổi chỉ được ghi sau khi người hỏi
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
        description="Liệt kê các task của cuộc trò chuyện (STT, ngày giao, tên, người thực hiện, nội dung, tiến độ, deadline). "
        "Có thể lọc theo khoảng ngày giao.",
        parameters={
            "type": "object",
            "properties": {
                "from_day": {"type": "string", "description": "Ngày dạng YYYY-MM-DD, mặc định 30 ngày trước."},
                "to_day": {"type": "string", "description": "Ngày dạng YYYY-MM-DD, mặc định hôm nay."},
            },
        },
    ),
    Tool(
        name="get_day",
        description="Lấy bản tóm tắt hội thoại và các tin nhắn gần nhất chưa tóm tắt của một ngày.",
        parameters={
            "type": "object",
            "properties": {"day": {"type": "string", "description": "Ngày dạng YYYY-MM-DD (vd. 2026-10-27)"}},
            "required": ["day"],
        },
    ),
    Tool(
        name="get_period",
        description="Lấy bản tóm tắt hội thoại của mọi ngày CÓ tin nhắn trong một khoảng ngày (tối đa "
        "{max_days} ngày), kèm số tin và số tin chưa xử lý (chưa có trong bản tóm tắt) của từng ngày, và "
        "phạm vi dữ liệu của cuộc trò chuyện. Dùng cho câu hỏi về tuần, tháng hoặc nhiều ngày.".format(
            max_days=MAX_PERIOD_DAYS
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_day": {"type": "string", "description": "Ngày đầu, dạng YYYY-MM-DD."},
                "to_day": {"type": "string", "description": "Ngày cuối (tính cả ngày này), dạng YYYY-MM-DD."},
            },
            "required": ["from_day", "to_day"],
        },
    ),
]


def question_line(day: str, asker: str, question: str) -> str:
    """Câu hỏi gửi model, có ngày hỏi ở đầu: "[Thứ 6, 06/11/2026] An hỏi: ..." (ngày mốc của câu đó)."""
    return f"[{day_label(day)}] {asker} hỏi: {question}"


class QA:
    def __init__(self, storage: Storage, llm: LLMProvider, sheets):
        self.storage = storage
        self.llm = llm
        self.sheets = sheets

    # ---- công cụ ----

    def _sheet(self, chat_id: int, memo: dict | None = None) -> TaskSheet:
        """Tab task của chat. `memo`: trong một lần trả lời chỉ đọc Google Sheet một lần (phần nạp sẵn,
        list_tasks, propose_task_edit dùng chung); sheet không bị bot ghi trong lúc trả lời."""
        if memo is not None and chat_id in memo:
            return memo[chat_id]
        sheet = TaskSheet(self.sheets.worksheet_for(chat_id), self.storage, chat_id)
        if memo is not None:
            memo[chat_id] = sheet
        return sheet

    def _list_tasks(
        self, chat_id: int, today: str, from_day: str | None, to_day: str | None, memo: dict | None = None
    ) -> list[dict]:
        """Task trên sheet có ngày giao hoặc ngày cập nhật nằm trong khoảng (task không rõ ngày luôn được lấy)."""
        from_day = from_day or (date.fromisoformat(today) - timedelta(days=30)).isoformat()
        to_day = to_day or today
        tasks = []
        for t in self._sheet(chat_id, memo).rows():
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

    def _coverage(self, chat_id: int) -> dict:
        """Phạm vi dữ liệu tin nhắn của chat: ngày bắt đầu theo dõi, và ngày bot có đủ tin nhắn từ đó (lần đầu
        theo dõi, userbot đồng bộ thêm USERBOT_HISTORY_DAYS ngày lịch sử nên mốc này sớm hơn ngày bắt đầu theo
        dõi). Trong phạm vi, ngày không có tin là ngày không ai nhắn; trước phạm vi, bot không biết."""
        tracked_since, data_from = self.storage.data_coverage(chat_id)
        if data_from is None:
            return {"tracked_since": tracked_since, "data_from": None, "note": "Chưa có tin nhắn nào được lưu"}
        return {
            "tracked_since": tracked_since,
            "data_from": data_from,
            "note": "Từ ngày data_from trở đi, ngày không có tin là ngày không ai nhắn; "
            "trước data_from bot không có dữ liệu.",
        }

    def _get_period(self, chat_id: int, from_day: str, to_day: str) -> dict:
        """Bản tóm tắt của mọi ngày có tin trong khoảng [from_day, to_day] (ngày không có tin bị bỏ qua), kèm
        số tin / số tin chưa xử lý của từng ngày và phạm vi dữ liệu. Chỉ tóm tắt, không có tin nguyên văn,
        để câu hỏi về cả tháng chỉ cần một lần gọi và kết quả không quá dài."""
        if from_day > to_day:
            raise ValueError(f"from_day ({from_day}) phải trước hoặc bằng to_day ({to_day})")
        span = (date.fromisoformat(to_day) - date.fromisoformat(from_day)).days + 1
        if span > MAX_PERIOD_DAYS:
            raise ValueError(
                f"Khoảng {span} ngày quá dài, tối đa {MAX_PERIOD_DAYS} ngày một lần: hãy chia nhỏ khoảng ngày"
            )
        return {
            "from_day": from_day,
            "to_day": to_day,
            "coverage": self._coverage(chat_id),
            "days": self.storage.days_overview(chat_id, from_day, to_day),
        }

    def _propose_edit(self, chat_id: int, args: dict, proposals: list[dict], memo: dict | None = None) -> dict:
        stt = args["stt"]
        changes = {k: v for k, v in args.get("changes", {}).items() if k in FIELDS}
        current = self._sheet(chat_id, memo).get(stt)
        if current is None:
            return {"error": f"Không có task STT {stt}"}
        if not changes:
            return {"error": "Không có trường nào để đổi"}
        proposals.append({"stt": stt, "changes": changes, "current": current})
        return {"status": "Đã gửi yêu cầu xác nhận tới người hỏi", "current": current}

    async def _run_tool(
        self, name: str, args: dict, chat_id: int, today: str, proposals: list[dict], memo: dict | None = None
    ):
        if name == "list_tasks":
            # Ngày sai dạng / không có thật: báo lỗi cho model sửa, không âm thầm trả kết quả sai
            from_day = check_day(args["from_day"], "from_day") if args.get("from_day") else None
            to_day = check_day(args["to_day"], "to_day") if args.get("to_day") else None
            return await asyncio.to_thread(self._list_tasks, chat_id, today, from_day, to_day, memo)
        if name == "get_day":
            return self._get_day(chat_id, check_day(args.get("day"), "day"))
        if name == "get_period":
            return self._get_period(
                chat_id, check_day(args.get("from_day"), "from_day"), check_day(args.get("to_day"), "to_day")
            )
        if name == "propose_task_edit" and proposals is not None:
            return await asyncio.to_thread(self._propose_edit, chat_id, args, proposals, memo)
        return {"error": f"Không có công cụ {name}"}

    async def _preload(self, chat_id: int, today: str, memo: dict) -> str:
        """Phần "Dữ liệu có sẵn" gửi kèm câu hỏi: phạm vi dữ liệu tin nhắn, kết quả list_tasks mặc định và
        get_day của hôm nay. Model hầu như luôn cần những thứ này; gửi sẵn thì không tốn một lượt gọi LLM chỉ để
        model xin dữ liệu. Phạm vi dữ liệu giúp model không tra ngày bot không có dữ liệu, và nói rõ khi dữ
        liệu không phủ hết khoảng được hỏi. Đọc sheet lỗi thì ghi lỗi vào phần đó, model vẫn tự gọi công cụ được."""
        with timed(log, "nạp sẵn dữ liệu cho hỏi đáp, chat %s", self.storage.chat_label(chat_id)):
            try:
                tasks = await asyncio.to_thread(self._list_tasks, chat_id, today, None, None, memo)
            except Exception as e:
                log.warning("Nạp sẵn danh sách task thất bại: %s", e)
                tasks = {"error": f"Không đọc được Google Sheet: {e}"}
            day = self._get_day(chat_id, today)
        return "\n".join([
            "Dữ liệu có sẵn (không cần gọi lại công cụ cho phần này):",
            "Phạm vi dữ liệu tin nhắn của cuộc trò chuyện:",
            json.dumps(self._coverage(chat_id), ensure_ascii=False),
            "list_tasks (mặc định: task giao hoặc cập nhật trong 30 ngày gần nhất):",
            json.dumps(tasks, ensure_ascii=False),
            f"get_day({today}):",
            json.dumps(day, ensure_ascii=False),
        ])

    # ---- vòng hỏi đáp ----

    async def answer(
        self,
        chat_id: int,
        chat_title: str,
        today: str,
        asker: str,
        question: str,
        can_edit: bool = False,
        history: Sequence[tuple[str, str, str]] = (),
    ) -> tuple[str, list[dict]]:
        """Trả về (câu trả lời, danh sách đề xuất sửa task đang chờ xác nhận).
        `history`: các lượt (câu hỏi, câu trả lời, ngày hỏi YYYY-MM-DD) trước của cùng chủ đề, để model hiểu câu
        hỏi tiếp theo. Mỗi câu hỏi gửi model đều có ngày hỏi ở đầu (xem question_line)."""
        proposals: list[dict] = []
        tools = TOOLS + [EDIT_TOOL] if can_edit else TOOLS
        system = SYSTEM_PROMPT.format(
            title=chat_title,
            today=today,
            today_label=day_label(today),
            past_rules=PAST_RULES,
            deadline_rules=DEADLINE_RULES,
            edit_rule=EDIT_ALLOWED if can_edit else EDIT_DENIED,
            calendar=calendar_text(today),
        )

        memo: dict = {}  # đọc Google Sheet một lần cho cả lần trả lời này

        async def execute(call: ToolCall) -> str:
            try:
                out = await self._run_tool(
                    call.name, call.arguments, chat_id, today, proposals if can_edit else None, memo
                )
            except ValueError as e:
                # Tham số sai (vd. ngày sai dạng): model đọc lỗi rồi gọi lại, không cần traceback
                log.info("Công cụ %s: tham số không hợp lệ: %s", call.name, e)
                out = {"error": str(e)}
            except Exception as e:
                log.exception("Công cụ %s lỗi", call.name)
                out = {"error": str(e)}
            return json.dumps(out, ensure_ascii=False)

        with timed(log, "hỏi đáp, chat %s (tổng, kể cả nạp sẵn dữ liệu)", self.storage.chat_label(chat_id)):
            preloaded = await self._preload(chat_id, today, memo)
            text = await run_tool_loop(
                self.llm,
                system=system,
                prompt=f"{question_line(today, asker, question)}\n\n{preloaded}",
                history=[
                    m for q, a, day in history
                    for m in (ChatMessage.user(question_line(day, asker, q)), ChatMessage("assistant", text=a))
                ],
                tools=tools,
                execute=execute,
                max_rounds=MAX_TOOL_ROUNDS,
                max_tokens=2000,
                purpose="hỏi đáp",
            )
        if text is None:
            return "Xin lỗi, mình chưa tìm được câu trả lời. Bạn thử hỏi cụ thể hơn nhé.", proposals
        return text, proposals
