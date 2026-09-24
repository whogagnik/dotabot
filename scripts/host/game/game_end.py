"""Recognize match result and interruption controls on Dota frames."""

from pathlib import Path
from typing import Optional

import cv2
import numpy as np


class GameEndDetector:
    def __init__(self, images_root: Optional[Path] = None, confidence: float = 0.87):
        root = images_root or Path(__file__).resolve().parents[3] / "images"
        self.confidence = confidence
        self.templates = {}
        for key, path in {
            "win_radiant": root / "game" / "win-radiant.png",
            "win_dire": root / "game" / "win-dire.png",
            "ok_when_kicked": root / "game" / "ok-when-kicked.png",
            "return_to_game": root / "game" / "return-to-game.png",
            "spell_lvl_up": root / "game" / "spell-lvl-up.png",
            "dota": root / "lobby" / "dota.png",
        }.items():
            if path.is_file():
                template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if template is not None and template.size:
                    self.templates[key] = template

    def find(
        self, frame_rgb: np.ndarray, key: str, confidence: Optional[float] = None
    ) -> Optional[tuple[int, int]]:
        template = self.templates.get(key)
        if template is None:
            return None
        gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        height, width = template.shape[:2]
        if gray.shape[0] < height or gray.shape[1] < width:
            return None
        _, score, _, top_left = cv2.minMaxLoc(
            cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        )
        if score < (self.confidence if confidence is None else confidence):
            return None
        return top_left[0] + width // 2, top_left[1] + height // 2

    def find_win(self, frame_rgb: np.ndarray) -> Optional[tuple[int, int]]:
        return self.find(frame_rgb, "win_radiant") or self.find(frame_rgb, "win_dire")

    def find_spell_level_up(self, frame_rgb: np.ndarray) -> Optional[tuple[int, int]]:
        """Search the ability bar, where the small level-up plus appears."""
        height, width = frame_rgb.shape[:2]
        left, top = int(width * 0.2), int(height * 0.55)
        right = int(width * 0.8)
        hit = self.find(frame_rgb[top:, left:right], "spell_lvl_up", confidence=0.90)
        return None if hit is None else (hit[0] + left, hit[1] + top)
