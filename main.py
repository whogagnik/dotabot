# -*- coding: utf-8 -*-
import sys, time, logging, threading, uuid

from typing import Optional, List, Dict

import psutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from CONSTANTS import *
# win32
import win32gui, win32process
from GuiHandler import GuiHandler
from windowPlacer import WindowPlacer
from controller import Controller
from threadRegistry import ThreadRegistry


def make_logger(gui_text: tk.Text) -> logging.Logger:
    logger = logging.getLogger("SteamManager")
    logger.setLevel(logging.INFO)

    # снять старые хендлеры, если пересоздаём
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    gh = GuiHandler(gui_text)
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
    for noisy in ("urllib3","requests","PIL","pyzbar","steam"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, lvl))


def _window_pid(hwnd: int) -> Optional[int]:
    try: return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception: return None

def _find_main_window_for_pid(pid: int) -> Optional[int]:
    result = None
    def cb(hwnd,_):
        nonlocal result
        if result is not None or not win32gui.IsWindowVisible(hwnd): return
        if _window_pid(hwnd)==pid: result = hwnd
    win32gui.EnumWindows(cb, None); return result



# ===================== GUI =====================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Steam Account Manager")
        self.geometry("1200x700")
        self.minsize(900, 560)
        self.configure(bg="#2c2f33")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # --- Глобальная сетка: нижняя панель всегда видима ---
        self.grid_rowconfigure(2, weight=1)  # mid растягивается
        self.grid_columnconfigure(0, weight=1)

        # ===== ЛОГ =====
        self.log_text = tk.Text(self, height=10, bg="#1e2124", fg="#d6d6d6",
                                insertbackground="#ffffff", font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        self.log_text.configure(state="disabled")
        self.logger = make_logger(self.log_text)

        # реестр потоков
        self.thread_reg = ThreadRegistry()

        # ===== ВЕРХНЯЯ ПАНЕЛЬ =====
        top = tk.Frame(self, bg="#2c2f33")
        top.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        top.grid_columnconfigure(1, weight=1)

        tk.Label(top,text="Steam.exe:",bg="#2c2f33",fg="#ffffff").grid(row=0,column=0,sticky="w")
        self.steam_path_var=tk.StringVar(value=r"C:\Program Files (x86)\Steam\steam.exe")
        tk.Entry(top,textvariable=self.steam_path_var,width=64).grid(row=0,column=1,padx=6,sticky="ew")
        tk.Button(top,text="Найти…",command=self.pick_steam_exe,bg="#7289da",fg="white",relief="flat",width=10).grid(row=0,column=2,padx=6)

        tk.Label(top,text="mafiles:",bg="#2c2f33",fg="#ffffff").grid(row=1,column=0,sticky="w")
        self.ma_dir_var=tk.StringVar(value=str(DEFAULT_MAFILES_DIR.resolve()))
        tk.Entry(top,textvariable=self.ma_dir_var,width=64).grid(row=1,column=1,padx=6,sticky="ew")
        tk.Button(top,text="Папка…",command=self.on_pick_ma_folder,bg="#43b581",fg="white",relief="flat",width=10).grid(row=1,column=2,padx=6)

        tk.Button(top,text="Загрузить accounts.txt",command=self.on_load_txt,bg="#7289da",fg="white",relief="flat",width=22).grid(row=0,column=3,padx=6)
        tk.Button(top,text="Сканировать mafiles",command=self.scan_mafiles,bg="#43b581",fg="white",relief="flat",width=22).grid(row=1,column=3,padx=6)

        tk.Label(top,text="Параллельно:",bg="#2c2f33",fg="#ffffff").grid(row=2,column=0,sticky="w")
        self.parallel_var=tk.IntVar(value=1)
        tk.Spinbox(top,from_=1,to=12,textvariable=self.parallel_var,width=6).grid(row=2,column=1,sticky="w",padx=6)

        tk.Label(top,text="Раскладка:",bg="#2c2f33",fg="#ffffff").grid(row=2,column=2,sticky="e")
        self.layout_var=tk.StringVar(value=TILE_MODE)
        cb=ttk.Combobox(top,textvariable=self.layout_var,values=["grid","right","bottom"],width=10,state="readonly")
        cb.grid(row=2,column=3,sticky="w")

        tk.Label(top,text="Логгер:",bg="#2c2f33",fg="#ffffff").grid(row=2,column=4,sticky="e",padx=(12,0))
        self.loglevel_var=tk.StringVar(value="INFO")
        ll=ttk.Combobox(top,textvariable=self.loglevel_var,
                        values=["DEBUG","INFO","WARNING","ERROR","CRITICAL"],
                        width=10,state="readonly")
        ll.grid(row=2,column=5,sticky="w"); ll.bind("<<ComboboxSelected>>", self.on_change_loglevel)

        tk.Label(top,text="CPU лимит Dota %:",bg="#2c2f33",fg="#ffffff").grid(row=2,column=6,sticky="e",padx=(12,0))
        self.cpu_limit_var=tk.IntVar(value=5)
        tk.Spinbox(top,from_=1,to=100,textvariable=self.cpu_limit_var,width=6).grid(row=2,column=7,sticky="w",padx=6)

        # ===== СЕРЕДИНА: слева аккаунты, справа сессия =====
        mid = tk.Frame(self, bg="#2c2f33")
        mid.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        mid.grid_columnconfigure(0, weight=1)

        # ЛЕВАЯ ТАБЛИЦА
        left=tk.Frame(mid,bg="#2c2f33")
        left.grid(row=0, column=0, sticky="nsew")
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)

        columns=("login","has_ma","status")
        self.tree=ttk.Treeview(left,columns=columns,show="headings",selectmode="none")
        self.tree.heading("login",text="Логин"); self.tree.heading("has_ma",text="maFile"); self.tree.heading("status",text="Статус")
        self.tree.column("login",width=320,anchor="w"); self.tree.column("has_ma",width=80,anchor="center"); self.tree.column("status",width=220,anchor="w")
        self.tree.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        for st,color in STATUS_COLORS.items(): self.tree.tag_configure(f"status-{st}",foreground=color)
        self.tree.bind("<Button-1>", self.on_tree_click)
        vsb=ttk.Scrollbar(left,orient="vertical",command=self.tree.yview); self.tree.configure(yscrollcommand=vsb.set); vsb.pack(side=tk.RIGHT,fill=tk.Y)

        # ПРАВАЯ ПАНЕЛЬ "Текущая сессия"
        right=tk.Frame(mid,bg="#23272a",bd=1,relief="solid")
        right.grid(row=0, column=1, sticky="ns", padx=(8,0))
        tk.Label(right,text="Текущая сессия",bg="#23272a",fg="#ffffff",font=("Segoe UI",11,"bold")).pack(anchor="w",padx=8,pady=(8,6))

        sumfrm = tk.Frame(right, bg="#23272a"); sumfrm.pack(fill=tk.X, padx=8, pady=(0,6))
        self.sum_start  = tk.Label(sumfrm, text="Старт: —",      bg="#23272a", fg="#d6d6d6", anchor="w"); self.sum_start.pack(fill=tk.X, pady=(0,2))
        self.sum_uptime = tk.Label(sumfrm, text="Длительность: 00:00:00", bg="#23272a", fg="#d6d6d6", anchor="w"); self.sum_uptime.pack(fill=tk.X, pady=(0,2))
        self.sum_accounts = tk.Label(sumfrm, text="Аккаунтов: 0", bg="#23272a", fg="#d6d6d6", anchor="w"); self.sum_accounts.pack(fill=tk.X, pady=(0,2))
        self.sum_syscpu = tk.Label(sumfrm, text="CPU общий: —",  bg="#23272a", fg="#d6d6d6", anchor="w"); self.sum_syscpu.pack(fill=tk.X, pady=(0,2))

        sess_cols = ("acc", "st", "hrs", "cpu_box", "cpu_dota")
        self.session_tree = ttk.Treeview(right, columns=sess_cols, show="headings", height=18)
        self.session_tree.heading("acc", text="Аккаунт")
        self.session_tree.heading("st", text="Статус")
        self.session_tree.heading("hrs", text="Часы (stub)")
        self.session_tree.heading("cpu_box", text="CPU Бокс")
        self.session_tree.heading("cpu_dota", text="CPU Dota")
        self.session_tree.column("acc", width=160, anchor="w")
        self.session_tree.column("st", width=140, anchor="w")
        self.session_tree.column("hrs", width=90, anchor="e")
        self.session_tree.column("cpu_box", width=90, anchor="e")
        self.session_tree.column("cpu_dota", width=90, anchor="e")
        for st, color in STATUS_COLORS.items(): self.session_tree.tag_configure(f"status-{st}", foreground=color)
        self.session_tree.pack(fill=tk.Y, padx=8, pady=(0,8))

        tk.Button(right, text="Обновить часы (stub)", command=self.on_update_hours_stub,
                  bg="#5865f2", fg="white", relief="flat").pack(fill=tk.X, padx=8, pady=(0,8))

        # ===== НИЖНЯЯ ПАНЕЛЬ (прибита) =====
        bottom=tk.Frame(self,bg="#2c2f33")
        bottom.grid(row=3, column=0, sticky="ew", padx=10, pady=(4,10))
        bottom.grid_columnconfigure(2, weight=1)

        tk.Button(bottom,text="Выбрать все",command=self.select_all,bg="#99aab5",fg="#2c2f33",relief="flat",width=16).grid(row=0,column=0,padx=4,sticky="w")
        tk.Button(bottom,text="Снять выбор",command=self.clear_selection,bg="#99aab5",fg="#2c2f33",relief="flat",width=16).grid(row=0,column=1,padx=4,sticky="w")
        tk.Button(bottom,text="Удалить выбранные",command=self.delete_selected,bg="#f04747",fg="white",relief="flat",width=18).grid(row=0,column=2,padx=4,sticky="w")

        self.start_btn=tk.Button(bottom,text="Старт фарма",command=self.start_farm,bg="#5865f2",fg="white",relief="flat",width=18)
        self.stop_btn =tk.Button(bottom,text="Стоп фарма",command=self.stop_farm ,bg="#f04747",fg="white",relief="flat",width=18)
        self.start_btn.grid(row=0,column=3,padx=4,sticky="e")
        self.stop_btn.grid(row=0,column=4,padx=4,sticky="e")

        # ===== Стили =====
        style=ttk.Style(self)
        style.configure("Treeview",background="#23272a",foreground="#ffffff",fieldbackground="#23272a",rowheight=26)
        style.configure("Treeview.Heading",background="#99aab5",foreground="#2c2f33",font=("Segoe UI",10,"bold"))

        # ===== Контроллер / данные =====
        self.placer=WindowPlacer(mode=TILE_MODE,columns=TILE_COLUMNS,gap=TILE_GAP,bottom_h=TILE_BOTTOM_HEIGHT,wrap_at=GRID_WRAP_AT)
        self.controller=Controller(self.logger,self.placer,self.set_status, thread_registry=self.thread_reg)

        self._selected: Dict[str,bool] = {a.username:False for a in self.controller.accounts}
        self._row_status: Dict[str,str] = {}

        # «заморозка» правой панели на старте
        self._session_frozen: bool = False
        self._session_snapshot_names: List[str] = []

        self.refresh_table()
        self.after(1000, self._tick_session_panel)

    # ===== UI helpers =====
    def on_change_loglevel(self,_=None): set_logger_level(self.logger,self.loglevel_var.get())

    def _get_acc(self, username:str):
        for a in self.controller.accounts:
            if a.username==username: return a
        return None

    def _has_ma(self, username:str)->bool:
        a = self._get_acc(username)
        return bool(a and a.mafile_data)

    def _current_selected_names(self)->List[str]:
        return [u for u,sel in self._selected.items() if sel]

    def set_status(self, username:str, status:str):
        if status not in STATUS_LABELS: status="idle"

        def _apply_left():
            self._row_status[username]=status
            if self.tree.exists(username):
                vals=list(self.tree.item(username,"values"))
                if len(vals)>=3:
                    vals[2]=STATUS_LABELS[status]
                else:
                    vals=[f"{'✓' if self._selected.get(username) else '□'}  {username}",
                          "✔" if self._has_ma(username) else "✖", STATUS_LABELS[status]]
                tags=[t for t in (self.tree.item(username,"tags") or []) if not t.startswith("status-")]
                tags.append(f"status-{status}")
                self.tree.item(username, values=tuple(vals), tags=tuple(tags))

        def _apply_right():
            if self.session_tree.exists(username):
                lab = STATUS_LABELS.get(status, status)
                self.session_tree.set(username, "st", lab)
                self.session_tree.item(username, tags=(f"status-{status}",))

        self.after(0,_apply_left)
        self.after(0,_apply_right)

    # ===== ЛЕВАЯ ТАБЛИЦА =====
    def refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for a in self.controller.accounts:
            sel=self._selected.get(a.username,False)
            mark="✓" if sel else "□"
            has_ma="✔" if a.mafile_data else "✖"
            status=self._row_status.get(a.username, a.status)
            status=status if status in STATUS_LABELS else "idle"
            tag=f"status-{status}"
            self.tree.insert("",tk.END,iid=a.username,
                             values=(f"{mark}  {a.username}", has_ma, STATUS_LABELS[status]),
                             tags=(tag,))
        if not self._session_frozen:
            self._rebuild_session_tree_from_selection()

    def on_tree_click(self, event):
        row_id=self.tree.identify_row(event.y)
        if not row_id: return
        self._selected[row_id]=not self._selected.get(row_id,False)
        self.refresh_table(); return "break"

    def select_all(self):
        for a in self.controller.accounts: self._selected[a.username]=True
        self.refresh_table()

    def clear_selection(self):
        for a in self.controller.accounts: self._selected[a.username]=False
        self.refresh_table()

    def delete_selected(self):
        names=[u for u,sel in self._selected.items() if sel]
        if not names: messagebox.showinfo("Удаление","Ничего не выбрано."); return
        if not messagebox.askyesno("Удалить",f"Удалить {len(names)} аккаунт(ов)?"): return
        self.controller.remove_accounts(names)
        for n in names:
            self._selected.pop(n,None)
            self._row_status.pop(n,None)
        self.refresh_table(); self.controller.save_state()

    # ===== ПРАВАЯ ПАНЕЛЬ =====
    def _rebuild_session_tree_from_selection(self):
        self.session_tree.delete(*self.session_tree.get_children())
        for u in self._current_selected_names():
            st = self._row_status.get(u, "idle")
            self.session_tree.insert("", tk.END, iid=u,
                values=(u, STATUS_LABELS.get(st,"Ожидание"), "0.00", "0.0%", "0.0%"),
                tags=(f"status-{st}",))

    def _rebuild_session_tree_from_snapshot(self):
        self.session_tree.delete(*self.session_tree.get_children())
        for u in self._session_snapshot_names:
            st = self._row_status.get(u, "idle")
            self.session_tree.insert("", tk.END, iid=u,
                values=(u, STATUS_LABELS.get(st,"Ожидание"), "0.00", "0.0%", "0.0%"),
                tags=(f"status-{st}",))

    # ===== СТАРТ / СТОП =====
    def start_farm(self):
        names = self._current_selected_names()
        accounts = self.controller.selected_accounts(names)
        if not accounts:
            messagebox.showwarning("Нет выбора","Отметьте хотя бы один аккаунт")
            return

        self._session_frozen = True
        self._session_snapshot_names = list(names)
        self._rebuild_session_tree_from_snapshot()

        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self.controller.steam_path=self.steam_path_var.get().strip()
        self.controller.placer.mode=self.layout_var.get().strip() or "grid"
        self.controller.cpu_limit_percent=int(self.cpu_limit_var.get())

        app_id=APP_ID_DOTA
        max_parallel=max(1,int(self.parallel_var.get()))

        name = f"start_farm-{uuid.uuid4().hex}"
        def bg(_):
            try:
                self.controller.start_farming(self.controller.steam_path, app_id, accounts, max_parallel)
            finally:
                self.thread_reg.remove(name, join=False, signal_stop=False)
        self.thread_reg.add(name, bg)

    def stop_farm(self):
        self.stop_btn.config(state="disabled")
        def after_stop(_):
            self.controller.stop_farming()
            time.sleep(2.0)
            self._session_frozen = False
            self._session_snapshot_names = []
            self._rebuild_session_tree_from_selection()
            self.start_btn.config(state="normal"); self.stop_btn.config(state="normal")
        self.thread_reg.add(f"after_stop-{uuid.uuid4().hex}", after_stop)

    # ===== Панель сессии — обновление =====
    def _tick_session_panel(self):
        try:
            started=self.controller.session_started_at
            if started:
                dur=int(time.time()-started)
                h=dur//3600; m=(dur%3600)//60; s=dur%60
                self.sum_start.config(text=f"Старт: {time.strftime('%H:%M:%S', time.localtime(started))}")
                self.sum_uptime.config(text=f"Длительность: {h:02d}:{m:02d}:{s:02d}")
                self.sum_syscpu.config(text=f"CPU общий: {psutil.cpu_percent(interval=0.0):.0f}%")
            else:
                self.sum_start.config(text="Старт: —")
                self.sum_uptime.config(text="Длительность: 00:00:00")
                self.sum_syscpu.config(text="CPU общий: —")

            names = list(self._session_snapshot_names) if self._session_frozen else self._current_selected_names()
            self.sum_accounts.config(text=f"Аккаунтов: {len(names)}")

            # синхронизируем строки
            existing = set(self.session_tree.get_children())
            for u in names:
                if not self.session_tree.exists(u):
                    st = self._row_status.get(u, "idle")
                    self.session_tree.insert("", tk.END, iid=u,
                        values=(u, STATUS_LABELS.get(st,"Ожидание"), "0.00", "0.0%", "0.0%"),
                        tags=(f"status-{st}",))
            for row in list(existing):
                if row not in names:
                    self.session_tree.delete(row)

            # обновляем цифры
            hours_map = self.controller.mm_hours_stub(self.controller.selected_accounts(names))
            for u in names:
                if not self.session_tree.exists(u): continue
                hrs = hours_map.get(u, 0.0)
                self.session_tree.set(u, "hrs", f"{hrs:.2f}")
                box_cpu = self.controller.per_box_cpu.get(u, 0.0)
                self.session_tree.set(u, "cpu_box", f"{box_cpu:.1f}%")
                dota_cpu = self.controller.per_proc_cpu.get(u, 0.0)
                self.session_tree.set(u, "cpu_dota", f"{dota_cpu:.1f}%")

        except Exception:
            pass
        finally:
            self.after(1000, self._tick_session_panel)

    def scan_mafiles(self):
        """Сканирует указанную папку с .maFile/.json и привязывает maFiles к аккаунтам."""
        folder = self.ma_dir_var.get().strip()

        if not folder:
            folder = str(DEFAULT_MAFILES_DIR.resolve())
            self.ma_dir_var.set(folder)
            Path(folder).mkdir(parents=True, exist_ok=True)
            self.controller.build_mafile_index(folder)
            self.controller.match_mafiles_to_accounts()
            self.refresh_table()
    # ===== Разное =====
    def on_update_hours_stub(self):
        names = list(self._session_snapshot_names) if self._session_frozen else self._current_selected_names()
        accounts=self.controller.selected_accounts(names) if names else self.controller.accounts
        res=self.controller.mm_hours_stub(accounts)
        lines=["Часы в ММ (stub):"]
        for user,hours in res.items():
            lines.append(f"• {user}: {hours:.2f} ч / 100")
        messagebox.showinfo("MM-часы (stub)","\n".join(lines))

    def pick_steam_exe(self):
        path=filedialog.askopenfilename(title="Укажите steam.exe", filetypes=[("Steam","steam.exe")])
        if path: self.steam_path_var.set(path)

    def on_pick_ma_folder(self):
        folder=filedialog.askdirectory(title="Папка с .maFile/.json")
        if not folder: return
        self.ma_dir_var.set(folder); self.controller.build_mafile_index(folder); self.controller.match_mafiles_to_accounts(); self.refresh_table()

    def on_load_txt(self):
        path=filedialog.askopenfilename(title="Выберите accounts.txt",filetypes=[("TXT files","*.txt")])
        if not path: return
        replace=messagebox.askyesno("Режим загрузки","Заменить текущий список?\n\nДА — заменить\nНЕТ — добавить")
        self.controller.load_accounts_from_txt(path, append=not replace)
        self._selected={a.username:False for a in self.controller.accounts}; self.refresh_table()

    def on_close(self):
        self.controller.stop_farming()
        #self.thread_reg.exit()
        self.controller.save_state()
        self.destroy()


if __name__=="__main__":
    try: DEFAULT_MAFILES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception: pass
    app=App(); app.mainloop()
