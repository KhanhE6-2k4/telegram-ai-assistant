"""Đăng nhập userbot (tài khoản Telegram của chủ bot) một lần, in ra session string.

    python -m bot.userbot_login

Cần TELEGRAM_API_ID, TELEGRAM_API_HASH (lấy ở https://my.telegram.org, mục API development tools), đọc
từ .env hoặc nhập khi được hỏi. Lệnh hỏi số điện thoại, mã OTP Telegram gửi về, và mật khẩu xác minh 2
bước (nếu có). Session string in ra chỉ một lần: dán vào .env (USERBOT_SESSION=...), không gửi cho ai.
"""
import os
import sys

from .config import load_env_file

WARNING = """
================================ CẢNH BÁO ================================
Session string dưới đây cho TOÀN QUYỀN vào tài khoản Telegram của bạn (đọc, gửi
tin, đổi cài đặt...) mà không cần mã OTP. Chỉ dán vào .env (USERBOT_SESSION=...),
không gửi cho ai, không commit lên git, không dán vào chat. Lộ session thì vào
Telegram > Cài đặt > Thiết bị và chấm dứt phiên đăng nhập này.
==========================================================================
"""


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    load_env_file()
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    api_id = os.environ.get("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: ").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ").strip()
    if not api_id.isdigit() or not api_hash:
        sys.exit("TELEGRAM_API_ID phải là số, TELEGRAM_API_HASH không được trống")
    # start() hỏi số điện thoại, mã OTP và mật khẩu 2 bước (nếu bật)
    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        me = client.get_me()
        session = client.session.save()
    print(f"\nĐã đăng nhập: {me.first_name or ''} {me.last_name or ''} (user id {me.id})".rstrip())
    print(WARNING)
    print(f"USERBOT_SESSION={session}\n")


if __name__ == "__main__":
    main()
