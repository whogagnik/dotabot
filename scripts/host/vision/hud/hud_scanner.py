# hud_scanner.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import torch

from scripts.host.core.config import (
    DEFAULT_ML_HP_DIR,
    HP_ROI,
    HUD_HP_MIN_COMBINED_DIGITS,
    HUD_HP_MORPH_KERNEL_SIZE,
    HUD_HP_SPLIT_MAX_RATIO,
    HUD_HP_TEXT_RGB_MAX,
    HUD_HP_TEXT_RGB_MIN,
    HUD_HP_UPSCALE,
    LEVEL_MATCH_MIN_SCORE,
    LEVEL_MAX,
    LEVEL_MIN,
    LEVEL_ROI,
    LEVEL_TEMPLATES_DIR,
)
from scripts.host.ml.infer import load_hp_ocr_net


class SelfHud:
    """Runtime scanner for the player HUD."""

    def __init__(self, device: Optional[str] = None):
        self.hp_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.hp_net = load_hp_ocr_net(DEFAULT_ML_HP_DIR, device=self.hp_device)
        self.hp_device = next(self.hp_net.parameters()).device

        self.hp_pad_token: str = getattr(self.hp_net, "_hp_pad_token")
        self.hp_img_h: int = int(getattr(self.hp_net, "_hp_H"))
        self.hp_img_w: int = int(getattr(self.hp_net, "_hp_W"))
        self.hp_max_len: int = int(getattr(self.hp_net, "_hp_T"))
        self.hp_idx2char: Dict[int, str] = getattr(self.hp_net, "_hp_idx2char")
        self.level_templates: Dict[int, np.ndarray] = self._load_level_templates()

    @staticmethod
    def _crop_roi_from_rgb(
        img_rgb: np.ndarray,
        roi_px: Tuple[int, int, int, int],
    ) -> np.ndarray:
        if img_rgb is None or img_rgb.size == 0:
            return img_rgb

        height, width = img_rgb.shape[:2]
        x1, y1, x2, y2 = roi_px
        x1 = max(0, min(width - 1, int(x1)))
        y1 = max(0, min(height - 1, int(y1)))
        x2 = max(x1 + 1, min(width, int(x2)))
        y2 = max(y1 + 1, min(height, int(y2)))
        return img_rgb[y1:y2, x1:x2].copy()

    @staticmethod
    def _clean_int(text: str) -> Optional[int]:
        digits = "".join(ch for ch in str(text) if ch.isdigit())
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    @staticmethod
    def _preprocess_level_image(img_rgb: np.ndarray) -> Optional[np.ndarray]:
        if img_rgb is None or img_rgb.size == 0:
            return None
        if img_rgb.ndim == 2:
            gray = img_rgb
        else:
            gray = cv2.cvtColor(img_rgb[:, :, :3], cv2.COLOR_RGB2GRAY)
        return gray.astype(np.float32)

    def _load_level_templates(self) -> Dict[int, np.ndarray]:
        templates: Dict[int, np.ndarray] = {}
        for level in range(int(LEVEL_MIN), int(LEVEL_MAX) + 1):
            path = LEVEL_TEMPLATES_DIR / f"{level}.png"
            img_bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img_bgr is None:
                raise FileNotFoundError(f"Level template not found: {path}")

            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            prepared = self._preprocess_level_image(img_rgb)
            if prepared is None:
                raise ValueError(f"Empty level template: {path}")
            templates[level] = prepared

        return templates

    def _preprocess_hp_roi_rawmask(self, img_rgb: np.ndarray) -> Optional[np.ndarray]:
        if img_rgb is None or img_rgb.size == 0:
            return None

        h, w = img_rgb.shape[:2]
        img_rgb_big = cv2.resize(
            img_rgb,
            (int(w * HUD_HP_UPSCALE), int(h * HUD_HP_UPSCALE)),
            interpolation=cv2.INTER_CUBIC,
        )

        lower = np.array(HUD_HP_TEXT_RGB_MIN, dtype=np.uint8)
        upper = np.array(HUD_HP_TEXT_RGB_MAX, dtype=np.uint8)
        mask = cv2.inRange(img_rgb_big, lower, upper)

        kernel = np.ones(HUD_HP_MORPH_KERNEL_SIZE, np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

        if not np.any(mask):
            return None

        return mask

    def _prepare_hp_tensor(self, roi_rgb: np.ndarray) -> Optional[torch.Tensor]:
        mask = self._preprocess_hp_roi_rawmask(roi_rgb)
        if mask is None:
            return None

        img_resized = cv2.resize(
            mask,
            (self.hp_img_w, self.hp_img_h),
            interpolation=cv2.INTER_AREA,
        )
        img_f = img_resized.astype(np.float32) / 255.0
        return torch.from_numpy(img_f).unsqueeze(0).unsqueeze(0)

    def _decode_hp_ids(self, ids: np.ndarray) -> str:
        chars: List[str] = []
        for idx in ids:
            ch = self.hp_idx2char.get(int(idx), "")
            if ch != self.hp_pad_token:
                chars.append(ch)
        return "".join(chars)

    def _infer_hp_text(self, roi_rgb: np.ndarray) -> Optional[str]:
        x = self._prepare_hp_tensor(roi_rgb)
        if x is None:
            return None

        x = x.to(self.hp_device)
        with torch.no_grad():
            logits = self.hp_net(x)
            pred = logits.argmax(dim=2)

        ids = pred.squeeze(0).cpu().numpy()
        return self._decode_hp_ids(ids).strip()

    def get_hp(self, window_rgb: np.ndarray) -> Tuple[Optional[int], Optional[int]]:
        crop = self._crop_roi_from_rgb(window_rgb, HP_ROI)
        text = self._infer_hp_text(crop)
        if not text:
            return None, None

        text = text.strip().replace(" ", "").replace("\\", "/")
        if "/" in text:
            left, right = text.split("/", 1)
            return self._clean_int(left), self._clean_int(right)

        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < HUD_HP_MIN_COMBINED_DIGITS:
            return None, None

        best_split: Optional[Tuple[int, int]] = None
        min_diff = float("inf")
        for split_idx in range(2, len(digits) - 1):
            try:
                cur_hp = int(digits[:split_idx])
                max_hp = int(digits[split_idx:])
            except ValueError:
                continue

            diff = abs(cur_hp - max_hp)
            ratio = diff / max(1, max_hp)
            if ratio < HUD_HP_SPLIT_MAX_RATIO and diff < min_diff:
                min_diff = diff
                best_split = (cur_hp, max_hp)

        if best_split is not None:
            return best_split

        mid = len(digits) // 2
        try:
            return int(digits[:mid]), int(digits[mid:])
        except ValueError:
            return None, None

    def get_hp_ratio(self, window_rgb: np.ndarray) -> Optional[float]:
        cur_hp, max_hp = self.get_hp(window_rgb)
        if cur_hp is None or max_hp is None or max_hp <= 0:
            return None
        return float(cur_hp) / float(max_hp)

    def get_hero_level(self, window_rgb: np.ndarray) -> Optional[int]:
        if window_rgb is None or window_rgb.size == 0:
            return None

        crop = self._crop_roi_from_rgb(window_rgb, LEVEL_ROI)
        prepared = self._preprocess_level_image(crop)
        if prepared is None:
            return None

        best_level: Optional[int] = None
        best_score = float("-inf")

        for level, template in self.level_templates.items():
            target = prepared
            if target.shape != template.shape:
                target = cv2.resize(
                    target,
                    (template.shape[1], template.shape[0]),
                    interpolation=cv2.INTER_AREA,
                )

            mse = float(np.mean((target - template) ** 2))
            score = 1.0 - mse / (255.0 * 255.0)
            if score > best_score:
                best_score = score
                best_level = level

        if best_level is None or best_score < LEVEL_MATCH_MIN_SCORE:
            return None

        return best_level
