# hud_hp_scanner.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional, Tuple, List, Dict
import os
import time
import json
import shutil

import numpy as np
import cv2
import torch

from uuid import uuid4

# win32 + скриншоты
import win32gui
import pyautogui as p
import easyocr
from scripts.ml.train_hp_seq_all import HudHPSeqNet

"""
Модуль OCR для HUD:
 - чтение голды (EasyOCR)
 - чтение HP в формате current/max через свою нейронку
 + live-граббер: раз в секунду сохраняет ROI и prediction в data/hud/hp_bar и hp_bar.json
"""

# -------------------------
# ROI в ПИКСЕЛЯХ: (x1, y1, x2, y2) относительно клиентской области окна игры
# -------------------------
HP_ROI: Tuple[int, int, int, int] = (375, 453, 440, 460)   # x1, y1, x2, y2
GOLD_ROI: Tuple[int, int, int, int] = (5, 460, 32, 471)
LEVEL_ROI: Tuple[int, int, int, int] = (360, 500, 390, 530)
TIME_ROI:  Tuple[int, int, int, int] = (880, 15, 1020, 45)

# -------------------------
# Папки и LS интеграция
# -------------------------
DUMP_DIR = "../../data/hud"
HP_DIR   = os.path.join(DUMP_DIR, "hp_bar")
TASKS_JSON = os.path.join(DUMP_DIR, "hp_bar.json")
TASKS_SHADOW = os.path.join(DUMP_DIR, "hp_bar.shadow.json")

# Корень проекта для /data/local-files
PROJECT_ROOT = os.environ.get("LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT", os.getcwd())

# -------------------------
# Вспомогательные утилиты (LS и запись JSON)
# -------------------------
def build_ls_task_for_hp(abs_png_path: str, hp_text: str | None,
                         model_version: str = "hud_ocr_v1",
                         score: float | None = None) -> dict:
    """
    Формирует задачу для Label Studio с ПРЕДИКТОМ в поле `predictions`
    под твой темплейт:
      <Image name="image" value="$image"/>
      <TextArea name="hp_text" toName="image"/>
    """
    ls_url = _to_localfiles_url(abs_png_path)
    task = {
        "data": {"image": ls_url},
        "meta": {"kind": "hp_bar", "format": "mask_0_255"}
    }

    hp_text = (hp_text or "").strip()
    if hp_text:
        result = [{
            "id": str(uuid4()),
            "from_name": "hp_text",
            "to_name":   "image",
            "type":      "textarea",
            "value":     {"text": ''},
        }]
        pred = {"result": result, "model_version": model_version}
        if score is not None:
            pred["score"] = float(score)
        task["predictions"] = [pred]
    else:
        task["predictions"] = []

    return task

def _to_unix(p: str) -> str:
    return p.replace("\\", "/")

def _to_localfiles_url(abs_img_path: str) -> str:
    rel = os.path.relpath(abs_img_path, PROJECT_ROOT)
    rel_unix = _to_unix(rel)
    return "/data/local-files/?d=" + rel_unix

def atomic_dump_json(path: str, data, max_retries: int = 20,
                     retry_sleep: float = 0.25,
                     make_shadow: Optional[str] = None):
    folder = os.path.dirname(path) or "."
    base = os.path.basename(path)
    ts = int(time.time() * 1e6)
    tmp_path = os.path.join(folder, f".{base}.{os.getpid()}.{ts}.tmp")

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    last_exc = None
    for _ in range(max_retries):
        try:
            os.replace(tmp_path, path)
            return
        except (PermissionError, OSError) as e:
            last_exc = e
            time.sleep(retry_sleep)

    if make_shadow:
        try:
            shutil.copyfile(tmp_path, make_shadow)
            print(f"[warn] {path} locked; wrote shadow: {make_shadow}")
        except Exception as e:
            print(f"[err] shadow write failed: {e}")
    try:
        os.remove(tmp_path)
    except Exception:
        pass
    if last_exc:
        raise last_exc

def load_or_init_tasks(path: str) -> list:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            return tasks if isinstance(tasks, list) else []
        except Exception:
            return []
    else:
        return []

# -------------------------
# Win32 helpers
# -------------------------
def _win_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""

def _is_main_visible(hwnd: int) -> bool:
    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not win32gui.GetParent(hwnd) and bool(_win_title(hwnd).strip())
    except Exception:
        return False

def _area(hwnd: int) -> int:
    try:
        L, T, R, B = win32gui.GetWindowRect(hwnd)
        return max(0, R - L) * max(0, B - T)
    except Exception:
        return 0

