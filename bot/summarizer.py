"""Xử lý hội thoại trong ngày bằng LLM (provider bất kỳ), theo hai pha:

A. Ghi task: model đọc các task hiện có trên Google Sheet (định danh bằng STT) cùng các tin nhắn
   mới, rồi gọi công cụ add_task / update_task. Mỗi lệnh được ghi ngay vào sheet.
B. Tóm tắt: model gộp tin nhắn mới vào bản tóm tắt của ngày (giống cách Claude nén cuộc trò chuyện dài).

Chỉ khi cả hai pha xong thì mốc compacted_until mới tiến lên. Nếu một pha lỗi, lần thử lại xử lý
lại cùng các tin nhắn; vì model đọc lại sheet (đã có các dòng vừa ghi) nên không thêm task trùng.
"""
import asyncio
import json
import logging

from .dates import DEADLINE_RULES, calendar_text
from .providers import ChatMessage, LLMProvider, Tool, ToolCall, run_tool_loop
from .sheets import FIELDS, TaskSheet, is_general, is_marked
from .storage import DaySummary, Message, Storage

log = logging.getLogger(__name__)

# Khi nén giữa ngày, giữ lại bấy nhiêu tin nhắn gần nhất ở dạng gốc để bot vẫn có ngữ cảnh chi tiết.
KEEP_RECENT_RAW = 30
# Số task gần nhất (theo STT) đưa cho model để đối chiếu
MAX_SHEET_ROWS_IN_PROMPT = 300
MAX_TASK_ROUNDS = 10

TASK_SYSTEM_PROMPT = """Bạn là trợ lý quản lý task cho một cuộc trò chuyện Telegram làm việc (nhóm chat hoặc chat riêng).
Bạn nhận: danh sách task hiện có trên Google Sheet (mỗi task định danh bằng STT), bản tóm tắt
hội thoại trước đó của ngày {day}, và các tin nhắn mới. Hãy cập nhật sheet bằng công cụ.

Quy tắc:
- Task là việc được giao, nhận, hoặc thống nhất sẽ làm (kể cả cách nói tự nhiên như
  "em làm cái này nhé", "anh check giúp", "mai nộp báo cáo").
- CHỈ ghi task khi xác định được người thực hiện cụ thể: người được gọi tên hoặc được trả lời
  giao việc, hoặc người tự nhận ("để em làm" → chính người gửi tin). Không ghi thành task:
  lịch họp, sự kiện, thông báo hay nhắc nhở chung cho cả nhóm ("cả team họp lúc 3h",
  "mọi người nhớ cập nhật timesheet") vì không có người thực hiện cụ thể.
- Nếu tin nhắn nói về một task ĐÃ CÓ trên sheet (kể cả task giao từ ngày trước, hoặc gọi bằng tên
  khác), gọi update_task với đúng STT đó. Không tạo dòng mới cho task đã có.
- update_task chỉ gửi những trường mà tin nhắn thực sự làm thay đổi. Giá trị trên sheet có thể đã
  được người dùng sửa tay: coi đó là đúng, không đổi lại trừ khi tin nhắn nói rõ.
- Task mới hoàn toàn thì gọi add_task.
- assignee (Người thực hiện): người nhận làm task, ghi tên như trong chat (ví dụ "Lan" trong
  "Lan ơi em làm..."). Khi giao lại cho người khác thì update_task đổi assignee.
- Task đã có trên sheet mà assignee còn trống: nếu nội dung task, bản tóm tắt hoặc tin nhắn cho
  biết ai làm (ví dụ nội dung "Minh kiểm tra lỗi đăng nhập") thì update_task điền assignee.
- Task bị huỷ: update_task với progress = "Đã huỷ". Không bao giờ xoá task.
- Dòng có người thực hiện "Tất cả" là lịch họp / thông báo chung (không phải task); dòng có nội dung
  "Trùng với task #..." là dòng trùng task khác. Không sửa các dòng này, không coi "Tất cả" là tên người.
- Deadline: ghi dạng YYYY-MM-DD, để trống nếu không có. Ngày mốc là ngày gửi tin nhắn (ngày {day}).
{deadline_rules}
- Tin nhắn có nhãn "(đã sửa)" là nội dung đã được người gửi sửa lại; dùng nội dung mới.
- Không bịa thông tin không có trong chat. Nếu không có gì cần ghi, không gọi công cụ.
- Khi xong, trả lời ngắn "Xong".

Lịch:
{calendar}"""

