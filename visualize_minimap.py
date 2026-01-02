# visualize_minimap.py
# -*- coding: utf-8 -*-

"""
Захват миникарты 100x100 из правого-нижнего угла окна Dota → инференс →
PNG + задача в ls_tasks.json (ABS /data/local-files) под конфиг LS:

<View>
  <Image name="image" value="$image" zoomControl="true"/>
  <RectangleLabels name="kp" toName="image" strokewidth="2" opacity="0.9">
    <Label value="self"/><Label value="ally"/><Label value="enemy"/>
  </RectangleLabels>
</View>

Запуск Label Studio (PowerShell):
  $env:LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED="true"
  $env:LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT="C:/Users/bajojo/PycharmProjects/dotabot"
  label-studio start
"""

import os
import sys
import time
import json
import shutil
from typing import Dict, List, Tuple, Optional
from urllib.parse import quote

import numpy as np
import cv2
import win32gui  # Win32 окно/координаты
import torch
# ==== Импорт из тренировочного скрипта ====
try:
    from train_minimap_heatmap import load_model, infer_image, find_peaks_per_channel
except Exception as e:
    print("[!] Не удалось импортировать из train_minimap_heatmap.py:", e)
    sys.exit(1)

# ==== Константы ====
CKPT_PATH = "runs/minimap/best.pt"

PATCH_W = 100
PATCH_H = 100

DISPLAY_SIZE_DEFAULT = 200
THR_DEFAULT = 0.7
NMS_DEFAULT = 7
GAUSS_DEFAULT = True

DUMP_DIR_DEFAULT = "data/minimap_hard"
TASKS_FILENAME = "ls_tasks.json"
SHADOW_FILENAME = "ls_tasks.shadow.json"

DUMP_PREDS_DEFAULT = True
PRED_BOX_PX_DEFAULT = 8
AUTO_DUMP_SEC_DEFAULT = 1.0
AUTO_DUMP_IF_MISS_SELF_DEFAULT = True

# ИМЕНА ВАШЕГО ИНТЕРФЕЙСА LS:
LS_FROM_NAME = "kp"      # <RectangleLabels name="kp" ...>
LS_TO_NAME   = "image"   # <Image name="image" ...>
LS_GEOM      = "rect"    # прямоугольники (rectanglelabels)

# Цвета (BGR)
COLORS = {
    "self":  (40, 215, 255),
    "ally":  (60, 220, 60),
    "enemy": (30, 30, 230),
}

# ==== Win32 utils ====
def _title(hwnd:int)->str:
    try: return win32gui.GetWindowText(hwnd) or ""
    except: return ""

def _is_main_visible(hwnd:int)->bool:
    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not win32gui.GetParent(hwnd) and bool(_title(hwnd).strip())
    except: return False

def _area(hwnd:int)->int:
    try:
        L,T,R,B = win32gui.GetWindowRect(hwnd)
        return max(0,R-L)*max(0,B-T)
    except: return 0

def find_dota_hwnd()->Optional[int]:
    c=[]
    def cb(hwnd,_):
        if not _is_main_visible(hwnd): return
        t=_title(hwnd)
        if "Dota 2" in t or "Dota" in t:
            c.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not c: return None
    c.sort(key=_area, reverse=True)
    return c[0]

def client_rect_screen(hwnd:int)->Tuple[int,int,int,int]:
    """Клиентская область в координатах экрана (device pixels)."""
    try:
        l,t,r,b = win32gui.GetClientRect(hwnd)
        x,y = win32gui.ClientToScreen(hwnd, (0,0))
        return x,y,max(1,r-l),max(1,b-t)
    except:
        L,T,R,B = win32gui.GetWindowRect(hwnd)
        return L,T,max(1,R-L),max(1,B-T)

# ==== Захват экрана ====
_BACKEND=None
_cam=None
_mss=None

_BACKEND = None
_cam = None
_mss = None

