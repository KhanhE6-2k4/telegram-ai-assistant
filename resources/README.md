Logo của app: `icon.ico` (gồm các cỡ 16, 32, 48, 64, 256 px), khai báo bằng dòng `icon = "resources/icon"`
trong `pyproject.toml`. Đổi logo thì thay tệp này rồi chạy lại `briefcase create windows` (hoặc
`briefcase update windows --update-resources`), sau đó build và package lại.

Đã cài bản cũ mà shortcut vẫn hiện logo cũ (ví dụ con ong mặc định của Briefcase) là do Windows lưu đệm
icon: chạy `ie4uinit.exe -show` hoặc khởi động lại Explorer. Ô tìm kiếm cạnh nút Start có bộ đệm riêng
(`%LOCALAPPDATA%\Packages\Microsoft.Windows.Search_cw5n1h2txyewy\LocalState\AppIconCache\100\`, tệp mang
tên đường dẫn tới `Telegram Bot.exe`, ảnh BMP 96×96 32 bit). Windows chỉ ghi tệp này khi cài app: chỉ xoá
tệp thì ô tìm kiếm hiện app không có logo. Cách sửa: gỡ rồi cài lại MSI, hoặc ghi đè tệp bằng logo mới
(giữ header 150 byte của một tệp cùng thư mục, điểm ảnh BGRA từ dưới lên) rồi tắt tiến trình `SearchApp`.
