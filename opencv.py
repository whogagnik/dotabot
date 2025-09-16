# ===== opencv_matcher.py =====================================================
import os, math, time, ctypes
from typing import Optional, Tuple, Sequence, List, Dict, Callable
from collections import deque
import threading

import painter
from painter import init_overlay,paint_with_coords
import pyautogui as p
import win32gui
user32 = ctypes.windll.user32

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np  = None

# -----------------------------------------------------------------------------
# DPI / window helpers
# -----------------------------------------------------------------------------
def _enable_dpi_awareness():
    """Make process DPI-aware so coordinates/screenshots match at 125/150/...% scale."""
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

_enable_dpi_awareness()

Region = Tuple[int,int,int,int]

def _client_region(hwnd: int) -> Region:
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        w, h = max(1, r - l), max(1, b - t)
        return sx, sy, w, h
    except Exception:
        try:
            L, T, R, B = win32gui.GetWindowRect(hwnd)
            return (L, T, max(1, R - L), max(1, B - T))
        except Exception:
            return (0, 0, 1, 1)

# Если в проекте есть глобальный подсказчик масштаба — можно подменить
def _dpi_scale_hint() -> float:
    try:
        return float(user32.GetDpiForSystem()) / 96.0
    except Exception:
        return 1.0

_SCALE_HINT = _dpi_scale_hint()