def find_dota_hwnd() -> Optional[int]:
    c = []
    def cb(hwnd, _):
        if not _is_main_visible(hwnd):
            return
        t = _win_title(hwnd)
        if "Dota 2" in t or "Dota" in t:
            c.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not c:
        return None
    c.sort(key=_area, reverse=True)
    return c[0]

def client_rect_screen(hwnd: int) -> Tuple[int, int, int, int]:
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        x, y = win32gui.ClientToScreen(hwnd, (0, 0))
        return x, y, max(1, r - l), max(1, b - t)
    except Exception:
        L, T, R, B = win32gui.GetWindowRect(hwnd)
        return L, T, max(1, R - L), max(1, B - T)

def grab_roi_rgb_from_window(hwnd: int, roi_win: Tuple[int, int, int, int]) -> Optional[np.ndarray]:
    """
    ROI в координатах КЛИЕНТСКОЙ области -> абсолютный экран -> PyAutoGUI screenshot(region).
    Возвращает ROI в RGB (HxWx3).
    """
    cx, cy, cw, ch = client_rect_screen(hwnd)
    x1, y1, x2, y2 = roi_win
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)
    sx = cx + x1
    sy = cy + y1
    try:
        shot = p.screenshot(region=(sx, sy, w, h))  # PIL RGB
        arr = np.array(shot)  # RGB uint8
        return arr
    except Exception as e:
        print("[grab] screenshot failed:", e)
        return None

# ============================================================
# 1. Загрузка HP-модели (как в train_hp_seq_all.py)
# ============================================================
def load_hp_model(
    ckpt_path: str = "runs/hp_seq/best.pt",
    device: Optional[str] = None,
) -> Tuple[HudHPSeqNet, List[str], str, int, int, int]:
    """
    Возвращает: net, vocab, pad_token, img_h, img_w, max_len
    """
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    dev = torch.device(device)

    ckpt = torch.load(ckpt_path, map_location=dev)
    vocab = ckpt.get("vocab") or ckpt.get("alphabet")
    if vocab is None:
        raise RuntimeError("Checkpoint does not contain 'vocab'/'alphabet'")

    pad_token = ckpt.get("pad_token", "<PAD>")
    img_h = ckpt["img_h"]; img_w = ckpt["img_w"]; max_len = ckpt["max_len"]

    net = HudHPSeqNet(
        in_ch=1,
        img_h=img_h,
        img_w=img_w,
        max_len=max_len,
    )
    net.load_state_dict(ckpt["model"])
    net.to(dev).eval()

    return net, vocab, pad_token, img_h, img_w, max_len

