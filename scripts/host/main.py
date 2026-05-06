# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import time
import logging
import threading
from pathlib import Path
from typing import Dict, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from scripts.host.app.gui_handler import GuiHandler
from scripts.host.app.controller import Controller
from scripts.host.core.config import DEFAULT_MAFILES_DIR


from scripts.host.app.controller import Controller, set_host_controller


def make_logger(gui_text: tk.Text) -> logging.Logger:
    logger = logging.getLogger("SteamHost")
    logger.setLevel(logging.INFO)

    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    gh = GuiHandler(gui_text, flush_interval_ms=100, max_batch=200)
    gh.setLevel(logging.INFO)
    gh.setFormatter(fmt)
    logger.addHandler(gh)

    logger.propagate = False
    return logger

def set_logger_level(logger: logging.Logger, level_name: str):
    lvl = getattr(logging, level_name.upper(), logging.INFO)
    logger.setLevel(lvl)
    for h in logger.handlers:
        h.setLevel(lvl)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Host Orchestrator")
        self.geometry("1250x760")
        self.minsize(1000, 620)
        self.configure(bg="#2c2f33")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ---------------------------------------------------------
        # log
        # ---------------------------------------------------------
        self.log_text = tk.Text(
            self,
            height=10,
            bg="#1e2124",
            fg="#d6d6d6",
            insertbackground="#ffffff",
            font=("Consolas", 10),
        )
        self.log_text.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        self.log_text.configure(state="disabled")

        self.logger = make_logger(self.log_text)
        self.controller = Controller(self.logger, self.set_status)
        set_host_controller(self.controller)
        global _HOST_CONTROLLER
        _HOST_CONTROLLER = self.controller

        self._selected: Dict[str, bool] = {
            a.username: False for a in self.controller.accounts
        }
        self._row_status: Dict[str, str] = {}

        self._tick_thread: Optional[threading.Thread] = None
        self._tick_stop = threading.Event()

        self._build_top()
        self._build_mid()
        self._build_bottom()

        self.refresh_accounts_table()
        self.after(1000, self._refresh_vm_table_loop)

        # Django server должен подниматься сразу при запуске окна
        self._start_django_server_on_boot()

    # ---------------------------------------------------------
    # UI build
    # ---------------------------------------------------------

    def _build_top(self):
        top = tk.Frame(self, bg="#2c2f33")
        top.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        top.grid_columnconfigure(1, weight=1)

        tk.Label(top, text="mafiles:", bg="#2c2f33", fg="#ffffff").grid(
            row=0, column=0, sticky="w"
        )
        self.ma_dir_var = tk.StringVar(value=str(DEFAULT_MAFILES_DIR.resolve()))
        tk.Entry(top, textvariable=self.ma_dir_var, width=64).grid(
            row=0, column=1, padx=6, sticky="ew"
        )

        tk.Button(
            top,
            text="Папка…",
            command=self.on_pick_ma_folder,
            bg="#43b581",
            fg="white",
            relief="flat",
            width=10,
        ).grid(row=0, column=2, padx=6)

        tk.Button(
            top,
            text="Load accounts.txt",
            command=self.on_load_txt,
            bg="#7289da",
            fg="white",
            relief="flat",
            width=18,
        ).grid(row=0, column=3, padx=6)

        tk.Button(
            top,
            text="Scan mafiles",
            command=self.scan_mafiles,
            bg="#43b581",
            fg="white",
            relief="flat",
            width=16,
        ).grid(row=0, column=4, padx=6)

        tk.Label(top, text="Batch size:", bg="#2c2f33", fg="#ffffff").grid(
            row=1, column=0, sticky="w"
        )
        self.batch_size_var = tk.IntVar(value=self.controller.batch_size)
        tk.Spinbox(
            top,
            from_=1,
            to=10,
            textvariable=self.batch_size_var,
            width=6,
        ).grid(row=1, column=1, sticky="w", padx=6)

        tk.Label(top, text="Log level:", bg="#2c2f33", fg="#ffffff").grid(
            row=1, column=2, sticky="e"
        )
        self.loglevel_var = tk.StringVar(value="INFO")
        ll = ttk.Combobox(
            top,
            textvariable=self.loglevel_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            width=10,
            state="readonly",
        )
        ll.grid(row=1, column=3, sticky="w")
        ll.bind("<<ComboboxSelected>>", self.on_change_loglevel)

    def _build_mid(self):
        mid = tk.Frame(self, bg="#2c2f33")
        mid.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_columnconfigure(1, weight=1)

        # left: accounts
        left = tk.Frame(mid, bg="#2c2f33")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self.acc_tree = ttk.Treeview(
            left,
            columns=("login", "ma", "status"),
            show="headings",
            selectmode="none",
        )
        self.acc_tree.heading("login", text="Login")
        self.acc_tree.heading("ma", text="maFile")
        self.acc_tree.heading("status", text="Status")
        self.acc_tree.column("login", width=320, anchor="w")
        self.acc_tree.column("ma", width=80, anchor="center")
        self.acc_tree.column("status", width=220, anchor="w")
        self.acc_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.acc_tree.bind("<Button-1>", self.on_tree_click)

        vsb_left = ttk.Scrollbar(left, orient="vertical", command=self.acc_tree.yview)
        self.acc_tree.configure(yscrollcommand=vsb_left.set)
        vsb_left.pack(side=tk.RIGHT, fill=tk.Y)

        # right: vm/session
        right = tk.Frame(mid, bg="#23272a", bd=1, relief="solid")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        tk.Label(
            right,
            text="VM / Session",
            bg="#23272a",
            fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(8, 6))

        self.vm_tree = ttk.Treeview(
            right,
            columns=("vm_id", "client_name", "status", "accounts", "login_hwnds", "dota_hwnds", "planner", "queue"),
            show="headings",
        )
        self.vm_tree.heading("vm_id", text="VM")
        self.vm_tree.heading("client_name", text="Client")
        self.vm_tree.heading("status", text="Status")
        self.vm_tree.heading("accounts", text="Accs")
        self.vm_tree.heading("login_hwnds", text="LoginH")
        self.vm_tree.heading("dota_hwnds", text="DotaH")
        self.vm_tree.heading("planner", text="Planner")
        self.vm_tree.heading("queue", text="Queue")

        self.vm_tree.column("vm_id", width=90, anchor="w")
        self.vm_tree.column("status", width=140, anchor="w")
        self.vm_tree.column("accounts", width=60, anchor="center")
        self.vm_tree.column("login_hwnds", width=60, anchor="center")
        self.vm_tree.column("dota_hwnds", width=60, anchor="center")
        self.vm_tree.column("planner", width=70, anchor="center")
        self.vm_tree.column("queue", width=60, anchor="center")

        self.vm_tree.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

    def _build_bottom(self):
        bottom = tk.Frame(self, bg="#2c2f33")
        bottom.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 10))

        tk.Button(
            bottom,
            text="Select all",
            command=self.select_all,
            bg="#99aab5",
            fg="#2c2f33",
            relief="flat",
            width=14,
        ).grid(row=0, column=0, padx=4)

        tk.Button(
            bottom,
            text="Clear",
            command=self.clear_selection,
            bg="#99aab5",
            fg="#2c2f33",
            relief="flat",
            width=14,
        ).grid(row=0, column=1, padx=4)

        tk.Button(
            bottom,
            text="Delete selected",
            command=self.delete_selected,
            bg="#f04747",
            fg="white",
            relief="flat",
            width=16,
        ).grid(row=0, column=2, padx=8)

        self.start_btn = tk.Button(
            bottom,
            text="Start",
            command=self.start_pipeline,
            bg="#5865f2",
            fg="white",
            relief="flat",
            width=18,
        )
        self.stop_btn = tk.Button(
            bottom,
            text="Stop",
            command=self.stop_pipeline,
            bg="#f04747",
            fg="white",
            relief="flat",
            width=18,
        )

        self.start_btn.grid(row=0, column=3, padx=12)
        self.stop_btn.grid(row=0, column=4, padx=4)

    # ---------------------------------------------------------
    # django boot
    # ---------------------------------------------------------

    def _start_django_server_on_boot(self):
        try:
            from scripts.host.core.server_runtime import start_django_server_in_thread

            start_django_server_in_thread(self.logger)
            self.logger.info("Django server launch requested on window startup")
        except Exception as e:
            self.logger.error(f"Failed to start Django server: {e}", exc_info=True)

    # ---------------------------------------------------------
    # actions
    # ---------------------------------------------------------

    def on_change_loglevel(self, _=None):
        set_logger_level(self.logger, self.loglevel_var.get())

    def set_status(self, username: str, status: str):
        def _apply():
            self._row_status[username] = status
            self.refresh_accounts_table()

        self.after(0, _apply)

    def on_load_txt(self):
        path = filedialog.askopenfilename(
            title="Выберите accounts.txt",
            filetypes=[("TXT files", "*.txt")],
        )
        if not path:
            return

        replace = messagebox.askyesno(
            "Режим загрузки",
            "Заменить текущий список?\n\nДА — заменить\nНЕТ — добавить",
        )

        self.controller.load_accounts_from_txt(path, append=not replace)
        self._selected = {a.username: False for a in self.controller.accounts}
        self.refresh_accounts_table()

    def scan_mafiles(self):
        folder = self.ma_dir_var.get().strip()
        if not folder:
            folder = str(DEFAULT_MAFILES_DIR.resolve())
            self.ma_dir_var.set(folder)

        Path(folder).mkdir(parents=True, exist_ok=True)

        self.controller.build_mafile_index(folder)
        self.controller.match_mafiles_to_accounts()
        self.refresh_accounts_table()

    def on_pick_ma_folder(self):
        folder = filedialog.askdirectory(title="Папка с .maFile/.json")
        if not folder:
            return
        self.ma_dir_var.set(folder)
        self.scan_mafiles()

    def start_pipeline(self):
        self.controller.batch_size = int(self.batch_size_var.get())
        self.controller.start()

        if self._tick_thread and self._tick_thread.is_alive():
            self.logger.info("Controller loop already running")
            return

        self._tick_stop.clear()
        tick_error_count = 0
        last_tick_error_log_ts = 0.0

        def loop():
            nonlocal tick_error_count, last_tick_error_log_ts
            while not self._tick_stop.is_set():
                try:

                    self.controller.tick_one()
                    tick_error_count = 0

                except Exception as e:
                    tick_error_count += 1
                    now = time.time()
                    min_interval = min(30.0, max(2.0, 0.25 * tick_error_count))
                    if now - last_tick_error_log_ts >= min_interval:
                        last_tick_error_log_ts = now
                        self.logger.error(
                            "controller.tick_one error: "
                            f"{e}; failures={tick_error_count}",
                            exc_info=True,
                        )
                time.sleep(0.02)

        self._tick_thread = threading.Thread(
            target=loop,
            daemon=True,
            name="host-controller-loop",
        )
        self._tick_thread.start()
        self.logger.info("Pipeline started")

    def stop_pipeline(self):
        self.controller.stop()
        self._tick_stop.set()
        self.logger.info("Pipeline stopped")

    def select_all(self):
        for a in self.controller.accounts:
            self._selected[a.username] = True
        self.refresh_accounts_table()

    def clear_selection(self):
        for a in self.controller.accounts:
            self._selected[a.username] = False
        self.refresh_accounts_table()

    def delete_selected(self):
        names = [u for u, sel in self._selected.items() if sel]
        if not names:
            return

        if not messagebox.askyesno("Удалить", f"Удалить {len(names)} аккаунт(ов)?"):
            return

        self.controller.remove_accounts(names)
        for n in names:
            self._selected.pop(n, None)
            self._row_status.pop(n, None)
        self.refresh_accounts_table()

    # ---------------------------------------------------------
    # tables
    # ---------------------------------------------------------

    def refresh_accounts_table(self):
        self.acc_tree.delete(*self.acc_tree.get_children())
        for a in self.controller.accounts:
            sel = self._selected.get(a.username, False)
            self.acc_tree.insert(
                "",
                tk.END,
                iid=a.username,
                values=(
                    f"{'✓' if sel else '□'}  {a.username}",
                    "✔" if a.mafile_data else "✖",
                    self._row_status.get(a.username, getattr(a, "status", "idle")),
                ),
            )

    def refresh_vm_table(self):
        self.vm_tree.delete(*self.vm_tree.get_children())
        for row in self.controller.get_vm_rows():
            self.vm_tree.insert(
                "",
                tk.END,
                iid=row["vm_id"],
                values=(
                    row["vm_id"],
                    "",
                    row["status"],
                    row["accounts"],
                    row["login_hwnds"],
                    row["dota_hwnds"],
                    "yes" if row["planner"] else "no",
                    row["queue"],
                ),
            )

    def _refresh_vm_table_loop(self):
        try:
            self.refresh_vm_table()
        finally:
            self.after(1000, self._refresh_vm_table_loop)

    def on_tree_click(self, event):
        row_id = self.acc_tree.identify_row(event.y)
        if not row_id:
            return
        self._selected[row_id] = not self._selected.get(row_id, False)
        self.refresh_accounts_table()
        return "break"

    # ---------------------------------------------------------
    # close
    # ---------------------------------------------------------

    def on_close(self):
        self._tick_stop.set()
        self.controller.stop()
        try:
            self.controller.save_state()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    try:
        DEFAULT_MAFILES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    app = App()
    app.mainloop()
