from pathlib import Path
import ctypes
from typing import Tuple
import os

BASE_DIR = Path(__file__).resolve().parents[2]


SANDBOXIE_START_EXE = r"C:\Program Files\Sandboxie-Plus\Start.exe"
DEFAULT_MAFILES_DIR = Path("mafiles")
DEFAULT_ML_MINIMAP_DIR = Path("config/minimap.pt")
DEFAULT_ML_HP_DIR = Path("config/hp.pt")
DEFAULT_LANDMARKS_DIR = Path("config/minimap_landmarks.json")
DEFAULT_HERO_BUILDS_DIR = Path("config/hero_builds.json")
DEFAULT_HERO_URL_DIR = Path("config/hero_urls.json")
DEFALUT_ID_HERO_TO_NAME = Path("config/id_hero_to_name.json")

OUT_FILE_PARSER_ITEMS = "config/items_parsed.json"
CATBOOST_DATASET_CSV_PATH = "config/catboost_dataset_csv.csv"

os.environ["TCL_LIBRARY"] = (
    r"C:\Users\bajojo\AppData\Local\Programs\Python\Python313\tcl\tcl8.6"
)
os.environ["TK_LIBRARY"] = (
    r"C:\Users\bajojo\AppData\Local\Programs\Python\Python313\tcl\tk8.6"
)


DOTABUFF_REFERER = "https://www.dotabuff.com/"
DOTABUFF_HERO_BUILDS_URL = "https://www.dotabuff.com/heroes/{hero_slug}/builds"
DOTABUFF_HERO_ABILITIES_URL = "https://www.dotabuff.com/heroes/{hero_slug}/abilities"

DEFAULT_ID_HERO_TO_NAME = "config/id_hero_to_name.json"
DEFAULT_OUT = "config/heroes_hero_abilities.json"

OUT_FILE_HERO_BUILDS = BASE_DIR / "config" / "hero_builds.json"

# ---------- HTTP ----------
HTTP_TIMEOUT_SECONDS = 30
HTTP_RETRIES = 4
HTTP_BACKOFF_BASE_SECONDS = 1.5

DEFAULT_HEADERS = {
    "User-Agent": ("dotabot-builds-parser/1.0 " "(requests; contact: you@example.com)"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
}

# ---------- Sources ----------
DOTA2PROTRACKER_BASE_URL = "https://dota2protracker.com"


DOTA2PROTRACKER_HERO_URL = DOTA2PROTRACKER_BASE_URL + "/hero/{hero_slug}"


BUILD_SOURCES = ("dota2protracker", "dotabuff")
DOTA2PROTRACKER_REFERER = DOTA2PROTRACKER_BASE_URL + "/"


# ---------- Parsing ----------
SLEEP_BETWEEN_REQUESTS_SECONDS = 0.5

# Если нужно парсить только топ-N предметов из таблицы item stats / popular items
DEFAULT_MAX_ITEMS = 20

# ---------- CSV / JSON helpers ----------
JSON_INDENT = 2
JSON_ENSURE_ASCII = False

HP_ROI: Tuple[int, int, int, int] = (375, 453, 440, 460)  # x1, y1, x2, y2
GOLD_ROI: Tuple[int, int, int, int] = (5, 460, 32, 471)
LEVEL_ROI: Tuple[int, int, int, int] = (360, 500, 390, 530)
TIME_ROI: Tuple[int, int, int, int] = (880, 15, 1020, 45)

# tower_detectore
TIMEOUT_SEC_TOWER_DETECTOR = 60.0
COLOR_RADIUS_TOWER_DETECTOR = 3

KEY_FOR_CENTER_SCREEN = 0x70  # VK_F1 (по умолчанию)
# (mm) - minimap
MM_W = 100
MM_H = 100
MM_DX = 4  # от правого края внутрь на 4 px
MM_DY = 4  # от нижнего края вверх на 4 px
DEFAULT_THR = 0.9
DEFAULT_NMS = 7

# HOTKEYS

PAUSE_BRAINS = 0x50


user32 = ctypes.windll.user32
HAS_WIN32JOB = True
TILE_MODE = "grid"
TILE_COLUMNS = 3
TILE_GAP = 8
TILE_BOTTOM_HEIGHT = 420
TILE_MIN_WIDTH = 640
TILE_MIN_HEIGHT = 380
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
CPU_CALM_THRESHOLD = 45  # % системной загрузки, ниже которого считаем «спокойно»
CPU_CALM_STABLE_SEC = 2  # секунд подряд ниже порога
CPU_CALM_TIMEOUT = 240  # максимум ожидать, сек

BOX_CALM_THRESHOLD = 8  # % на бокс (steam.exe+steamwebhelper.exe суммарно)
BOX_CALM_STABLE_SEC = 2  # секунд подряд на каждом боксе
APP_ID_DOTA = 570
DOTA_LAUNCH_OPTS = [
    "-novid",
    "-sw",
    "-480",
    "+fps_max",
    "30",
    "-threads",
    "1",
    "-nosound",
    "+engine_no_focus_sleep",
    "1",
    "-nobigpicture",
    "-noreactlogin",
    "-silent",
]

heroes = [x for x in range(0, 126)]

STATUS_LABELS = {
    "idle": "Ожидание",
    "launching": "Запуск…",
    "ready": "Окно готово",
    "scanning": "Сканирую QR",
    "success": "Логин ок",
    "ingame": "Dota в игре",
    "gc_ready": "GC готов",
    "queueing": "Поиск игры",
    "error": "Ошибка",
    "skipped": "Пропущен",
    "stopping": "Остановка…",
    # игровые состояния (без STATE)
    "waiting": "Ожидание",
    "playing": "Dota в игре",
    "start_buy": "Стартовый закуп",
    "did_not_run_mid": "Не побежал мид",
}

STATUS_COLORS = {
    "idle": "#737373",
    "launching": "#60a5fa",
    "ready": "#22d3ee",
    "scanning": "#fbbf24",
    "success": "#22c55e",
    "ingame": "#10b981",
    "gc_ready": "#34d399",
    "queueing": "#eab308",
    "error": "#ef4444",
    "skipped": "#a78bfa",
    "stopping": "#f59e0b",
    # игровые состояния (без STATE)
    "waiting": "#60a5fa",  # как launching
    "playing": "#10b981",  # как ingame
    "start_buy": "#fbbf24",  # как scanning
    "did_not_run_mid": "#ef4444",  # как error
}


# ====== Локальные константы для JobObject (pywin32 может их не экспортировать) ======
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x0001
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x0004
# JOBOBJECTINFOCLASS: JobObjectCpuRateControlInformation
JOBINFOCLASS_CPU_RATE = 15
