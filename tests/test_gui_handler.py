import logging
import unittest

from scripts.host.app.gui_handler import GuiHandler


class FakeTextWidget:
    def __init__(self):
        self.after_calls = []
        self.configure_calls = []
        self.inserts = []
        self.deleted = []

    def after(self, delay_ms, callback):
        self.after_calls.append((delay_ms, callback))

    def configure(self, **kwargs):
        self.configure_calls.append(kwargs)

    def insert(self, where, text):
        self.inserts.append((where, text))

    def see(self, where):
        return None

    def index(self, where):
        return "2.0"

    def delete(self, start, end):
        self.deleted.append((start, end))


class GuiHandlerTests(unittest.TestCase):
    def test_emit_only_queues_and_does_not_schedule_tk_from_caller(self):
        widget = FakeTextWidget()
        handler = GuiHandler(widget, flush_interval_ms=100, max_batch=10)
        initial_after_count = len(widget.after_calls)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )

        handler.emit(record)

        self.assertEqual(len(widget.after_calls), initial_after_count)

        _, callback = widget.after_calls.pop(0)
        callback()

        self.assertEqual(widget.inserts, [("end", "hello\n")])
        self.assertEqual(len(widget.after_calls), initial_after_count)


if __name__ == "__main__":
    unittest.main()
