"""Rà soát sheet của một nhóm (chạy khi bot khởi động), sửa các vấn đề tồn đọng:

1. Dòng nhập tay chưa có STT: gán STT kế tiếp (code tự làm, không cần LLM).
2. Task thiếu người thực hiện: điền từ nội dung task / bản tóm tắt ngày giao.
3. Deadline chưa chuẩn ("mai", "đầu tuần sau"...): quy đổi sang YYYY-MM-DD theo ngày giao task
   (bảng lịch và quy ước trong bot/dates.py).
4. Dòng không phải task thật (lịch họp, thông báo chung, không có người thực hiện cụ thể):
   đánh dấu Người thực hiện = "Tất cả", Tiến độ để trống, Nội dung giữ nguyên. Không xoá dòng.
5. Task trùng lặp: giữ dòng STT nhỏ hơn (bổ sung thông tin còn thiếu), dòng kia đánh dấu
   "Trùng với task #N".

Sau khi rà xong, lưu "dấu vân tay" của sheet (kèm quy tắc rà soát): lần khởi động sau, sheet và
quy tắc không đổi thì bỏ qua (không gọi LLM).
"""
import asyncio
import hashlib
import json
import logging
import re

from .dates import DEADLINE_RULES, calendar_text
from .providers import LLMProvider, Tool, ToolCall, run_tool_loop
from .sheets import FIELDS, TaskSheet, is_general, is_marked
from .storage import Storage
from .summarizer import TASK_TOOLS, run_task_tool

log = logging.getLogger(__name__)

MAX_REVIEW_ROUNDS = 10
# Số ngày giao gần nhất lấy bản tóm tắt làm ngữ cảnh
MAX_SUMMARY_DAYS = 14
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

REVIEW_SYSTEM_PROMPT = """Bạn rà soát bảng task (Google Sheet) của một nhóm chat Telegram. Hôm nay là {today}.
Bạn nhận: các task trên sheet (STT, tên, người thực hiện, nội dung, tiến độ, deadline, ngày giao),
danh sách vấn đề đã phát hiện, và bản tóm tắt hội thoại của các ngày giao task. Sửa bằng công cụ:

1. Thiếu người thực hiện: nếu nội dung task hoặc bản tóm tắt cho biết người làm cụ thể thì
   update_task điền assignee (tên như trong chat).
2. Deadline chưa ở dạng YYYY-MM-DD: quy đổi theo NGÀY GIAO của task đó (không phải hôm nay), dùng
   bảng lịch của ngày giao trong phần "Lịch theo ngày giao", rồi update_task. Ngày mốc = ngày giao.
{deadline_rules}
3. Dòng KHÔNG phải task thật: lịch họp, sự kiện, thông báo hay nhắc nhở chung cho cả nhóm, không có
   người thực hiện cụ thể (ví dụ "Họp nhanh cả team") → mark_row với reason = "not_task".
   Trước đó, nếu deadline của dòng chưa chuẩn thì vẫn quy đổi như mục 2.
4. Task trùng: hai dòng cùng một việc (cùng nội dung công việc, cùng người làm) → giữ dòng STT nhỏ
   hơn; nếu dòng bị trùng có thông tin mà dòng giữ lại còn thiếu thì update_task bổ sung vào dòng giữ
   lại; rồi mark_row dòng kia với reason = "duplicate", duplicate_of = STT dòng giữ lại.
   Chỉ coi là trùng khi chắc chắn; hai việc giống nhau nhưng khác người làm hoặc khác đợt thì KHÔNG trùng.

Không sửa gì khác: giá trị trên sheet có thể do người dùng sửa tay, coi đó là đúng.
Không bịa thông tin. Không có gì cần sửa thì không gọi công cụ. Xong thì trả lời ngắn "Xong"."""

MARK_TOOL = Tool(
    name="mark_row",
    description="Đánh dấu một dòng không phải task thật (not_task: Người thực hiện = \"Tất cả\", nội dung "
    "giữ nguyên) hoặc trùng task khác (duplicate: nội dung = \"Trùng với task #N\"). Dòng được giữ lại "
    "trên sheet, tiến độ để trống.",
    parameters={
        "type": "object",
        "properties": {
            "stt": {"type": "integer", "description": "STT của dòng cần đánh dấu."},
            "reason": {"type": "string", "enum": ["not_task", "duplicate"]},
            "duplicate_of": {"type": "integer", "description": "STT của dòng giữ lại (khi reason = duplicate)."},
        },
        "required": ["stt", "reason"],
    },
)

UPDATE_TOOL = next(t for t in TASK_TOOLS if t.name == "update_task")


def fingerprint(tasks: list[dict]) -> str:
    rows = [[t["stt"], *(t.get(f, "") for f in FIELDS)] for t in sorted(tasks, key=lambda t: t["stt"])]
    # Kèm quy tắc rà soát: đổi prompt / quy ước ngày thì lần khởi động sau rà lại dù sheet không đổi
    rules = [REVIEW_SYSTEM_PROMPT, DEADLINE_RULES]
    return hashlib.sha256(json.dumps([rules, rows], ensure_ascii=False).encode()).hexdigest()


