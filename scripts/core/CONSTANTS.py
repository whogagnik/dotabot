from pathlib import Path
import ctypes
from typing import Tuple
SANDBOXIE_START_EXE = r"C:\Program Files\Sandboxie-Plus\Start.exe"
DEFAULT_MAFILES_DIR = Path("mafiles")
DEFAULT_ML_MINIMAP_DIR = Path("config/minimap.pt")
DEFAULT_ML_HP_DIR = Path("config/hp.pt")
DEFAULT_LANDMARKS_DIR = Path("config/minimap_landmarks.json")



HP_ROI: Tuple[int, int, int, int] = (375, 453, 440, 460)   # x1, y1, x2, y2
GOLD_ROI: Tuple[int, int, int, int] = (5, 460, 32, 471)
LEVEL_ROI: Tuple[int, int, int, int] = (360, 500, 390, 530)
TIME_ROI:  Tuple[int, int, int, int] = (880, 15, 1020, 45)


#HOTKEYS

PAUSE_BRAINS = 0x50


user32 = ctypes.windll.user32
HAS_WIN32JOB = True
TILE_MODE = "grid"; TILE_COLUMNS = 3; TILE_GAP = 8
TILE_BOTTOM_HEIGHT = 420; TILE_MIN_WIDTH = 640; TILE_MIN_HEIGHT = 380
GRID_WRAP_AT = 1920

WAIT_STEAM_PROC_ATTEMPTS = 180
WAIT_STEAM_PROC_INTERVAL = 0.5
WAIT_LOGIN_WIN_TIMEOUT = 120

QR_TIMEOUT_SEC = 30
POLL_SECONDS = 10

LOGIN_GONE_GRACE_SEC = 8
MAX_LAUNCH_RETRIES = 3
RELAUNCH_DELAY_SEC = 7
MAX_SCAN_RETRIES = 2

STARTUP_SYNC_TIMEOUT = 300
DELAY_BEFORE_SCANNING_ALL_READY = 5



# --- CPU calm thresholds ---
CPU_CALM_THRESHOLD = 45         # % системной загрузки, ниже которого считаем «спокойно»
CPU_CALM_STABLE_SEC = 2        # секунд подряд ниже порога
CPU_CALM_TIMEOUT = 240          # максимум ожидать, сек

BOX_CALM_THRESHOLD = 8          # % на бокс (steam.exe+steamwebhelper.exe суммарно)
BOX_CALM_STABLE_SEC = 2         # секунд подряд на каждом боксе
APP_ID_DOTA = 570
DOTA_LAUNCH_OPTS = [
    "-novid",
    "-sw",
    "-480",
    "+fps_max", "30",
    "-threads", "1",
    "-nosound",
    "+engine_no_focus_sleep", "1",
    "-nobigpicture",
    "-noreactlogin",
    "-silent",

]

heroes = [x for x in range(0,126)]

STATUS_LABELS = {
    "idle":"Ожидание","launching":"Запуск…","ready":"Окно готово","scanning":"Сканирую QR",
    "success":"Логин ок","ingame":"Dota в игре","gc_ready":"GC готов","queueing":"Поиск игры",
    "error":"Ошибка","skipped":"Пропущен","stopping":"Остановка…",

    # игровые состояния (без STATE)
    "waiting": "Ожидание",
    "playing": "Dota в игре",
    "start_buy": "Стартовый закуп",
    "did_not_run_mid": "Не побежал мид",
}

STATUS_COLORS = {
    "idle":"#737373","launching":"#60a5fa","ready":"#22d3ee","scanning":"#fbbf24",
    "success":"#22c55e","ingame":"#10b981","gc_ready":"#34d399","queueing":"#eab308",
    "error":"#ef4444","skipped":"#a78bfa","stopping":"#f59e0b",

    # игровые состояния (без STATE)
    "waiting": "#60a5fa",        # как launching
    "playing": "#10b981",        # как ingame
    "start_buy": "#fbbf24",      # как scanning
    "did_not_run_mid": "#ef4444" # как error
}


# ====== Локальные константы для JobObject (pywin32 может их не экспортировать) ======
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE   = 0x0001
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x0004
# JOBOBJECTINFOCLASS: JobObjectCpuRateControlInformation
JOBINFOCLASS_CPU_RATE = 15