TASK_FIELDS_SCHEMA = {
    "name": {"type": "string", "description": "Tên task ngắn gọn."},
    "assignee": {
        "type": "string",
        "description": "Người thực hiện (tên như trong chat; nhiều người thì cách nhau dấu phẩy). "
        "Bắt buộc khi thêm task mới.",
    },
    "description": {"type": "string", "description": "Nội dung task (ghi cả người giao nếu có)."},
    "progress": {"type": "string", "description": "Tiến độ, ví dụ: Mới giao, Đang làm, Xong, Đã huỷ."},
    "deadline": {"type": "string", "description": "Deadline dự kiến."},
}

TASK_TOOLS = [
    Tool(
        name="add_task",
        description="Thêm một task mới vào cuối sheet. Trả về STT của task.",
        parameters={
            "type": "object",
            "properties": TASK_FIELDS_SCHEMA,
            "required": ["name", "assignee", "description", "progress", "deadline"],
        },
    ),
    Tool(
        name="update_task",
        description="Sửa task có STT cho trước. Chỉ gửi các trường cần đổi.",
        parameters={
            "type": "object",
            "properties": {
                "stt": {"type": "integer", "description": "STT của task trên sheet."},
                "changes": {"type": "object", "properties": TASK_FIELDS_SCHEMA},
            },
            "required": ["stt", "changes"],
        },
    ),
]

SUMMARY_SYSTEM_PROMPT = """Bạn là trợ lý ghi chép cho một cuộc trò chuyện Telegram làm việc (nhóm chat hoặc chat riêng).
Nhiệm vụ: cập nhật bản tóm tắt hội thoại của ngày {day}.

- Viết bằng tiếng Việt, ngắn gọn, theo thứ tự thời gian, nêu ai nói/quyết định gì.
- Gộp bản tóm tắt cũ với nội dung mới thành một bản duy nhất, không lặp lại.
- Tin nhắn có nhãn "(đã sửa)" là nội dung đã được sửa lại; nếu nó khác điều bản tóm tắt cũ ghi,
  hãy sửa bản tóm tắt cho đúng.
- Task đã được ghi trên Google Sheet; trong tóm tắt chỉ cần nhắc ngắn (kèm STT nếu biết).
- Không bịa thông tin không có trong chat."""

SUMMARY_TOOL = Tool(
    name="record_summary",
    description="Ghi bản tóm tắt cập nhật của ngày.",
    parameters={
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "Bản tóm tắt cập nhật của ngày."}},
        "required": ["summary"],
    },
)


def _format_messages(messages: list[Message]) -> str:
    return "\n".join(m.format_line() for m in messages)


def _task_prompt(tasks: list[dict], current: DaySummary, messages: list[Message]) -> str:
    shown = sorted(tasks, key=lambda t: t["stt"])[-MAX_SHEET_ROWS_IN_PROMPT:]
    missing = [t["stt"] for t in shown if not t.get("assignee", "").strip() and not is_marked(t)]
    parts = [
        "## Task hiện có trên sheet",
        json.dumps(shown, ensure_ascii=False, indent=1) if shown else "(chưa có)",
        "",
    ]
    if missing:
        parts += [
            "## Task chưa có người thực hiện: STT " + ", ".join(map(str, missing)),
            "Với từng task này, nếu nội dung task, bản tóm tắt hoặc tin nhắn cho biết người làm cụ thể "
            "thì gọi update_task điền assignee, kể cả khi tin nhắn mới không nhắc tới task đó. "
            "Không xác định được người cụ thể (ví dụ việc chung của cả nhóm) thì để nguyên.",
            "",
        ]
    return "\n".join(parts + [
        "## Bản tóm tắt trước đó của ngày",
        current.summary or "(chưa có)",
        "",
        "## Tin nhắn mới",
        _format_messages(messages),
    ])


async def run_task_tool(sheet: TaskSheet, name: str, args: dict, day: str) -> dict:
    if name == "add_task":
        if not str(args.get("assignee") or "").strip() or is_general(args.get("assignee")):
            # Không có người thực hiện cụ thể (lịch họp, thông báo chung...) thì không phải task
            return {"error": "Không ghi: task phải có người thực hiện cụ thể (assignee)."}
        stt = await asyncio.to_thread(sheet.add, args, day)
        return {"stt": stt}
    if name == "update_task":
        changes = {k: v for k, v in (args.get("changes") or {}).items() if k in FIELDS}
        if sheet.get(args["stt"]) is None:
            return {"error": f"Không có task STT {args['stt']}"}
        if not changes:
            return {"error": "Không có trường nào để đổi"}
        return {"stt": args["stt"], "values": await asyncio.to_thread(sheet.update, args["stt"], changes, day)}
    return {"error": f"Không có công cụ {name}"}


