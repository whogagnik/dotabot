from pathlib import Path
import ctypes
from typing import Tuple
import os

BASE_DIR = Path(__file__).resolve().parents[2]


DEFAULT_MAFILES_DIR = Path("mafiles")
DEFAULT_ML_MINIMAP_DIR = Path("config/minimap.pt")
DEFAULT_ML_HP_DIR = Path("config/hp.pt")
DEFAULT_LANDMARKS_DIR = Path("config/minimap_landmarks.json")
DEFAULT_HERO_BUILDS_DIR = Path("config/hero_builds.json")

OUT_FILE_PARSER_ITEMS = "config/items_parsed.json"
CATBOOST_DATASET_CSV_PATH = "runs/catboost_dataset.csv"

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

OUT_FILE_HERO_BUILDS = "config/hero_builds.json"

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
LEVEL_ROI: Tuple[int, int, int, int] = (228, 467, 236, 475)


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

user32 = ctypes.windll.user32
TILE_MODE = "grid"
TILE_COLUMNS = 3
TILE_GAP = 8
TILE_BOTTOM_HEIGHT = 420
GRID_WRAP_AT = 1920
FIND_LOGIN_WINDOW_TIMEOUT_SEC = 30.0
FIND_DOTA_WINDOW_TIMEOUT_SEC = 60
MM_PARTY_INVITE_TIMEOUT_SEC = 20.0
POLL_SECONDS = 10
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