# ============================================================
# 2. HudOCR: HP через свою NN, голда через EasyOCR
# ============================================================
class SelfHp:
    """
    OCR для HUD:
      - read_gold(window_rgb) -> Optional[int]  (EasyOCR)
      - read_hp_pair(window_rgb) -> (Optional[int], Optional[int]) (NN)
      - read_hp_ratio(window_rgb) -> Optional[float]
    """

    def __init__(self,
                 hp_ckpt_path: str,
                 device: Optional[str] = None,
                 gpu_easyocr: bool = False):
        # ---- HP модель ----
        self.hp_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.hp_net, self.hp_vocab, self.hp_pad_token, \
            self.hp_img_h, self.hp_img_w, self.hp_max_len = load_hp_model(
                hp_ckpt_path, device=self.hp_device
            )
        self.hp_device = next(self.hp_net.parameters()).device.type
        # словари для декодинга
        self.hp_char2idx: Dict[str, int] = {c: i for i, c in enumerate(self.hp_vocab)}
        self.hp_idx2char: Dict[int, str] = {i: c for c, i in self.hp_char2idx.items()}

        # ---- EasyOCR (голда / уровень / время) ----
        self.easyocr_gpu = gpu_easyocr
        self.easyocr_reader = None
        if easyocr is not None:
            try:
                # Языка "en" для цифр более чем достаточно
                self.easyocr_reader = easyocr.Reader(['en'], gpu=gpu_easyocr,model_storage_directory='~/.EasyOCR/')
            except Exception as e:
                print(f"[HudOCR] EasyOCR init failed: {e}")
                self.easyocr_reader = None
        else:
            print("[HudOCR] easyocr is not installed; gold/level/time OCR disabled")

    # -------------------------
    # Вспомогательные методы
    # -------------------------
    @staticmethod
    def _crop_roi_from_rgb(img_rgb: np.ndarray,
                           roi_px: Tuple[int, int, int, int]) -> np.ndarray:
        if img_rgb is None or img_rgb.size == 0:
            return img_rgb
        H, W = img_rgb.shape[:2]
        x1, y1, x2, y2 = roi_px
        x1 = max(0, min(W - 1, int(x1)))
        y1 = max(0, min(H - 1, int(y1)))
        x2 = max(x1 + 1, min(W,     int(x2)))
        y2 = max(y1 + 1, min(H,     int(y2)))
        return img_rgb[y1:y2, x1:x2].copy()

    def _preprocess_hp_roi_rawmask(self, img_rgb: np.ndarray) -> Optional[np.ndarray]:
        """
        ROI -> апскейл -> "почти белый" -> морфология -> бинарная маска 0/255.
        Если после фильтрации все пиксели чёрные -> вернуть None.
        """
        if img_rgb is None or img_rgb.size == 0:
            return None

        h, w = img_rgb.shape[:2]
        scale = 8
        img_rgb_big = cv2.resize(
            img_rgb,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_CUBIC,
        )

        lower = np.array([210, 210, 210], dtype=np.uint8)
        upper = np.array([255, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(img_rgb_big, lower, upper)

        kernel = np.ones((2, 2), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

        # <<< вот это главное >>>
        if not np.any(mask):  # все пиксели == 0
            return None

        return mask

    def _prepare_hp_tensor(self, roi_rgb: np.ndarray) -> Optional[torch.Tensor]:
        mask = self._preprocess_hp_roi_rawmask(roi_rgb)
        if mask is None: return None
        img_resized = cv2.resize(
            mask,
            (self.hp_img_w, self.hp_img_h),
            interpolation=cv2.INTER_AREA
        )
        img_f = img_resized.astype(np.float32) / 255.0
        x = torch.from_numpy(img_f).unsqueeze(0).unsqueeze(0)  # 1x1xH x W
        return x

    def _decode_hp_ids(self, ids: np.ndarray) -> str:
        chars: List[str] = []
        for i in ids:
            ch = self.hp_idx2char.get(int(i), "")
            if ch == self.hp_pad_token:
                continue
            chars.append(ch)
        return "".join(chars)

    def _infer_hp_text(self, roi_rgb: np.ndarray) -> Optional[str]:
        x = self._prepare_hp_tensor(roi_rgb)
        if x is None: return None
        x = x.to(self.hp_device)
        with torch.no_grad():
            logits = self.hp_net(x)          # 1 x T x C
            pred = logits.argmax(dim=2)      # 1 x T
        ids = pred.squeeze(0).cpu().numpy()  # T
        text = self._decode_hp_ids(ids)
        return text.strip()

    def _prepare_hp_tensor_from_mask(self, mask_u8: np.ndarray) -> torch.Tensor:
        """
        Уже готовая бинарная маска (0/255) -> ресайз (hp_img_w,hp_img_h) -> тензор 1x1xH x W.
        """
        if mask_u8 is None or mask_u8.size == 0:
            mask_u8 = np.zeros((self.hp_img_h, self.hp_img_w), np.uint8)

        img_resized = cv2.resize(
            mask_u8, (self.hp_img_w, self.hp_img_h),
            interpolation=cv2.INTER_AREA
        )
        img_f = img_resized.astype(np.float32) / 255.0
        x = torch.from_numpy(img_f).unsqueeze(0).unsqueeze(0)  # 1x1xH x W
        return x
    def _infer_hp_from_mask(self, mask_u8: np.ndarray) -> Optional[str]:
        """
        Прямой инференс по уже подготовленной маске (0/255).
        Избегаем повторного препроцесса.
        """
        x = self._prepare_hp_tensor_from_mask(mask_u8).to(self.hp_device)
        if not x: return None
        with torch.no_grad():
            logits = self.hp_net(x)  # 1 x T x C
            pred = logits.argmax(dim=2)  # 1 x T
        ids = pred.squeeze(0).cpu().numpy()  # T
        return self._decode_hp_ids(ids).strip()


    # -------------------------
    # EasyOCR helper
    # -------------------------
    def _easyocr_read_roi(
        self,
        window_rgb: np.ndarray,
        roi_px: Tuple[int, int, int, int],
        *,
        min_conf: float = 0.3,
    ) -> Optional[str]:
        """
        Читает текст из ROI с помощью EasyOCR.
        Возвращает строку (наиболее уверенное предсказание) или None.
        """
        if self.easyocr_reader is None:
            return None

        crop = self._crop_roi_from_rgb(window_rgb, roi_px)
        scale = 4

        crop = cv2.resize(crop, (crop.shape[1] * scale,crop.shape[0] * scale),interpolation=cv2.INTER_AREA)

        if crop is None or crop.size == 0:
            return None

        # EasyOCR ожидает RGB / BGR — у нас уже RGB
        try:
            # detail=1 -> [(bbox, text, conf), ...]
            results = self.easyocr_reader.readtext(crop, detail=1, paragraph=False)
        except Exception as e:
            print(f"[HudOCR] EasyOCR read error: {e}")
            return None

        if not results:
            return None

        # Выбираем результат с максимальной confidence
        best_text = None
        best_conf = -1.0
        for (_, text, conf) in results:
            if conf is None:
                conf = 0.0
            if conf >= min_conf and conf > best_conf and text:
                best_text = text
                best_conf = conf

        if best_text is None:
            return None

        return str(best_text).strip()

    # -------------------------
    # Публичные методы
    # -------------------------


    def get_hp(self, window_rgb: np.ndarray) -> Tuple[Optional[int], Optional[int]]:
        crop = self._crop_roi_from_rgb(window_rgb, HP_ROI)
        text = self._infer_hp_text(crop)

        if not text:
            return None, None

        text = text.strip().replace(" ", "").replace("\\", "/")
        cur_hp: Optional[int] = None
        max_hp: Optional[int] = None

        if "/" in text:
            left, right = text.split("/", 1)
            left_digits  = "".join(ch for ch in left  if ch.isdigit())
            right_digits = "".join(ch for ch in right if ch.isdigit())
            if left_digits:
                try: cur_hp = int(left_digits)
                except ValueError: cur_hp = None
            if right_digits:
                try: max_hp = int(right_digits)
                except ValueError: max_hp = None
            return cur_hp, max_hp

        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits or len(digits) < 4:
            return None, None

        best_split = None
        min_diff = 1e18
        for split_idx in range(2, len(digits) - 1):
            left = digits[:split_idx]; right = digits[split_idx:]
            try:
                a, b = int(left), int(right)
            except ValueError:
                continue
            diff = abs(a - b); ratio = diff / max(1, b)
            if ratio < 0.3 and diff < min_diff:
                min_diff = diff
                best_split = (a, b)

        if best_split:
            return best_split[0], best_split[1]
        mid = len(digits) // 2
        try:
            return int(digits[:mid]), int(digits[mid:])
        except ValueError:
            return None, None

    def get_hp_ratio(self, window_rgb: np.ndarray) -> Optional[float]:
        cur, mx = self.get_hp(window_rgb)
        if cur is None or mx is None or mx <= 0:
            return None
        return float(cur) / float(mx)
    def get_gold(self, window_rgb: np.ndarray) -> Optional[int]:
        """
        OCR золота из GOLD_ROI.
        Возвращает целое число или None.
        """
        text = self._easyocr_read_roi(window_rgb, GOLD_ROI)
        if not text:
            return None

        # Оставляем только цифры
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None

        try:
            gold = int(digits)
        except ValueError:
            return None

        # Можно добавить sanity-check, например > 0
        if gold < 0:
            return None
        return gold
    def get_level(self, window_rgb: np.ndarray) -> Optional[int]:
        """
        OCR уровня героя из LEVEL_ROI.
        Возвращает int 1..30 или None.
        """
        text = self._easyocr_read_roi(window_rgb, LEVEL_ROI)
        if not text:
            return None

        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None

        try:
            lvl = int(digits)
        except ValueError:
            return None

        # Уровень Dota героя лежит примерно в 1..30
        if not (1 <= lvl <= 30):
            return None

        return lvl
    def get_time(self, window_rgb: np.ndarray) -> Optional[int]:
        """
        OCR игрового таймера из TIME_ROI.
        Ожидаемый формат: 'MM:SS' (возможно с артефактами).
        Возвращает время в СЕКУНДАХ или None.
        """
        text = self._easyocr_read_roi(window_rgb, TIME_ROI)
        if not text:
            return None

        # Нормализуем: ':' / '.' / пробел между минутами и секундами
        raw = text.strip()
        # Заменяем точки на двоеточие, остальное фильтруем
        raw = raw.replace(".", ":").replace(" ", "")
        # Иногда EasyOCR может вернуть что-то типа '12;34' — заменим всё не-цифра на ':'
        norm = []
        for ch in raw:
            if ch.isdigit() or ch == ":":
                norm.append(ch)
            else:
                norm.append(":")
        norm = "".join(norm)

        if ":" not in norm:
            return None

        parts = norm.split(":")
        # ищем пару (минуты, секунды) справа налево (иногда бывает лишний мусор)
        # пример: ['0', '12', '34'] -> возьмём '12','34'
        if len(parts) >= 2:
            min_str, sec_str = parts[-2], parts[-1]
        else:
            return None

        if not (min_str.isdigit() and sec_str.isdigit()):
            return None

        minutes = int(min_str)
        seconds = int(sec_str)
        if not (0 <= seconds < 60):
            # если секунда отъехала, можно попробовать "обрезать" до двух последних символов
            sec_str2 = sec_str[-2:]
            if sec_str2.isdigit():
                seconds = int(sec_str2)
                if not (0 <= seconds < 60):
                    return None
            else:
                return None

        total = minutes * 60 + seconds
        if total < 0:
            return None
        return total


# -------------------------
# LIVE: скрины раз в секунду + сохранение PNG и inference -> hp_bar.json
# -------------------------
def run_live_capture(interval_sec: float = 1.0, hp_ckpt_path: str = "runs/hp_seq/best.pt"):
    os.makedirs(HP_DIR, exist_ok=True)

    hwnd = find_dota_hwnd()
    if not hwnd:
        print("[!] Окно Dota не найдено")
        return
    print(f"[i] Dota hwnd: {hex(hwnd)} title='{_win_title(hwnd)}'")

    ocr = SelfHp(hp_ckpt_path=hp_ckpt_path)
    tasks = load_or_init_tasks(TASKS_JSON)
    print(f"[i] Using tasks file: {os.path.abspath(TASKS_JSON)}")

    last_ts = 0.0
    while True:
        now = time.time()
        if now - last_ts < interval_sec:
            time.sleep(0.01)
            continue
        last_ts = now

        # 1) берём ROI (RGB)
        roi_rgb = grab_roi_rgb_from_window(hwnd, HP_ROI)
        if roi_rgb is None or roi_rgb.size == 0:
            print("[warn] empty ROI")
            continue

        # 2) ПРЕПРОЦЕСС → МАСКА (0/255) — ЭТО И СОХРАНЯЕМ
        mask_u8 = ocr._preprocess_hp_roi_rawmask(roi_rgb)  # HxW, 0/255

        # 3) ИНФЕРЕНС по маске (без повторного препроцесса)
        try:
            text = ocr._infer_hp_from_mask(mask_u8)
            print(text)

        except Exception as e:
            print("[err] inference failed:", e)
            text = ""

        # 4) СОХРАНЕНИЕ МАСКИ (а не сырого ROI)
        ts = time.strftime("%Y%m%d_%H%M%S")
        us = int((time.time() % 1) * 1e6)
        fname = f"hp_{ts}_{us:06d}.png"
        fpath = os.path.join(HP_DIR, fname)

        # mask_u8 уже одно-канальная — сразу пишем
        #cv2.imwrite(fpath, mask_u8)

        # 5) ДОПИСЫВАЕМ Label Studio задачу (ссылка на МАСКУ)
        abs_png = os.path.abspath(fpath)

        task = build_ls_task_for_hp(abs_png, text)
        tasks.append(task)
        try:
            atomic_dump_json(TASKS_JSON, tasks, make_shadow=TASKS_SHADOW)
            pass
        except Exception as e:
            print(f"[warn] atomic_dump_json failed: {e}")

        print(f"[+] saved {fname}  pred='{text}' (saved MASK)")


# -------------------------
# __main__
# -------------------------
if __name__ == "__main__":
    import argparse, glob

    ap = argparse.ArgumentParser("HUD OCR (HP via NN + gold via EasyOCR) with optional live capture")
    ap.add_argument("--live", action="store_true", help="включить живой режим: раз в секунду ROI->PNG+inference+hp_bar.json")
    ap.add_argument("--interval", type=float, default=1.0, help="период в секундах для live")
    ap.add_argument("--hp_ckpt", type=str, default="runs/hp_seq/best.pt", help="путь к чекпоинту HP модели")
    ap.add_argument("--offline_dir", type=str, default="data/hud/hp_bar", help="оффлайн-папка с hp_bar картинками для проверки")
    args = ap.parse_args()

    if args.live:
        run_live_capture(interval_sec=args.interval, hp_ckpt_path=args.hp_ckpt)
    else:
        # Оффлайн-проверка: прогон по имеющимся hp_bar *.png
        ocr = SelfHp(hp_ckpt_path=args.hp_ckpt)
        imgs = sorted(glob.glob(os.path.join(args.offline_dir, "*.png")))[:10000]
        print(f"Found {len(imgs)} hp-bar images for test")
        for path in imgs:
            img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                print("[skip]", path)
                continue
            img_rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
            text = ocr._infer_hp_text(img_rgb)
            print(os.path.basename(path), "->", repr(text))