async def record_tasks(
    sheet: TaskSheet, llm: LLMProvider, day: str, current: DaySummary, messages: list[Message]
) -> None:
    """Pha A: model đọc sheet + tin nhắn mới, gọi add_task / update_task để ghi vào sheet.

    Lỗi khi ghi sheet được ném ra ngoài (để cả lượt xử lý được thử lại); còn lỗi do model gọi sai
    (STT không tồn tại...) thì báo lại cho model tự sửa.
    """

    async def execute(call: ToolCall) -> str:
        return json.dumps(await run_task_tool(sheet, call.name, call.arguments, day), ensure_ascii=False)

    answer = await run_tool_loop(
        llm,
        system=TASK_SYSTEM_PROMPT.format(day=day, deadline_rules=DEADLINE_RULES, calendar=calendar_text(day)),
        prompt=_task_prompt(sheet.rows(), current, messages),
        tools=TASK_TOOLS,
        execute=execute,
        max_rounds=MAX_TASK_ROUNDS,
    )
    if answer is None:
        raise RuntimeError("Model gọi công cụ quá nhiều vòng khi ghi task")


async def summarize(llm: LLMProvider, day: str, current: DaySummary, messages: list[Message]) -> str:
    """Pha B: gộp tin nhắn mới vào bản tóm tắt của ngày."""
    prompt = "\n".join([
        "## Bản tóm tắt hiện có",
        current.summary or "(chưa có)",
        "",
        "## Tin nhắn mới",
        _format_messages(messages),
    ])
    response = await llm.complete(
        system=SUMMARY_SYSTEM_PROMPT.format(day=day),
        messages=[ChatMessage.user(prompt)],
        tools=[SUMMARY_TOOL],
        tool_choice=SUMMARY_TOOL.name,
        max_tokens=8000,
    )
    call = next((c for c in response.tool_calls if c.name == SUMMARY_TOOL.name), None)
    if call is None or "summary" not in call.arguments:
        raise RuntimeError("Model không trả về bản tóm tắt")
    return call.arguments["summary"]


def messages_to_process(storage: Storage, current: DaySummary, keep_recent: int) -> list[Message]:
    """Các tin cần xử lý, trừ `keep_recent` tin chưa nén gần nhất (giữ ở dạng gốc làm ngữ cảnh)."""
    pending = storage.pending_messages(current.chat_id, current.day, current.compacted_until)
    if keep_recent <= 0:
        return pending
    fresh = [m for m in pending if m.id > current.compacted_until]
    keep = {m.id for m in fresh[-keep_recent:]}
    return [m for m in pending if m.id not in keep]


async def process_day(
    storage: Storage,
    sheet: TaskSheet,
    llm: LLMProvider,
    chat_id: int,
    day: str,
    finalize: bool = False,
    keep_recent: bool = True,
) -> DaySummary | None:
    """Ghi task lên sheet rồi nén các tin nhắn chưa xử lý của ngày vào bản tóm tắt.

    finalize=False (giữa ngày): ngày chưa chốt. Mặc định giữ lại KEEP_RECENT_RAW tin nhắn gần nhất
      ở dạng gốc (nén tự động khi vượt ngưỡng); keep_recent=False thì xử lý hết (lệnh /tomtat).
    finalize=True (chốt ngày): xử lý toàn bộ và đánh dấu ngày đã chốt.
    Trả về None nếu không có gì để xử lý. Lỗi (LLM hoặc Google Sheet) được ném ra để gọi lại.
    """
    current = storage.get_summary(chat_id, day)
    batch = messages_to_process(storage, current, KEEP_RECENT_RAW if keep_recent and not finalize else 0)
    if not batch:
        if finalize and not current.finalized:
            current.finalized = True
            storage.save_summary(current)
        return None

    await record_tasks(sheet, llm, day, current, batch)
    summary = await summarize(llm, day, current, batch)

    updated = DaySummary(
        chat_id=chat_id,
        day=day,
        summary=summary,
        compacted_until=max(current.compacted_until, max(m.id for m in batch)),
        finalized=finalize,
    )
    storage.save_summary(updated)
    storage.clear_edit_pending([m.id for m in batch if m.edit_pending])
    log.info("Đã xử lý %d tin nhắn của chat %s ngày %s", len(batch), storage.chat_label(chat_id), day)
    return updated


def pending_chars(storage: Storage, chat_id: int, day: str) -> int:
    current = storage.get_summary(chat_id, day)
    return sum(len(m.text) for m in storage.pending_messages(chat_id, day, current.compacted_until))
