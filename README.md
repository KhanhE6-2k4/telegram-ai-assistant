# Bot Telegram quản lý task bằng LLM

Bot đọc toàn bộ tin nhắn trong nhóm, lưu và tóm tắt theo ngày, tự nhận diện task được giao và ghi
vào Google Sheet, trả lời câu hỏi qua lệnh `/ask` hoặc khi được tag `@ten_bot`, và cho người có quyền sửa task qua tin nhắn.
Bot dùng được nhiều nhà cung cấp LLM (Anthropic Claude, OpenAI, ...), xem mục [LLM provider](#llm-provider).

## Tính năng

**Tự ghi nhận task.** Bot hiểu cách giao việc tự nhiên ("Lan ơi em làm báo cáo Q3 nhé, thứ 6 nộp")
và ghi lên Google Sheet: tên task, người thực hiện, nội dung, tiến độ, deadline (quy ra YYYY-MM-DD).
- Chỉ ghi khi xác định được **người thực hiện cụ thể** (người được gọi tên, hoặc người tự nhận
  "để em làm"). Lịch họp, sự kiện, nhắc nhở chung cho cả nhóm không được ghi.
- Chat nhắc lại một task đã có (kể cả của ngày trước) thì bot **sửa đúng dòng đó** theo STT, không thêm
  dòng mới. Giao lại cho người khác thì đổi người thực hiện; huỷ thì Tiến độ = "Đã huỷ".
- Bot chỉ ghi những ô mà nội dung chat làm thay đổi, nên **chỉnh sửa tay trên sheet được giữ nguyên**.

**Hỏi đáp.** Dùng lệnh `/ask`, ví dụ `/ask task báo cáo Q3 giao hôm qua nội dung là gì, hạn khi nào?`,
hoặc tag bot: `@ten_bot task báo cáo Q3 giao hôm qua nội dung là gì, hạn khi nào?` (tag ở đầu, giữa hay
cuối câu đều được). Hai cách như nhau, dùng cách nào tuỳ thích. Bot tra sheet, bản tóm tắt các ngày và cả tin nhắn chưa xử lý để trả lời; câu hỏi không cần ghi rõ
tên task hay ngày.

**Sửa task qua tin nhắn.** Người có quyền dùng `/ask dời hạn task báo cáo Q3 sang 10/11` (hoặc
`@ten_bot dời hạn task báo cáo Q3 sang 10/11`).
Bot hiện nút Xác nhận / Huỷ; chỉ ghi vào sheet sau khi **chính người yêu cầu** bấm Xác nhận.
Người có quyền: admin của nhóm Telegram, cộng với các user id trong `EDITOR_USER_IDS`.

**Lệnh** (hiện trong menu khi gõ `/`). Chỉ cần gõ `/task`, `/ask ...`; không cần thêm
`@ten_bot` (ứng dụng Telegram có thể tự chèn khi chọn từ menu, dạng đó vẫn chạy):

| Lệnh | Tác dụng | Gọi LLM? |
|---|---|---|
| `/ask <câu hỏi>` | Hỏi về task / nội dung trao đổi; người có quyền nhờ sửa task (có nút Xác nhận) | Có |
| `/task` | Liệt kê **mọi task** của nhóm, đọc thẳng từ sheet (bỏ dòng "Tất cả" và dòng trùng) | Không |
| `/task <tên>` | Chỉ task có người thực hiện chứa `<tên>` (không phân biệt hoa thường), ví dụ `/task lan` | Không |
| `/tomtat` | Xử lý ngay mọi tin nhắn chưa xử lý của hôm nay (ghi task lên sheet, cập nhật tóm tắt), rồi liệt kê task được giao / cập nhật hôm nay | Có (nếu có tin mới) |
| `/tomtat YYYY-MM-DD` | Liệt kê task được giao / cập nhật trong ngày đó (chỉ đọc) | Không |

`/task` chỉ thấy những gì đã lên sheet; nếu hôm nay còn tin chưa xử lý, bot nhắc gõ `/tomtat`.

Bấm `/ask` trong menu gợi ý thì ứng dụng Telegram gửi ngay lệnh trống (không cho gõ tiếp câu hỏi,
bot không đổi được hành vi này). Khi đó bot hỏi lại "Bạn muốn hỏi gì?" và **tự mở ô trả lời** cho đúng
người đó: gõ câu hỏi vào rồi gửi là xong (chỉ có hiệu lực một lần, người khác trả lời thì là tin thường).
Muốn chèn `/ask` vào ô chat mà không gửi ngay: trên điện thoại **nhấn giữ** lệnh trong danh sách gợi ý;
trên Telegram Desktop chọn lệnh rồi bấm **Tab**.

Tin tag `@ten_bot` được xử lý giống hệt `/ask` với phần còn lại của tin (bỏ chữ `@ten_bot`) là câu
hỏi; chỉ tag mà không kèm gì thì bot hỏi lại "Bạn muốn hỏi gì?" như `/ask` trống. Tin tag bot và các
lệnh không được lưu vào hội thoại của nhóm, nên một yêu cầu sửa task chỉ có hiệu lực qua nút Xác nhận.
Sửa lại tin lệnh / tin tag bot đã gửi thì bot không chạy lại; muốn hỏi tiếp hãy gửi tin mới.

## Quy trình xử lý

### Một tin nhắn

```
Lệnh /ask, /task, /tomtat ─> thực hiện ngay, không lưu vào hội thoại
                             (gửi lúc bot tắt → reply "tạm ngừng, hãy hỏi / gõ lại")
Tin tag @bot ──────────────> trả lời như /ask (câu hỏi = tin bỏ chữ @bot), không lưu

Tin nhắn thường
  └─> lưu vào messages (chat_id, day theo giờ địa phương)
        │
        └─ chờ một lượt xử lý:
             • vượt COMPACT_THRESHOLD_CHARS  (giữ lại 30 tin gần nhất)
             • /tomtat                       (xử lý hết)
             • 00:01 (DAILY_SUMMARY_TIME)    (chốt các ngày trước, finalized = 1)
             • khi khởi động                 (xem bên dưới)
                    │
                    ▼
             Bước A – Ghi task: model đọc tối đa 300 task gần nhất trên Sheet
                      + tóm tắt hiện có của ngày + các tin chưa xử lý
                      → add_task / update_task theo STT → ghi ngay lên Sheet
                    │  (chỉ khi bước A thành công)
                    ▼
             Bước B – Tóm tắt: gộp các tin đó vào bản tóm tắt của ngày
                    │
                    ▼
             Lưu tóm tắt, tăng compacted_until, xoá cờ edit_pending
             (lỗi ở bất kỳ bước nào → thử lại, tin vẫn ở trạng thái chưa xử lý)
```

- **Thử lại:** LLM hoặc Google Sheet lỗi thì thử lại tối đa `COMPACT_MAX_ATTEMPTS` lần (chờ
  `COMPACT_RETRY_DELAY_SECONDS` giây, nhân đôi mỗi lần). Nếu vẫn lỗi, lần chốt sau xử lý lại. Thử lại
  sau khi đã ghi một phần không tạo task trùng (model đọc lại sheet).
- **Chốt ngày lúc 00:01:** mọi ngày trước hôm nay chưa chốt đều được xử lý nốt và đánh dấu đã chốt,
  kể cả khi trong ngày đã chạy `/tomtat` hay đã nén vì vượt ngưỡng.
- **Tin nhắn bị sửa** được cập nhật và đưa lại vào lần xử lý tới (ngày đã chốt được mở lại và chốt
  lại lúc 00:01 hôm sau). Tin lệnh chỉ có hiệu lực lần gửi đầu: sửa lại thì bot bỏ qua.
- **Lúc bot tắt:** Telegram gửi bù tin nhắn (tối đa 24 giờ) khi bot chạy lại. Tin thường được lưu như
  bình thường; lệnh (`/ask`, `/task`, `/tomtat`) và tin tag bot gửi lúc bot tắt không được thực hiện muộn, bot chỉ
  báo là lúc đó đang tạm ngừng và nhờ hỏi / gõ lại.

### Khi khởi động

Sau khoảng 10 giây (để nhận hết tin Telegram gửi bù lúc bot tắt), bot lần lượt:
1. Chốt bù các ngày trước hôm nay còn dở.
2. Xử lý hết tin chưa xử lý của hôm nay (như `/tomtat`, không chốt ngày).
3. **Rà soát sheet** của từng nhóm:
   - dòng nhập tay chưa có STT → gán STT kế tiếp;
   - thiếu người thực hiện → điền từ nội dung task / tóm tắt ngày giao;
   - deadline chưa chuẩn ("mai", "đầu tuần sau") → đổi sang YYYY-MM-DD theo ngày giao task (xem
     "Quy đổi deadline" bên dưới);
   - dòng không phải task (lịch họp, thông báo chung) → giữ dòng, Người thực hiện = `Tất cả`, Tiến độ
     để trống, Nội dung giữ nguyên;
   - task trùng → giữ dòng STT nhỏ hơn, dòng kia ghi Nội dung = `Trùng với task #N`.

   Sheet và quy tắc rà soát không đổi kể từ lần rà trước thì bỏ qua (không gọi LLM). Bot không bao
   giờ xoá dòng.

### Quy đổi deadline

Deadline luôn ghi dạng YYYY-MM-DD. Code (`bot/dates.py`) tính sẵn bảng lịch của ngày mốc (tuần này
và 2 tuần tới, ngày cuối tháng) đưa vào prompt, model chỉ tra bảng, không tự nhẩm thứ. Ngày mốc là
ngày gửi tin khi ghi task, ngày giao task khi rà soát, hôm nay khi `/ask`. Tuần tính từ thứ Hai đến
Chủ nhật:

| Cụm trong chat | Deadline |
|---|---|
| mai / ngày kia | ngày mốc + 1 / + 2 |
| thứ N (này) / thứ N tuần sau | thứ N của tuần này / tuần sau |
| đầu tuần sau | thứ Hai tuần sau |
| giữa tuần / giữa tuần sau | thứ Năm tuần này / tuần sau |
| cuối tuần / cuối tuần sau | Chủ nhật tuần này / tuần sau |
| cuối tháng / cuối tháng sau | ngày cuối cùng của tháng này / tháng sau |
| cụm không chỉ được ngày nào ("sớm", "khi nào xong") | ghi nguyên văn |

## Cấu trúc lưu trữ

Có ba nơi lưu: **Google Sheet** (task, nơi chính thức), **SQLite** (hội thoại), và **bộ nhớ** của bot
(mất khi tắt).

### Google Sheet (`SPREADSHEET_ID`)

```
Spreadsheet
├── Tab "Team Marketing"   ← metadata ẩn: telegram_chat_id = -100123..., telegram_tab_title = "Team Marketing"
│     STT | Tên task          | Người thực hiện | Nội dung task       | Tiến độ  | Deadline dự kiến
│     1   | Báo cáo Q3        | Lan             | Lan làm báo cáo Q3  | Mới giao | 2026-11-02
│     2   | Họp nhanh cả team | Tất cả          | Cả team họp lúc 3h  |          | 2026-10-29   ← không phải task
│     3   | Làm báo cáo Q3    |                 | Trùng với task #1   |          |              ← dòng trùng
├── Tab "Team Sales"       ← telegram_chat_id = -100456...
└── Trang tính1            ← không có metadata, bot không đụng tới
```

- **Mỗi nhóm một tab**, tên tab là tên nhóm (trùng tên với tab khác thì thêm chat id phía sau). Tên
  nhóm được lấy từ mọi cập nhật của nhóm (tin nhắn, lệnh, nút bấm, bot được thêm vào nhóm), nên tab tạo
  lần đầu bởi một lệnh như `/task` cũng mang tên nhóm.
- **Bot nhận ra tab bằng chat id gắn ngầm** vào tab (developer metadata `telegram_chat_id`), không dựa
  vào tên: đổi tên, đổi vị trí tab hay mất database vẫn tìm đúng. Nhóm đổi tên thì tab đổi theo; tự đặt
  tên tab khác trên sheet thì bot giữ nguyên tên đó (`telegram_tab_title` ghi tên bot đặt lần cuối).
- **Mỗi task định danh bằng STT** (cột A). Xoá dòng trên sheet không làm đổi STT các dòng khác.
- Nâng cấp tự động từ phiên bản cũ: tab đặt tên theo chat id được đổi sang tên nhóm; tab chưa có cột
  Người thực hiện được chèn cột C (dữ liệu dịch sang phải).

### SQLite (`DB_PATH`, mặc định `data/bot.db`)

| Bảng | Khoá | Nội dung |
|---|---|---|
| `chats` | `chat_id` | Tên nhóm gần nhất (dùng đặt tên tab) |
| `messages` | `id` tự tăng; `(chat_id, message_id)` duy nhất | Từng tin nhắn gốc: người gửi, nội dung, tin được reply, `sent_at`, `day`, `edit_pending` (tin đã xử lý nhưng bị sửa, cần xử lý lại) |
| `day_summaries` | `(chat_id, day)` | Bản tóm tắt của ngày, `compacted_until` (id tin cuối đã xử lý), `finalized` (ngày đã chốt). Cột `tasks_json` của phiên bản cũ không còn dùng |
| `task_meta` | `(chat_id, stt)` | `created_day` (ngày giao), `updated_day` (ngày cập nhật gần nhất) của từng task trên sheet |
| `sheet_reviews` | `chat_id` | Dấu vân tay sheet ở lần rà soát gần nhất (sheet không đổi thì không rà lại) |

Database là file trên ổ đĩa: tắt / khởi động lại bot không mất gì. Khi chạy bằng Docker, nhớ gắn thư
mục `data/` ra ngoài và sao lưu.

### Bộ nhớ (mất khi tắt bot)

- Đề xuất sửa task đang chờ bấm Xác nhận (sau khi khởi động lại, nút cũ báo "đã hết hạn").
- Tab đã tra của mỗi nhóm (giữ 5 phút), khoá chống xử lý trùng của mỗi nhóm, thời điểm khởi động.

### Bot xác định nhóm, ngày, tab, task thế nào

| Câu hỏi | Cách xác định |
|---|---|
| Tin thuộc nhóm nào? | `chat.id` có trong mọi cập nhật Telegram; mọi bảng đều có `chat_id`, dữ liệu các nhóm tách biệt |
| Tin thuộc ngày nào? | Thời điểm **gửi gốc** (`msg.date`, UTC) đổi sang `TIMEZONE` rồi lấy ngày. Tin bị sửa vẫn thuộc ngày gửi gốc |
| Tab nào trên sheet? | Tab có metadata `telegram_chat_id` = chat id |
| Task nào? | STT ở cột A; ngày giao / cập nhật tra trong `task_meta` |
| Tin nào đã xử lý? | `id ≤ compacted_until` của ngày đó, trừ tin có `edit_pending = 1` |
| Ngày nào đã chốt? | `finalized = 1`. Có tin mới / tin bị sửa thuộc ngày đã chốt thì ngày đó được mở lại |

### Hỏi đáp (`/ask`) tra cứu những gì

Với mỗi lệnh `/ask`, model tự gọi công cụ, chỉ trong phạm vi **nhóm đang hỏi**, tối đa 6 lượt gọi model:
- `list_tasks`: task trên sheet, mặc định được giao hoặc cập nhật trong **30 ngày gần nhất** (model có
  thể chọn khoảng ngày khác).
- `get_day`: bản tóm tắt của **một ngày** + tối đa 200 tin **chưa xử lý** của ngày đó. Model luôn xem
  hôm nay, cộng các ngày câu hỏi nhắc tới.

Tin nhắn đã xử lý chỉ còn được tra qua bản tóm tắt (nguyên văn vẫn nằm trong bảng `messages` nhưng
chưa có công cụ nào đọc ra); chưa có tìm kiếm theo từ khoá trên toàn bộ lịch sử.

## Cài đặt

1. **Bot Telegram:** nhắn [@BotFather](https://t.me/BotFather) `/newbot`, lấy token.
   Sau đó `/setprivacy`, chọn bot, `Disable` để bot đọc được mọi tin nhắn
   (nếu bot đã ở trong nhóm, xoá ra rồi thêm lại). Bot không cần quyền admin.
2. **API key của LLM:** Claude tạo tại https://console.anthropic.com (`ANTHROPIC_API_KEY`),
   OpenAI tạo tại https://platform.openai.com (`OPENAI_API_KEY`). Chỉ cần key của provider sẽ dùng.
3. **Google Sheet:**
   - Vào https://console.cloud.google.com, tạo project, bật **Google Sheets API**.
   - Tạo **Service account** (không cần cấp role cho project), tạo key dạng JSON, lưu thành
     `credentials.json` cạnh code.
   - Tạo một Google Sheet trống, bấm Share và thêm email của service account
     (dạng `...@...iam.gserviceaccount.com`) với quyền Editor.
   - `SPREADSHEET_ID` là đoạn giữa `/d/` và `/edit` trong link sheet.
4. **Cấu hình:** `cp .env.example .env` rồi điền các giá trị. Để lấy user id của ai đó, nhờ họ nhắn
   [@userinfobot](https://t.me/userinfobot).
5. **Chạy thử trên máy** (Python 3.12, môi trường ảo `.venv`):

```bash
python -m venv .venv
# kích hoạt .venv theo hệ điều hành: source .venv/bin/activate hoặc .venv\Scripts\activate
pip install -r requirements.txt
python -m bot.main --check   # thử gọi LLM trước
python -m bot.main
```

6. Thêm bot vào nhóm chat.

## LLM provider

Chọn provider bằng biến môi trường `LLM_PROVIDER` / `LLM_MODEL` / `LLM_BASE_URL`, hoặc bằng tham số
dòng lệnh (ghi đè biến môi trường; để trống trong `.env` cũng được):

```bash
python -m bot.main --help
python -m bot.main --provider openai --model <tên-model>       # chạy bot với OpenAI
python -m bot.main --provider anthropic --model claude-haiku-5-5
python -m bot.main --provider openai-chat --model <tên-model> --base-url http://localhost:8000/v1  # dịch vụ tương thích OpenAI
python -m bot.main --check --provider openai --model <tên-model>  # chỉ thử gọi LLM rồi thoát
```

`LLM_MODEL` trong `.env` chỉ áp dụng cho provider ghi trong `LLM_PROVIDER`; chọn provider khác qua
dòng lệnh mà không kèm `--model` thì dùng model mặc định của provider đó.

Các provider có sẵn:
- `anthropic`: Claude (Messages API), model mặc định `claude-sonnet-5-5`.
- `openai`: OpenAI qua Responses API (bắt buộc ghi model). Dùng được với model có suy luận
  (reasoning); phần suy luận được gửi lại giữa các vòng gọi công cụ.
- `openai-chat`: Chat Completions API, cho các dịch vụ tương thích OpenAI chưa có Responses API
  (Ollama, vLLM, OpenRouter, ...). Với OpenAI chính hãng nên dùng `openai`, vì một số model reasoning
  không cho dùng công cụ qua Chat Completions.

`--check` thử provider theo đúng ba kiểu bot dùng (trả lời thường, bắt buộc gọi công cụ, vòng gọi
công cụ nhiều lượt) rồi in kết quả. Lệnh này không cần Telegram hay Google Sheet, chỉ cần API key.

Mã nguồn nằm trong `bot/providers/`:
- `base.py`: giao diện chung `LLMProvider.complete()` (Strategy) và các kiểu dữ liệu trung lập
  (`Tool`, `ToolCall`, `ChatMessage`, `Response`), cùng vòng gọi công cụ `run_tool_loop`.
  Phần còn lại của bot chỉ dùng những thứ này, không phụ thuộc SDK nào.
- `factory.py`: registry + `create_provider(name, model, ...)` (Factory).
- `anthropic_provider.py`, `openai_provider.py`: chuyển đổi sang API riêng của từng hãng. Dữ liệu
  riêng của provider (ví dụ item suy luận) đi kèm qua `ChatMessage.provider_data`.

Thêm provider mới: tạo lớp con `LLMProvider` gắn `@register_provider("ten")`, đặt `api_key_env` và
`default_model`, cài `complete()`, rồi import module trong `bot/providers/__init__.py`.

## Mã nguồn

| File | Vai trò |
|---|---|
| `bot/main.py` | Khởi chạy, nhận tin nhắn / lệnh Telegram, lịch chạy (00:01, khởi động), thử lại |
| `bot/summarizer.py` | Một lượt xử lý: bước A ghi task, bước B tóm tắt |
| `bot/review.py` | Rà soát sheet khi khởi động |
| `bot/qa.py` | Trả lời lệnh `/ask`, đề xuất sửa task |
| `bot/sheets.py` | Đọc / ghi task trên Google Sheet, tìm / tạo tab theo chat id |
| `bot/storage.py` | SQLite: tin nhắn, tóm tắt, ngày giao task |
| `bot/config.py` | Đọc cấu hình từ `.env` và dòng lệnh |
| `bot/providers/` | Nhà cung cấp LLM |
| `bot/checks.py` | Kiểm tra kết nối LLM / Telegram / Google Sheet (`--check` và nút trong app) |
| `bot/runner.py` | Chạy bot trong thread riêng, chờ mạng khi khởi động (app desktop dùng) |
| `telegram_bot/` | App desktop Windows (Toga + Briefcase), xem mục dưới |

## App desktop (Windows)

Bản đóng gói cho người không rành kỹ thuật: cấu hình qua giao diện (không cần `.env`), chạy nền như
dịch vụ VPN (biểu tượng ở khay hệ thống, đóng cửa sổ vẫn chạy, tự khởi động cùng Windows). Dùng chung
toàn bộ logic trong `bot/`; bản dòng lệnh và Docker không đổi.

### Build và cài

Cần Windows và Python 3.12. Dùng một môi trường ảo riêng chỉ chứa Briefcase (quy ước tên `.venv-app`,
tách khỏi `.venv` của bản dòng lệnh):

```bash
python -m venv .venv-app          # rồi kích hoạt .venv-app
pip install briefcase
briefcase dev                     # chạy thử app từ mã nguồn
briefcase dev --test              # chạy test trong môi trường của app
briefcase create windows          # tạo khung app (một lần; làm lại khi đổi icon hoặc cấu hình bộ cài)
briefcase build windows
briefcase package windows         # ra tệp .msi trong dist/
```

Briefcase tự tạo môi trường riêng cho app (thư mục `.briefcase/` khi `briefcase dev`, `build/` khi build)
và cài toga, keyring... theo `requires` trong `pyproject.toml`; không cài các gói này vào `.venv-app`.
Đổi `requires` thì chạy `briefcase dev -r`. Sửa code thì `briefcase update windows` (thêm `-r` nếu đổi
`requires`) rồi build lại. Bộ cài MSI cài theo người dùng (không cần quyền admin), tạo shortcut trong
Start menu và trên màn hình chính (bỏ được bằng ô "Create a desktop shortcut" lúc cài; gỡ app thì shortcut
tự mất). Logo: xem `resources/README.md`.

Bộ cài MSI chỉ nhận chữ trong code page 1252 (không có chữ tiếng Việt), nên mục `windows` trong
`pyproject.toml` dùng `description` không dấu và giấy phép `LICENSE.rtf` (chữ có dấu viết dạng mã
`\uN?` của RTF, hiện đúng trong bộ cài). Sửa `LICENSE` thì sửa cả `LICENSE.rtf`; nếu không, `briefcase
package` báo lỗi `WIX0311`.

⚠️ Không đổi `bundle`, `author`, `formal_name` trong `pyproject.toml` sau khi đã phát hành: chúng quyết
định thư mục dữ liệu, đổi thì bản cài mới không thấy dữ liệu cũ.

### Cấu hình

Tab **Cấu hình**: chọn provider (Anthropic / OpenAI), gõ tên model (điền sẵn model gợi ý của provider), nhập API key
(lưu riêng cho từng provider, đổi qua lại không mất), Telegram bot token, Spreadsheet ID (dán nguyên
link sheet cũng được), rồi chọn tệp khoá Google (`credentials.json`). App kiểm tra tệp đúng là khoá
service account, chép vào thư mục cấu hình, và hiện email để Share sheet (quyền Editor).

Bấm **Kiểm tra kết nối**: app thử LLM, Telegram, Google Sheet và hiện ✓/✗ từng mục. Cả 3 đều ✓ thì
cấu hình được lưu và bot tự (khởi động lại). Không có nút Lưu riêng. Các thiết lập khác (múi giờ, giờ
chốt 00:01, editor ids...) dùng giá trị mặc định như bản dòng lệnh.

Tab **Trạng thái** hiện Đang khởi động / Đang chạy / Đang chờ mạng / Đã dừng / Lỗi (kèm nội dung lỗi)
và nút Bật / Tắt bot.

### Chạy nền

- Đóng cửa sổ: app ẩn xuống khay, bot vẫn chạy. Menu ở biểu tượng khay: Mở cửa sổ, Bật / Tắt bot, Thoát.
- Lần đầu bot kết nối Telegram thành công (bản đã cài), app ghi khoá
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\TelegramBot` = `"<exe>" --background` để tự
  khởi động cùng Windows. Đây là hành vi cố định; chỉ gỡ app mới xoá khoá này.
- Tắt bot hoặc Thoát chỉ có tác dụng trong phiên hiện tại: khởi động lại máy thì bot tự bật lại.
- `--background`: không mở cửa sổ, chỉ hiện ở khay, tự bật bot. Lúc mới khởi động máy chưa có mạng thì
  bot chờ và thử lại (thời gian chờ tăng dần, tối đa 5 phút).
- Chỉ chạy một bản: mở app lần nữa thì cửa sổ của bản đang chạy hiện lên.
- Chạy bằng `briefcase dev` thì không ghi khoá Run.

### Dữ liệu

| Nội dung | Vị trí |
|---|---|
| `settings.json` (provider, model, spreadsheet), `credentials.json` | `%LOCALAPPDATA%\khanhpl2op04\Telegram Bot\Config` |
| `bot.db` (SQLite), `instance.port` | `%LOCALAPPDATA%\khanhpl2op04\Telegram Bot\Data` |
| `bot.log` (xoay vòng 1 MB × 5 tệp, chỉ để tra lỗi) | `%LOCALAPPDATA%\khanhpl2op04\Telegram Bot\Logs` |
| Telegram token, API key từng provider | Windows Credential Manager, dịch vụ `Telegram Bot` |

Bộ cài không đụng tới các chỗ này, nên cài lại là tự dùng tiếp dữ liệu cũ.

### Gỡ app

App không có nút gỡ riêng: gỡ từ **Settings → Apps** (nên Thoát app ở menu khay trước, nhưng không bắt
buộc). Bộ cài chạy `packaging/windows/pre_uninstall.bat`, tệp này gọi
`"Telegram Bot.exe" --uninstall-cleanup`: báo bản đang chạy thoát, xoá khoá Run, rồi cố gắng hỏi có
xoá cả dữ liệu không. Chọn Có thì xoá các thư mục ở bảng trên và khoá trong Credential Manager; chọn
Không hoặc không hỏi được thì giữ nguyên dữ liệu.

## Triển khai lên server (chạy 24/7)

Trên một VPS bất kỳ có Docker:

```bash
docker build -t telegram-task-bot .
mkdir -p data && cp credentials.json data/
docker run -d --name tgbot --restart unless-stopped \
  --env-file .env -e DB_PATH=/data/bot.db -e GOOGLE_CREDENTIALS_FILE=/data/credentials.json \
  -v $(pwd)/data:/data telegram-task-bot
docker logs -f tgbot
```

Mỗi token chỉ nên có **một** bot chạy cùng lúc (chạy cả trên máy lẫn server sẽ bị lỗi `Conflict`).

## Kiểm thử

Trong `.venv` (bản dòng lệnh):

```bash
pip install pytest && python -m pytest -q
```

Hoặc trong `.venv-app`: `briefcase dev --test`.

Test của app desktop (`tests/test_desktop_*.py`) không cần giao diện, mạng hay Windows: keyring và
winreg được thay bằng bản giả trong `tests/fakes_desktop.py`.

## Giới hạn hiện tại

- Chỉ đọc tin nhắn văn bản và chú thích ảnh; chưa xử lý nội dung file, ảnh, voice.
- Telegram không gửi lại tin nhắn cũ, nên bot chỉ biết tin nhắn từ lúc được thêm vào nhóm.
- Đề xuất sửa task chờ xác nhận nằm trong bộ nhớ, bot khởi động lại thì phải yêu cầu lại.
- Sửa task qua bot chỉ áp dụng cho task đã lên sheet (task vừa giao thì gõ `/tomtat` trước).
- Admin gửi ở chế độ ẩn danh thì bot không biết là ai nên không sửa task được; hãy tắt ẩn danh khi
  cần sửa.
- Task chỉ lên sheet khi có lượt xử lý (vượt ngưỡng, `/tomtat`, khi khởi động, hoặc 00:01 hôm sau).
  Nén tự động khi vượt ngưỡng giữ lại 30 tin gần nhất cho lần sau; muốn xử lý ngay mọi tin, dùng `/tomtat`.
- Hỏi đáp chỉ đọc được nguyên văn các tin chưa xử lý; tin đã xử lý chỉ còn qua bản tóm tắt.