def find_issues(tasks: list[dict]) -> dict[str, list[int]]:
    """Các vấn đề phát hiện được bằng code (gợi ý cho model; trùng lặp thì model tự xét)."""
    active = [t for t in tasks if not is_marked(t)]
    return {
        "thiếu người thực hiện": [t["stt"] for t in active if not t["assignee"].strip()],
        "deadline chưa ở dạng YYYY-MM-DD": [
            t["stt"] for t in tasks if t["deadline"].strip() and not ISO_DATE.fullmatch(t["deadline"].strip())
        ],
    }


def _prompt(tasks: list[dict], issues: dict[str, list[int]], summaries: dict[str, str]) -> str:
    shown = [{k: v for k, v in t.items() if k != "updated_day"} for t in tasks]
    parts = ["## Task trên sheet", json.dumps(shown, ensure_ascii=False, indent=1), "", "## Vấn đề đã phát hiện"]
    parts += [f"- {name}: STT {', '.join(map(str, stts))}" for name, stts in issues.items() if stts]
    parts += ["- task trùng lặp / dòng không phải task: tự xét trên toàn bộ danh sách", ""]
    # Lịch của từng ngày giao có task cần quy đổi deadline
    to_convert = set(issues.get("deadline chưa ở dạng YYYY-MM-DD", []))
    days = sorted({t["created_day"] for t in tasks if t["stt"] in to_convert and t["created_day"]})
    if days:
        parts += ["## Lịch theo ngày giao"] + [calendar_text(day) for day in days] + [""]
    parts += ["## Bản tóm tắt các ngày giao task"]
    parts += [f"### {day}\n{text or '(không có)'}" for day, text in summaries.items()] or ["(không có)"]
    return "\n".join(parts)


async def _run_review_tool(sheet: TaskSheet, call: ToolCall, today: str) -> dict:
    args = call.arguments
    if call.name == "update_task":
        return await run_task_tool(sheet, "update_task", args, today)
    if call.name != "mark_row":
        return {"error": f"Không có công cụ {call.name}"}
    stt, reason = args.get("stt"), args.get("reason")
    row = sheet.get(stt) if isinstance(stt, int) else None
    if row is None:
        return {"error": f"Không có task STT {stt}"}
    if reason == "not_task":
        if row["assignee"].strip() and not is_general(row["assignee"]):
            return {"error": "Dòng đã có người thực hiện cụ thể nên là task thật, không đánh dấu not_task."}
        values = await asyncio.to_thread(sheet.mark_general, stt, today)
    elif reason == "duplicate":
        keep = args.get("duplicate_of")
        kept = sheet.get(keep) if isinstance(keep, int) else None
        if kept is None or keep == stt or is_marked(kept):
            return {"error": "duplicate_of phải là STT của một task khác, chưa bị đánh dấu."}
        values = await asyncio.to_thread(sheet.mark_duplicate, stt, keep, today)
    else:
        return {"error": "reason phải là not_task hoặc duplicate"}
    return {"stt": stt, "values": values}


async def review_sheet(storage: Storage, sheet: TaskSheet, llm: LLMProvider, chat_id: int, today: str) -> bool:
    """Rà soát và sửa sheet của một nhóm. Trả về True nếu đã chạy rà soát, False nếu sheet không đổi
    kể từ lần rà trước (bỏ qua). Lỗi (LLM / Google Sheet) được ném ra; lần khởi động sau rà lại."""
    assigned = await asyncio.to_thread(sheet.assign_missing_stt, today)
    if assigned:
        log.info("Chat %s: gán STT cho dòng nhập tay: %s", chat_id, assigned)

    tasks = sheet.rows()
    if storage.review_fingerprint(chat_id) == fingerprint(tasks):
        return False
    if any(not is_marked(t) for t in tasks):
        days = sorted({t["created_day"] for t in tasks if t["created_day"]})[-MAX_SUMMARY_DAYS:]
        summaries = {day: storage.get_summary(chat_id, day).summary for day in days}

        async def execute(call: ToolCall) -> str:
            return json.dumps(await _run_review_tool(sheet, call, today), ensure_ascii=False)

        answer = await run_tool_loop(
            llm,
            system=REVIEW_SYSTEM_PROMPT.format(today=today, deadline_rules=DEADLINE_RULES),
            prompt=_prompt(tasks, find_issues(tasks), summaries),
            tools=[UPDATE_TOOL, MARK_TOOL],
            execute=execute,
            max_rounds=MAX_REVIEW_ROUNDS,
        )
        if answer is None:
            raise RuntimeError("Model gọi công cụ quá nhiều vòng khi rà soát sheet")
    storage.save_review(chat_id, fingerprint(sheet.rows()))
    log.info("Đã rà soát sheet của chat %s", chat_id)
    return True
