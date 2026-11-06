"""Đo thời gian từng đoạn xử lý và ghi vào log, để biết chỗ nào làm bot trả lời chậm.

Mọi dòng đo thời gian bắt đầu bằng "Thời gian - " để dễ lọc (ví dụ `findstr "Thời gian" bot.log`).
Không ghi nội dung tin nhắn, câu hỏi hay câu trả lời.
"""
import logging
import time
from contextlib import contextmanager

PREFIX = "Thời gian - "


@contextmanager
def timed(logger: logging.Logger, what: str, *args):
    """Ghi "Thời gian - <what>: 1.23 s" khi đoạn code bên trong kết thúc; lỗi thì thêm "(lỗi)".
    `what` / `args` theo kiểu %-format của logging."""
    start = time.perf_counter()
    try:
        yield
    except BaseException:
        logger.info(PREFIX + what + ": %.2f s (lỗi)", *args, time.perf_counter() - start)
        raise
    logger.info(PREFIX + what + ": %.2f s", *args, time.perf_counter() - start)


def log_elapsed(logger: logging.Logger, start: float, what: str, *args) -> None:
    """Ghi thời gian từ `start` (time.perf_counter()) tới bây giờ, khi không tiện dùng `timed`."""
    logger.info(PREFIX + what + ": %.2f s", *args, time.perf_counter() - start)