# -----------------------------------------------------------------------------
# OCVMatcher
# -----------------------------------------------------------------------------
class OCVMatcher:
    """
    Единый класс для матчинга:
      • все вызовы в рамках hwnd;
      • картинки сняты из окна 640×480 → масштабы якорятся по (reg_w/640, reg_h/480);
      • поддержка RGBA шаблонов (альфа-канал как mask), TM_CCORR_NORMED для маски,
        TM_CCOEFF_NORMED — без маски;
      • match_center → (score, (x,y)|None, used_scale).
      • loc() записывает историю (score, x, y, ...), хранится последних N (по умолчанию 20).
    """

    def __init__(self,
                 logger,
                 dpi_anchor: float,
                 base_wh: Tuple[int,int] = (640,480),
                 min_px: int = 8,
                 history_size: int = 20):
        self.log        = logger
        self.base_w     = int(base_wh[0])
        self.base_h     = int(base_wh[1])
        self.dpi_anchor = float(dpi_anchor or 1.0)
        self.min_px     = int(min_px)

        # История loc-вызовов (кольцевой буфер)
        self._hist_lock = threading.Lock()
        self._loc_history = deque(maxlen=max(1, int(history_size)))  # каждый элемент — dict
        init_overlay()
        painter.set_max_items(history_size)

    # ---- управление историей -------------------------------------------------
    def set_history_size(self, maxlen: int) -> None:
        """Задать новую ёмкость истории; старые записи сохранятся по возможности."""
        maxlen = max(1, int(maxlen))
        with self._hist_lock:
            old = list(self._loc_history)
            self._loc_history = deque(old[-maxlen:], maxlen=maxlen)

    def loc_history(self, last: Optional[int] = None) -> List[dict]:
        """
        Получить копию истории. Если last задан — вернуть только последние N записей.
        Формат записи:
          {
            "ts": float,        # timestamp
            "hwnd": int,
            "key": str,         # ключ или путь, который передавался в loc()
            "path": str,        # реальный путь к PNG
            "score": float,
            "x": Optional[int],
            "y": Optional[int],
            "scale": float,     # использованный масштаб лучшего совпадения
            "confidence": float,
            "region": (x,y,w,h) # экранные координаты ROI, где искали
          }
        """
        with self._hist_lock:
            data = list(self._loc_history)
        if last is not None and last > 0:
            return data[-int(last):]
        return data

    def _push_loc_hit(self,
                      *,
                      hwnd: int,
                      key: str,
                      path: Optional[str],
                      score: float,
                      pt: Optional[Tuple[int,int]],
                      scale: float,
                      confidence: float,
                      region: Region) -> None:
        """Внутреннее: добавить запись в историю."""
        entry = {
            "ts": time.time(),
            "hwnd": int(hwnd),
            "key": str(key),
            "path": str(path or ""),
            "score": float(score),
            "x": int(pt[0]) if pt else None,
            "y": int(pt[1]) if pt else None,
            "scale": float(scale),
            "confidence": float(confidence),
            "region": tuple(region),
        }
        with self._hist_lock:
            self._loc_history.append(entry)

    # ---- utils ---------------------------------------------------------------
    def img_path(self, key_or_path: str, atlas: Optional[Dict[str,str]] = None) -> Optional[str]:
        if atlas and key_or_path in atlas:
            path = atlas[key_or_path]
        else:
            path = key_or_path
        return path if (path and os.path.exists(path)) else None

    def grab_roi(self, hwnd: int, region: Optional[Region] = None):
        reg = region or _client_region(hwnd)
        if reg[2] <= 1 or reg[3] <= 1:
            return None, reg
        try:
            shot = p.screenshot(region=reg)  # PIL
            hay  = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR) if (cv2 is not None) else None
            return hay, reg
        except Exception:
            return None, reg

    def _anchor_scale(self, reg: Region) -> float:
        rw, rh = reg[2], reg[3]
        if self.base_w <= 0 or self.base_h <= 0:
            return 1.0
        # привязываемся к меньшему коэффициенту, чтобы сохранять аспект
        anchor = min(rw / float(self.base_w), rh / float(self.base_h)) * self.dpi_anchor
        # ограничим разумно
        return max(0.30, min(2.0, anchor))

    def build_scales(self, reg: Region, templ_wh: Tuple[int,int]) -> List[float]:
        # базовый набор + окрестность якоря
        tw, th = templ_wh
        anchor = self._anchor_scale(reg)
        base   = [0.30,0.33,0.36,0.40,0.45,0.50,0.60,0.70,0.80,0.90,1.00,1.10,1.25,1.50,1.75,2.00]
        around = [anchor*x for x in (0.80,0.90,1.00,1.10,1.25)]
        cand   = sorted({round(s,3) for s in (base + around)})

        rw, rh = reg[2], reg[3]
        out: List[float] = []
        for s in cand:
            w = int(tw*s); h = int(th*s)
            if w < self.min_px or h < self.min_px: continue
            if w >= rw or h >= rh:                 continue
            out.append(s)

        # ближе к якорю — раньше
        out.sort(key=lambda s: abs(math.log(max(s,1e-6)/anchor)))
        # fallback если всё выкинули
        return out or [anchor]

    # ---- core ---------------------------------------------------------------
    def match_center(self,
                     hwnd: int,
                     key_or_path: str,
                     *,
                     atlas: Optional[Dict[str,str]] = None,
                     confidence: float = 0.88,
                     region: Optional[Region] = None,
                     preblur: bool = True) -> Tuple[float, Optional[Tuple[int,int]], float]:
        """
        Возвращает (best_score, (x,y)|None, used_scale).
        Координаты — экранные. Порог НЕ режет — просто сравниваешь score с confidence.
        """
        if cv2 is None or np is None:
            return (float("nan"), None, 1.0)

        path = self.img_path(key_or_path, atlas)
        if not path:
            return (float("nan"), None, 1.0)

        hay, reg = self.grab_roi(hwnd, region)
        if hay is None:
            return (float("nan"), None, 1.0)

        templ_rgba = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if templ_rgba is None:
            return (float("nan"), None, 1.0)

        if templ_rgba.ndim == 3 and templ_rgba.shape[2] == 4:
            templ = templ_rgba[:,:,:3]
            mask  = templ_rgba[:,:,3]
            method = cv2.TM_CCORR_NORMED   # с маской корректно поддерживается
        else:
            templ = templ_rgba if templ_rgba.ndim == 3 else cv2.cvtColor(templ_rgba, cv2.COLOR_GRAY2BGR)
            mask  = None
            method = cv2.TM_CCOEFF_NORMED

        H, W = hay.shape[:2]
        scales = self.build_scales(reg, (templ.shape[1], templ.shape[0]))

        best_val = -1.0
        best_pt  = None
        best_s   = 1.0

        for s in scales:
            h = int(templ.shape[0]*s); w = int(templ.shape[1]*s)
            if h < self.min_px or w < self.min_px or h >= H or w >= W:
                continue

            templ_s = cv2.resize(templ, (w,h), interpolation=cv2.INTER_AREA if s<1.0 else cv2.INTER_LINEAR)
            mask_s  = cv2.resize(mask,  (w,h), interpolation=cv2.INTER_NEAREST) if mask is not None else None

            # немного стабилизируем при сильном даунскейле
            if preblur and s < 0.7:
                hay_use   = cv2.GaussianBlur(hay,   (3,3), 0)
                templ_use = cv2.GaussianBlur(templ_s,(3,3), 0)
            else:
                hay_use, templ_use = hay, templ_s

            try:
                if mask_s is not None:
                    res = cv2.matchTemplate(hay_use, templ_use, method, mask=mask_s)
                else:
                    res = cv2.matchTemplate(hay_use, templ_use, method)
            except Exception:
                res = cv2.matchTemplate(hay_use, templ_use, cv2.TM_CCOEFF_NORMED)

            _minVal, maxVal, _minLoc, maxLoc = cv2.minMaxLoc(res)
            if maxVal > best_val:
                cx = reg[0] + maxLoc[0] + w//2
                cy = reg[1] + maxLoc[1] + h//2
                best_val, best_pt, best_s = float(maxVal), (cx, cy), float(s)

            # быстрый выход при уверенном попадании
            if maxVal >= confidence:
                cx = reg[0] + maxLoc[0] + w//2
                cy = reg[1] + maxLoc[1] + h//2
                return (float(maxVal), (cx, cy), float(s))

        return (best_val, best_pt, best_s)

    # удобные обёртки
    def loc(self, hwnd: int, key_or_path: str, *,
            atlas: Optional[Dict[str,str]] = None,
            confidence: float = 0.88,
            region: Optional[Region] = None) -> Optional[Tuple[int,int]]:
        """
        Возвращает координату при score >= confidence, иначе None.
        ВСЕ вызовы loc() пишутся в историю (score, x, y, scale, ...).
        """
        score, pt, used_scale = self.match_center(
            hwnd, key_or_path, atlas=atlas, confidence=confidence, region=region
        )

        # выясним реальный путь, чтобы видеть какой файл искали
        real_path = self.img_path(key_or_path, atlas)

        # записать в историю (всегда, даже при pt=None)
        reg = region or _client_region(hwnd)
        self._push_loc_hit(
            hwnd=hwnd,
            key=key_or_path,
            path=real_path,
            score=score if score == score else float("nan"),
            pt=pt,
            scale=used_scale,
            confidence=confidence,
            region=reg,
        )

        # вернуть результат исполнения
        points = self.loc_history()
        for point in points:
            paint_with_coords(point['x'], point['y'], str(round(int(point['score']),2)))
        return pt if (pt is not None and score >= confidence) else None

    def has(self, hwnd: int, key_or_path: str, *,
            atlas: Optional[Dict[str,str]] = None,
            confidence: float = 0.88,
            region: Optional[Region] = None) -> bool:
        return self.loc(hwnd, key_or_path, atlas=atlas, confidence=confidence, region=region) is not None

    def count_any(self, hwnd: int, keys: Sequence[str], *,
                  atlas: Optional[Dict[str,str]] = None,
                  confidence: float = 0.88,
                  region: Optional[Region] = None) -> int:
        c = 0
        for k in keys:
            if self.has(hwnd, k, atlas=atlas, confidence=confidence, region=region):
                c += 1
        return c

    def count_any_across(self, hwnds: Sequence[int], keys: Sequence[str], *,
                         atlas: Optional[Dict[str,str]] = None,
                         confidence: float = 0.88) -> int:
        return sum(self.count_any(h, keys, atlas=atlas, confidence=confidence) for h in hwnds)

    # отладочный проход по всему атласу (match_center — без записи истории)
    def debug_scan_atlas(self, hwnd: int, atlas: Dict[str,str],
                         *, confidence: float = 0.0,
                         annotate: Optional[Callable[[int,int,str], None]] = None,
                         region: Optional[Region] = None) -> List[Tuple[str,float,Optional[Tuple[int,int]],float]]:
        results = []
        for k, path in atlas.items():
            if not path or not os.path.exists(path): continue
            score, pt, s = self.match_center(hwnd, k, atlas=atlas, confidence=confidence, region=region)
            results.append((k, score, pt, s))
            if annotate and pt and score >= confidence:
                try:
                    annotate(pt[0], pt[1], f"{k}: {score:.3f} ×{s:.2f}")
                except Exception:
                    pass
        results.sort(key=lambda x: -(x[1] if x[1]==x[1] else -1.0))
        return results
# =============================================================================
