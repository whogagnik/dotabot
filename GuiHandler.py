import tkinter as tk
import sys, logging
class GuiHandler(logging.Handler):
    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self.text = text_widget
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
            self.text.after(0, self._append_line, msg)
        except Exception:
            try:
                sys.stderr.write(self.format(record) + "\n")
            except Exception:
                pass

    def _append_line(self, msg: str):
        try:
            self.text.configure(state="normal")
            self.text.insert(tk.END, msg + "\n")
            self.text.see(tk.END)
            self.text.configure(state="disabled")
        except Exception:
            try:
                sys.stderr.write(msg + "\n")
            except Exception:
                pass