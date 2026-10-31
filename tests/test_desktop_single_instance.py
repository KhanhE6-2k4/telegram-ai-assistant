import threading

from telegram_bot.single_instance import QUIT, SHOW, InstanceServer, send_command


def test_second_instance_sends_show_and_quit(tmp_path):
    port_file = tmp_path / "Data" / "instance.port"
    assert not send_command(port_file, SHOW)  # chưa có bản nào chạy

    received, done = [], threading.Event()

    def handler(name):
        def run():
            received.append(name)
            done.set()
        return run

    server = InstanceServer(port_file, {SHOW: handler(SHOW), QUIT: handler(QUIT)})
    server.start()
    try:
        assert send_command(port_file, SHOW)
        assert done.wait(3)
        done.clear()
        assert send_command(port_file, QUIT)
        assert done.wait(3)
        assert received == [SHOW, QUIT]
        assert not send_command(port_file, "khac")  # lệnh lạ bị từ chối

        # Sai mã bí mật (ví dụ tệp cổng của lần chạy trước) thì không làm gì
        port = port_file.read_text().split()[0]
        port_file.write_text(f"{port} sai-ma")
        assert not send_command(port_file, SHOW)
        assert received == [SHOW, QUIT]
    finally:
        server.close()
    assert not port_file.exists()
    assert not send_command(port_file, SHOW)