def init_grabber(monitor_idx: int = 0, target_fps: int = 30):
    """
    Пытаемся dxcam → mss → pyautogui.
    На dxcam ловим WinError 87 (ошибка таймера) и деградируем.
    """
    global _BACKEND, _cam, _mss
    # --- DXCAM ---


    # --- PyAutoGUI (медленно, но работает почти всегда) ---
    try:
        import pyautogui as p  # noqa
        _BACKEND = "pyautogui"
        print("[i] grab: pyautogui")
        return
    except Exception as e:
        print("[!] no dxcam/mss/pyautogui available:", e)
        raise


def grab_region_bgr(x:int,y:int,w:int,h:int)->Optional[np.ndarray]:
    if _BACKEND=="dxcam":
        frame = _cam.get_latest_frame()
        if not (isinstance(frame, np.ndarray) and frame.size>0):
            frame = _cam.grab()
        if not (isinstance(frame, np.ndarray) and frame.size>0):
            return None
        H,W = frame.shape[:2]
        if x<0 or y<0 or x+w>W or y+h>H: return None
        return frame[y:y+h, x:x+w].copy()
    elif _BACKEND=="mss":
        raw = _mss.grab({"left":x,"top":y,"width":w,"height":h})
        return np.asarray(raw)[:,:,:3].copy()
    elif _BACKEND=="pyautogui":
        import pyautogui as p
        shot = p.screenshot(region=(x,y,w,h))
        return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
    return None

# ==== Визуализация ====
def letterbox(img:np.ndarray, size:int=200, color=(16,16,16))->np.ndarray:
    h,w=img.shape[:2]; s=min(size/w, size/h); nw,nh=int(w*s),int(h*s)
    out=np.full((size,size,3), color, np.uint8)
    rs=cv2.resize(img,(nw,nh), interpolation=cv2.INTER_AREA)
    x0=(size-nw)//2; y0=(size-nh)//2; out[y0:y0+nh, x0:x0+nw]=rs
    return out

def draw_overlays(mm_bgr:np.ndarray, peaks:Dict[int,List[Tuple[float,float,float]]], classes:List[str])->np.ndarray:
    h,w=mm_bgr.shape[:2]; img=mm_bgr.copy(); counts={}
    for ci,name in enumerate(classes):
        pts=peaks.get(ci,[])
        k = 1 if name=="self" else 6 if name=="ally" else 10
        pts = pts[:k]
        counts[name]=len(pts)
        for (u,v,s) in pts:
            x=int(u*w); y=int(v*h); r=3
            cv2.rectangle(img,(max(0,x-r),max(0,y-r)),(min(w-1,x+r),min(h-1,y+r)), COLORS[name],1, cv2.LINE_AA)
            cv2.circle(img,(x,y),1,COLORS[name],-1, cv2.LINE_AA)
    y=14
    for name in classes:
        #cv2.putText(img,f"{name}:{counts.get(name,0)}",(4,y),cv2.FONT_HERSHEY_SIMPLEX,0.46,COLORS[name],1, cv2.LINE_AA)
        y+=12
    return img

# ==== Label Studio helpers ====
def _to_unix(p: str) -> str:
    return p.replace("\\", "/")

def _to_localfiles_url(abs_img_path: str) -> str:
    return "/data/local-files/?d=" + quote(_to_unix(abs_img_path), safe="/")

def peaks_to_ls_results(
    peaks: Dict[int, List[Tuple[float,float,float]]],
    classes: List[str],
    *,
    from_name: str = LS_FROM_NAME,
    to_name: str   = LS_TO_NAME,
    box_px: int    = PRED_BOX_PX_DEFAULT,
    patch_w: int   = PATCH_W,
    patch_h: int   = PATCH_H
) -> List[Dict]:
    """Преобразуем пики в LS RectangleLabels (в процентах) — from_name='kp', to_name='image'."""
    results=[]
    bw_pct = 100.0 * (box_px / max(1, patch_w))
    bh_pct = 100.0 * (box_px / max(1, patch_h))
    for ci, name in enumerate(classes):
        for (u,v,score) in peaks.get(ci, []):
            cx = u*100.0; cy = v*100.0
            x_pct = max(0.0, cx - bw_pct/2)
            y_pct = max(0.0, cy - bh_pct/2)
            results.append({
                "from_name": from_name,
                "to_name": to_name,
                "type": "rectanglelabels",
                "value": {
                    "x": x_pct, "y": y_pct,
                    "width": bw_pct, "height": bh_pct,
                    "rotation": 0,
                    "rectanglelabels": [name]
                },
                "score": float(score),
            })
    return results

