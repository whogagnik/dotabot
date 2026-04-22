# hud_live_capture.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
from scripts.host.core.utils import find_dota_hwnd, grab_roi_rgb_from_window, _title
from scripts.host.vision.hud.hud_scanner import (
    HP_ROI,
    HP_DIR,
    TASKS_JSON,
    TASKS_SHADOW,
    SelfHud,
    load_or_init_tasks,
    build_ls_task_for_hp,
    atomic_dump_json,
)


def run_live_capture(interval_sec: float = 1.0) -> None:
    os.makedirs(HP_DIR, exist_ok=True)

    hwnd = find_dota_hwnd()
    if not hwnd:
        print("[!] Окно Dota не найдено")
        return
    print(f"[i] Dota hwnd: {hex(hwnd)} title='{_title(hwnd)}'")

    ocr = SelfHud()
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

        # 2) ПРЕПРОЦЕСС → МАСКА (0/255)
        mask_u8 = ocr._preprocess_hp_roi_rawmask(roi_rgb)
        if mask_u8 is None:
            # если маска пустая — просто пропускаем тик
            print("[warn] empty mask")
            continue

        # 3) ИНФЕРЕНС по маске
        try:
            text = ocr._infer_hp_from_mask(mask_u8)
            print(text)
        except Exception as e:
            print("[err] inference failed:", e)
            text = ""

        # 4) СОХРАНЕНИЕ (если решишь писать файл)
        ts = time.strftime("%Y%m%d_%H%M%S")
        us = int((time.time() % 1) * 1e6)
        fname = f"hp_{ts}_{us:06d}.png"
        fpath = os.path.join(HP_DIR, fname)

        # Если нужно реально сохранять маску — раскомментируй и добавь cv2 импорт тут,
        # либо сохраняй в hud_scanner, как раньше.
        import cv2

        cv2.imwrite(fpath, mask_u8)

        abs_png = os.path.abspath(fpath)

        # 5) ДОПИСЫВАЕМ Label Studio задачу
        task = build_ls_task_for_hp(abs_png, text)
        tasks.append(task)

        try:
            atomic_dump_json(TASKS_JSON, tasks, make_shadow=TASKS_SHADOW)
        except Exception as e:
            print(f"[warn] atomic_dump_json failed: {e}")

        print(f"[+] saved {fname}  pred='{text}' (saved MASK)")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        "HUD OCR (HP via NN + gold via EasyOCR) with optional live capture"
    )
    ap.add_argument(
        "--interval", type=float, default=1.0, help="период в секундах для live"
    )

    args = ap.parse_args()

    run_live_capture(interval_sec=args.interval)
