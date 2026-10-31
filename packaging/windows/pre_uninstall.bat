@echo off
rem Bộ cài MSI gọi trước khi gỡ app. Tệp này được cài vào <thư mục app>\_installer\,
rem tệp chạy của app nằm ở thư mục cha. App ở chế độ --uninstall-cleanup: báo bản đang chạy thoát,
rem xoá khoá tự khởi động, rồi cố gắng hỏi có xoá dữ liệu không.
set "APP_EXE=%~dp0..\Telegram Bot.exe"
if not exist "%APP_EXE%" set "APP_EXE=%~dp0Telegram Bot.exe"
if not exist "%APP_EXE%" exit /b 0
start "" /wait "%APP_EXE%" --uninstall-cleanup
exit /b 0