# ==== Надёжная запись JSON на Windows ====
def atomic_dump_json(path:str, data, max_retries:int=20, retry_sleep:float=0.25, make_shadow:Optional[str]=None):
    folder = os.path.dirname(path) or "."
    base = os.path.basename(path)
    ts = int(time.time()*1e6)
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
            print(f"[warn] ls_tasks.json locked; wrote shadow: {make_shadow}")
        except Exception as e:
            print(f"[err] shadow write failed: {e}")
    try: os.remove(tmp_path)
    except Exception: pass
    if last_exc:
        raise last_exc

class TasksWriter:
    """Единый ls_tasks.json с ABS /data/local-files ссылками и predictions (kp/image)."""
    def __init__(self, dump_dir:str, dump_preds:bool=True, pred_box_px:int=PRED_BOX_PX_DEFAULT):
        self.dump_dir = dump_dir
        self.img_dir  = os.path.join(dump_dir, "images")
        self.tasks_path = os.path.join(dump_dir, TASKS_FILENAME)
        self.shadow_path = os.path.join(dump_dir, SHADOW_FILENAME)
        self.dump_preds = dump_preds
        self.pred_box_px = pred_box_px

        os.makedirs(self.img_dir, exist_ok=True)

        if os.path.exists(self.tasks_path):
            try:
                with open(self.tasks_path, "r", encoding="utf-8") as f:
                    self.tasks = json.load(f)
                if not isinstance(self.tasks, list):
                    print("[!] ls_tasks.json не массив — пересоздаю.")
                    self.tasks = []
            except Exception:
                print("[!] не удалось прочитать ls_tasks.json — пересоздаю.")
                self.tasks = []
                self._flush()
        else:
            self.tasks = []
            self._flush()

    def _flush(self):
        try:
            atomic_dump_json(self.tasks_path, self.tasks, make_shadow=self.shadow_path)
        except Exception as e:
            print(f"[warn] ls_tasks.json replace failed: {e}. Using shadow for this iteration.")

    def save_sample(self,
                    img_bgr: np.ndarray,
                    classes: List[str],
                    peaks: Dict[int, List[Tuple[float,float,float]]]) -> str:
        ts = time.strftime("%Y%m%d_%H%M%S")
        us = int((time.time()%1)*1e6)
        fname = f"mm_{ts}_{us:06d}.png"
        fpath = os.path.join(self.img_dir, fname)
        cv2.imwrite(fpath, img_bgr)

        abs_png = os.path.abspath(fpath)
        task = {"data": {"image": _to_localfiles_url(abs_png)}}

        if self.dump_preds:
            preds = peaks_to_ls_results(
                peaks, classes,
                from_name=LS_FROM_NAME,
                to_name=LS_TO_NAME,
                box_px=self.pred_box_px,
                patch_w=PATCH_W, patch_h=PATCH_H
            )
            task["predictions"] = [{
                "result": preds,
                "score": 1.0,
                "model_version": "minimap-auto"
            }]

        self.tasks.append(task)
        self._flush()
        return fpath

