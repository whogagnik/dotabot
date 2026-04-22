# hud_scanner.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Optional, List, Dict
import time
import json
import shutil

import numpy as np
import cv2
import torch

from uuid import uuid4
from scripts.host.core.config import *
from scripts.host.ml.infer import load_hp_ocr_net

"""
Модуль OCR для HUD:
 - чтение голды (EasyOCR)
 - чтение HP в формате current/max через свою нейронку
 + live-граббер: раз в секунду сохраняет ROI и prediction в data/hud/hp_bar и hp_bar.json
"""

# -------------------------
# ROI в ПИКСЕЛЯХ: (x1, y1, x2, y2) относительно клиентской области окна игры
# -------------------------


# -------------------------
# Папки и LS интеграция
# -------------------------
DUMP_DIR = "data/hud"
HP_DIR = os.path.join(DUMP_DIR, "hp_bar")
TASKS_JSON = os.path.join(DUMP_DIR, "hp_bar.json")
TASKS_SHADOW = os.path.join(DUMP_DIR, "hp_bar.shadow.json")

# Корень проекта для /data/local-files
PROJECT_ROOT = os.environ.get("LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT", os.getcwd())


# -------------------------
# Вспомогательные утилиты (LS и запись JSON)
# -------------------------
def build_ls_task_for_hp(
    abs_png_path: str,
    hp_text: str | None,
    model_version: str = "hud_ocr_v1",
    score: float | None = None,
) -> dict:
    """
    Формирует задачу для Label Studio с ПРЕДИКТОМ в поле `predictions`
    под твой темплейт:
      <Image name="image" value="$image"/>
      <TextArea name="hp_text" toName="image"/>
    """
    ls_url = _to_localfiles_url(abs_png_path)
    task = {
        "data": {"image": ls_url},
        "meta": {"kind": "hp_bar", "format": "mask_0_255"},
    }

    hp_text = (hp_text or "").strip()
    if hp_text:
        result = [
            {
                "id": str(uuid4()),
                "from_name": "hp_text",
                "to_name": "image",
                "type": "textarea",
                "value": {"text": ""},
            }
        ]
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


def atomic_dump_json(
    path: str,
    data,
    max_retries: int = 20,
    retry_sleep: float = 0.25,
    make_shadow: Optional[str] = None,
):
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


# ============================================================
# 2. HudOCR: HP через свою NN, голда через EasyOCR
# ============================================================
class SelfHud:
    """
    OCR для HUD:
      - read_gold(window_rgb) -> Optional[int]  (EasyOCR)
      - read_hp_pair(window_rgb) -> (Optional[int], Optional[int]) (NN)
      - read_hp_ratio(window_rgb) -> Optional[float]
    """

    def __init__(self, device: Optional[str] = None):
        self.hp_device = device or ("cuda" if torch.cuda.is_available() else "cpu")

        # load once (returns ONLY net)
        self.hp_net = load_hp_ocr_net(DEFAULT_ML_HP_DIR, device=self.hp_device)

        # real device where model lives
        self.hp_device = next(self.hp_net.parameters()).device

        # meta attached in load_hp_ocr_net()
        self.hp_pad_token: str = getattr(self.hp_net, "_hp_pad_token")
        self.hp_img_h: int = int(getattr(self.hp_net, "_hp_H"))
        self.hp_img_w: int = int(getattr(self.hp_net, "_hp_W"))
        self.hp_max_len: int = int(getattr(self.hp_net, "_hp_T"))

        # decoder dict
        self.hp_idx2char: Dict[int, str] = getattr(self.hp_net, "_hp_idx2char")

    # -------------------------
    # Вспомогательные методы
    # -------------------------
    @staticmethod
    def _crop_roi_from_rgb(
        img_rgb: np.ndarray, roi_px: Tuple[int, int, int, int]
    ) -> np.ndarray:
        if img_rgb is None or img_rgb.size == 0:
            return img_rgb
        H, W = img_rgb.shape[:2]
        x1, y1, x2, y2 = roi_px
        x1 = max(0, min(W - 1, int(x1)))
        y1 = max(0, min(H - 1, int(y1)))
        x2 = max(x1 + 1, min(W, int(x2)))
        y2 = max(y1 + 1, min(H, int(y2)))
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
        if mask is None:
            return None
        img_resized = cv2.resize(
            mask, (self.hp_img_w, self.hp_img_h), interpolation=cv2.INTER_AREA
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
        if x is None:
            return None
        x = x.to(self.hp_device)
        with torch.no_grad():
            logits = self.hp_net(x)  # 1 x T x C
            pred = logits.argmax(dim=2)  # 1 x T
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
            mask_u8, (self.hp_img_w, self.hp_img_h), interpolation=cv2.INTER_AREA
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

        with torch.no_grad():
            logits = self.hp_net(x)  # 1 x T x C
            pred = logits.argmax(dim=2)  # 1 x T
        ids = pred.squeeze(0).cpu().numpy()  # T
        return self._decode_hp_ids(ids).strip()

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
            left_digits = "".join(ch for ch in left if ch.isdigit())
            right_digits = "".join(ch for ch in right if ch.isdigit())
            if left_digits:
                try:
                    cur_hp = int(left_digits)
                except ValueError:
                    cur_hp = None
            if right_digits:
                try:
                    max_hp = int(right_digits)
                except ValueError:
                    max_hp = None
            return cur_hp, max_hp

        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits or len(digits) < 4:
            return None, None

        best_split = None
        min_diff = 1e18
        for split_idx in range(2, len(digits) - 1):
            left = digits[:split_idx]
            right = digits[split_idx:]
            try:
                a, b = int(left), int(right)
            except ValueError:
                continue
            diff = abs(a - b)
            ratio = diff / max(1, b)
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
