"""Chỉ cho chạy một bản app.

Bản đầu tiên mở socket ở 127.0.0.1 (cổng ngẫu nhiên), ghi cổng và một mã bí mật vào `instance.port`.
Bản chạy sau gửi lệnh cho bản đầu rồi thoát:
- "show": hiện cửa sổ (người dùng mở app lần nữa);
- "quit": thoát hẳn (hook gỡ cài đặt cần app đã tắt).
"""
import logging
import secrets
import socket
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

SHOW = "show"
QUIT = "quit"


def send_command(port_file: Path, command: str, timeout: float = 3) -> bool:
    """Gửi lệnh cho bản đang chạy. False nếu không có bản nào đang chạy (hoặc không trả lời)."""
    try:
        port, token = Path(port_file).read_text(encoding="utf-8").split()
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as conn:
            conn.sendall(f"{token} {command}\n".encode())
            return conn.makefile("r", encoding="utf-8").readline().strip() == "ok"
    except (OSError, ValueError):
        return False


class InstanceServer:
    """Nhận lệnh từ các bản chạy sau. Handler được gọi trên thread của server."""

    def __init__(self, port_file: Path, handlers: dict[str, Callable[[], None]]):
        self.port_file = Path(port_file)
        self.handlers = handlers
        self.token = secrets.token_hex(16)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        self.port_file.write_text(f"{self._sock.getsockname()[1]} {self.token}", encoding="utf-8")
        self._thread = threading.Thread(target=self._serve, name="single-instance", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        try:
            self.port_file.unlink()
        except OSError:
            pass

    def _serve(self) -> None:
        sock = self._sock
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:  # socket đã đóng
                return
            with conn:
                try:
                    conn.settimeout(3)
                    token, command = conn.makefile("r", encoding="utf-8").readline().split()
                    handler = self.handlers.get(command) if token == self.token else None
                    conn.sendall(b"ok\n" if handler else b"error\n")
                except (OSError, ValueError):
                    continue
            if handler:
                try:
                    handler()
                except Exception:
                    log.exception("Lỗi khi xử lý lệnh %s", command)