# ==== Main ====
def main():
    import argparse
    ap = argparse.ArgumentParser("Minimap 100x100 → ls_tasks.json (kp/image rectanglelabels)")
    ap.add_argument("--ckpt", type=str, default=CKPT_PATH)
    ap.add_argument("--display", type=int, default=DISPLAY_SIZE_DEFAULT)
    ap.add_argument("--thr", type=float, default=THR_DEFAULT)
    ap.add_argument("--nms", type=int, default=NMS_DEFAULT)
    ap.add_argument("--monitor", type=int, default=0)
    ap.add_argument("--dx", type=int, default=0, help="сдвиг X от правого-нижнего угла: влево(+)/вправо(-), px")
    ap.add_argument("--dy", type=int, default=0, help="сдвиг Y от правого-нижнего угла: вверх(+)/вниз(-), px")
    ap.add_argument("--gauss", action="store_true", default=GAUSS_DEFAULT)

    ap.add_argument("--dump_dir", type=str, default=DUMP_DIR_DEFAULT)
    ap.add_argument("--dump_preds", type=int, default=1 if DUMP_PREDS_DEFAULT else 0)
    ap.add_argument("--pred_box_px", type=int, default=PRED_BOX_PX_DEFAULT)
    ap.add_argument("--auto_dump_sec", type=float, default=AUTO_DUMP_SEC_DEFAULT)
    ap.add_argument("--auto_dump_if_miss_self", type=int, default=1 if AUTO_DUMP_IF_MISS_SELF_DEFAULT else 0)

    args = ap.parse_args()

    hwnd = find_dota_hwnd()
    if not hwnd:
        print("[!] окно Dota не найдено"); return
    print(f"[i] Dota hwnd: {hex(hwnd)}  title='{_title(hwnd)}'")

    init_grabber(monitor_idx=args.monitor)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    net, classes, size = load_model(args.ckpt, device=device)
    device = next(net.parameters()).device.type
    print(f"[i] model: {args.ckpt} classes={classes} size={size} device={device}")

    writer = TasksWriter(args.dump_dir, dump_preds=bool(args.dump_preds), pred_box_px=args.pred_box_px)
    print(f"[i] dump dir: {os.path.abspath(args.dump_dir)}  tasks: {TASKS_FILENAME}")

    cv2.namedWindow("Minimap", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Minimap", args.display, args.display)

    thr=args.thr; nms_k=args.nms
    t_prev=time.time(); fps_ema=None
    last_dump_ts = 0.0
    W,H=PATCH_W, PATCH_H

    while True:
        cx,cy,cw,ch = client_rect_screen(hwnd)

        # правый-нижний угол 100x100 + мелкие смещения
        x = cx + cw - W + (-args.dx)
        y = cy + ch - H + (-args.dy)

        mm_bgr = grab_region_bgr(x,y,W,H)
        if mm_bgr is None or mm_bgr.size==0:
            time.sleep(0.02); continue

        mm_rgb = cv2.cvtColor(mm_bgr, cv2.COLOR_BGR2RGB)


        prob  = infer_image(net, mm_rgb, size=size, device=device)  # CxHxW
        peaks = find_peaks_per_channel(prob, thr=thr, nms_kernel=nms_k)

        vis = draw_overlays(mm_bgr, peaks, classes)

        now=time.time(); dt=now-t_prev; t_prev=now
        fps=1.0/max(dt,1e-6); fps_ema=fps if fps_ema is None else 0.9*fps_ema+0.1*fps
        cv2.putText(vis, f"FPS:{fps_ema:5.1f} thr:{thr:.2f} nms:{nms_k}",
                    (4, vis.shape[0]-6), cv2.FONT_HERSHEY_SIMPLEX,0.46,(255,255,255),1,cv2.LINE_AA)

        disp = vis if args.display==100 else letterbox(vis, size=args.display)
        cv2.imshow("Minimap", disp)

        # автосохранение
        should_dump = False
        if args.auto_dump_sec > 0 and (now - last_dump_ts) >= args.auto_dump_sec:
            should_dump = True
        self_ch = classes.index("self") if "self" in classes else 0
        if args.auto_dump_if_miss_self and len(peaks.get(self_ch, []))==0 and (now - last_dump_ts) >= 1.0:
            should_dump = True

        if should_dump:
            out = writer.save_sample(mm_bgr, classes, peaks)
            print(f"[auto] saved {out}")
            last_dump_ts = now

        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q')):
            break
        elif key in (ord('+'), ord('=')):
            thr = min(0.99, thr+0.02)
        elif key in (ord('-'), ord('_')):
            thr = max(0.01, thr-0.02)
        elif key == ord(']'):
            nms_k = min(31, nms_k+2);  nms_k += (nms_k % 2 == 0)
        elif key == ord('['):
            nms_k = max(1, nms_k-2);   nms_k -= (nms_k % 2 == 0)
        elif key == ord('x'):
            out = writer.save_sample(mm_bgr, classes, peaks)
            print(f"[manual] saved {out}")
            last_dump_ts = now

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()

