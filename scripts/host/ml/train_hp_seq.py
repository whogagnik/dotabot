# train_hp_seq.py
# -*- coding: utf-8 -*-

import os
import json
import argparse
from typing import List, Dict, Any, Optional, Tuple, Set
from tqdm import tqdm
import numpy as np
import cv2
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, random_split
import random

from scripts.host.ml.models import HudHPSeqNet

# ============================================================
# 1. Конфигурация алфавита и словаря
# ============================================================

ALPHABET = "0123456789/"
PAD_TOKEN = "<PAD>"
VOCAB = list(ALPHABET) + [PAD_TOKEN]
CHAR2IDX = {c: i for i, c in enumerate(VOCAB)}
IDX2CHAR = {i: c for i, c in enumerate(VOCAB)}
NUM_CLASSES = len(VOCAB)

# ============================================================
# 2. Утилиты
# ============================================================


def set_seed(seed: int = 1337):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def encode_text(text: str, max_len: int) -> np.ndarray:
    text = text.strip()
    ids = [CHAR2IDX[ch] for ch in text if ch in CHAR2IDX]
    ids = ids[:max_len] + [CHAR2IDX[PAD_TOKEN]] * max(0, max_len - len(ids))
    return np.array(ids, dtype=np.int64)


def decode_ids(ids: np.ndarray) -> str:
    return "".join(
        IDX2CHAR.get(int(i), "") for i in ids if IDX2CHAR.get(int(i), "") != PAD_TOKEN
    )


def _extract_local_path_from_ls_url(url: str) -> str:
    """
    /data/local-files/?d=data/hud/hp_bar/hp_....png -> data/hud/hp_bar/hp_....png
    """
    if not url:
        return ""
    if "?d=" in url:
        return url.split("?d=", 1)[1]
    return url


def int_or_none(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    s = str(s).strip()
    if s.lower() in ("none", "null"):
        return None
    return int(s)


# ============================================================
# 3. Простой парсер чисел по фиксированному положению '/'
# ============================================================


def extract_numbers_and_slash_simple(
    mask_u8: np.ndarray,
    slash_x_min: int,
    slash_x_max: int,
) -> Optional[
    Tuple[np.ndarray, np.ndarray, np.ndarray, Tuple[int, int, int, int, int, int]]
]:
    """
    mask_u8: бинарная маска HxW (0/255), после resize в (img_h, img_w).

    Алгоритм:
      - считаем colsum по колонкам;
      - ищем слеш в коридоре [slash_x_min; slash_x_max];
      - левое число: от 0 до x0_слеша-1;
      - правое число: от x1_слеша+1 до W-1.

    Возвращает:
      (left_crop, slash_crop, right_crop, (lx0, lx1, sx0, sx1, rx0, rx1))
    """
    H, W = mask_u8.shape[:2]
    colsum = (mask_u8 > 0).sum(axis=0)

    # аккуратно зажимаем коридор в пределах [0, W-1]
    sx_min = max(0, min(W - 1, slash_x_min))
    sx_max = max(0, min(W - 1, slash_x_max))
    if sx_max < sx_min:
        sx_min, sx_max = sx_max, sx_min

    def find_range(x0: int, x1: int) -> Optional[Tuple[int, int]]:
        x0 = max(0, min(W - 1, x0))
        x1 = max(0, min(W - 1, x1))
        if x1 < x0:
            return None
        xs = [x for x in range(x0, x1 + 1) if colsum[x] > 0]
        if not xs:
            return None
        return min(xs), max(xs)

    # 1) слеш в коридоре
    slash_range = find_range(sx_min, sx_max)
    if slash_range is None:
        return None
    sx0, sx1 = slash_range

    # 2) левое число до реальной левой границы слеша
    left_range = find_range(0, sx0 - 1)

    # 3) правое число после реальной правой границы слеша
    right_range = find_range(sx1 + 1, W - 1)

    if left_range is None or right_range is None:
        return None

    lx0, lx1 = left_range
    rx0, rx1 = right_range

    left_crop = mask_u8[:, lx0 : lx1 + 1].copy()
    slash_crop = mask_u8[:, sx0 : sx1 + 1].copy()
    right_crop = mask_u8[:, rx0 : rx1 + 1].copy()

    if left_crop.shape[1] < 1 or right_crop.shape[1] < 1 or slash_crop.shape[1] < 1:
        return None

    return left_crop, slash_crop, right_crop, (lx0, lx1, sx0, sx1, rx0, rx1)


def build_number_bank_and_xy(
    imgs: List[np.ndarray],
    texts: List[str],
    slash_x_min: int,
    slash_x_max: int,
) -> Tuple[
    Dict[str, List[np.ndarray]],
    Optional[np.ndarray],
    List[str],
    List[str],
    Optional[int],
    Optional[int],
    Optional[int],
    Optional[int],
]:
    """
    Строим:
      - number_bank: { "500": [кропы числа 500 (слева/справа), ...], ... }
      - slash_template: один выбранный кроп слеша (берём первый найденный)
      - X: список уникальных левых чисел (по аннотации)
      - Y: список уникальных правых чисел (по аннотации)
      - anchor_lx1: x, где заканчиваются левые числа (правый край)
      - anchor_rx0: x, где начинаются правые числа (левый край)
      - anchor_sx0, anchor_sx1: границы слеша в оригинале
    """
    number_bank: Dict[str, List[np.ndarray]] = {}
    slash_template: Optional[np.ndarray] = None
    set_X: Set[str] = set()
    set_Y: Set[str] = set()
    bad_cnt = 0

    anchor_lx1: Optional[int] = None
    anchor_rx0: Optional[int] = None
    anchor_sx0: Optional[int] = None
    anchor_sx1: Optional[int] = None

    for mask_u8, raw_text in zip(imgs, texts):
        text = raw_text.strip()
        if "/" not in text:
            bad_cnt += 1
            continue
        lt, rt = text.split("/", 1)
        lt_clean = "".join(ch for ch in lt if ch.isdigit())
        rt_clean = "".join(ch for ch in rt if ch.isdigit())
        if not lt_clean or not rt_clean:
            bad_cnt += 1
            continue

        ext = extract_numbers_and_slash_simple(mask_u8, slash_x_min, slash_x_max)
        if ext is None:
            bad_cnt += 1
            continue

        left_crop, slash_crop, right_crop, (lx0, lx1, sx0, sx1, rx0, rx1) = ext

        number_bank.setdefault(lt_clean, []).append(left_crop)
        number_bank.setdefault(rt_clean, []).append(right_crop)

        if slash_template is None:
            slash_template = slash_crop
            anchor_lx1 = lx1
            anchor_rx0 = rx0
            anchor_sx0 = sx0
            anchor_sx1 = sx1

        set_X.add(lt_clean)
        set_Y.add(rt_clean)

    number_bank = {k: v for k, v in number_bank.items() if v}
    X = sorted(set_X)
    Y = sorted(set_Y)

    print(
        f"[aug] bank: numbers={len(number_bank)} unique; X={len(X)}; Y={len(Y)}; bad={bad_cnt}"
    )
    if slash_template is None:
        print("[aug] WARNING: slash_template is None (не нашли ни одного слеша)")
    else:
        print(
            f"[aug] anchors: lx1={anchor_lx1}, rx0={anchor_rx0}, sx0={anchor_sx0}, sx1={anchor_sx1}"
        )

    return (
        number_bank,
        slash_template,
        X,
        Y,
        anchor_lx1,
        anchor_rx0,
        anchor_sx0,
        anchor_sx1,
    )


# ============================================================
# 4. Парсер экспорта Label Studio
# ============================================================


def parse_hp_ls_export(
    json_path: str,
    project_root: Optional[str] = None,
    textarea_name: str = "hp_text",
) -> List[Dict[str, Any]]:
    """
    Ожидается:
      - data.image: "/data/local-files/?d=data/hud/hp_bar/hp_....png"
      - annotations[*].result[*]: type="textarea", from_name="hp_text", value.text=["500/500"]
    Возвращает список словарей { "image_path": <абс. путь>, "text": "500/500" }
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("LS export must be a list of tasks")

    if project_root is None:
        project_root = os.getcwd()

    samples: List[Dict[str, Any]] = []

    for task in data:
        data_field = task.get("data") or {}
        img_url = data_field.get("image")
        if not img_url:
            continue

        rel_path = _extract_local_path_from_ls_url(img_url)
        img_path = os.path.join(project_root, rel_path)
        img_path = os.path.normpath(img_path)

        anns = task.get("annotations") or task.get("completions") or []
        if not anns:
            continue

        hp_str: Optional[str] = None
        for ann in anns:
            result = ann.get("result") or []
            for r in result:
                if (r.get("type") or "").lower() != "textarea":
                    continue
                if r.get("from_name") != textarea_name:
                    continue
                val = r.get("value") or {}
                txt_list = val.get("text") or []
                if not txt_list:
                    continue
                hp_str = str(txt_list[0]).strip()

        if not hp_str:
            continue

        samples.append(
            {
                "image_path": img_path,
                "text": hp_str,
            }
        )

    return samples


# ============================================================
# 5. Загрузка и бинаризация картинок HP-бара
# ============================================================


def load_and_binarize_hp(
    path: str,
    img_h: int,
    img_w: int,
    thr: Optional[int] = 200,
) -> np.ndarray:
    """
    Возвращает маску 2D uint8 [0,255]: белый текст на чёрном фоне.
    Поддерживает thr=None -> Otsu.
    """
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(path)

    if img.ndim == 3:
        if img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    img = cv2.resize(img, (img_w, img_h), interpolation=cv2.INTER_AREA)
    img = img.astype(np.uint8)

    if thr is None:
        _, mask = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        _, mask = cv2.threshold(img, thr, 255, cv2.THRESH_BINARY)
    return mask


# ============================================================
# 6. Dataset
# ============================================================


class HpSeqOfflineDataset(Dataset):
    def __init__(self, imgs: List[np.ndarray], labels: List[np.ndarray]):
        assert len(imgs) == len(labels)
        self.imgs = imgs
        self.labels = labels

    def __len__(self):
        return len(self.imgs)

    def __getitem__(self, idx: int):
        img = self.imgs[idx]  # 2D uint8 HxW
        lbl = self.labels[idx]  # 1D int64 T

        img_f = img.astype(np.float32) / 255.0
        x = torch.from_numpy(img_f).unsqueeze(0)  # 1xHxW
        y = torch.from_numpy(lbl.astype(np.int64))  # T
        return x, y


# ============================================================
# 7. Сборка синтетики X/X, X/Y, Y/Y, Y/X
# ============================================================


def _ensure_h(crop: np.ndarray, H: int) -> np.ndarray:
    return (
        crop
        if crop.shape[0] == H
        else cv2.resize(
            crop,
            (int(crop.shape[1] * H / crop.shape[0]), H),
            interpolation=cv2.INTER_NEAREST,
        )
    )


def synthesize_pair_xy(
    left_num: str,
    right_num: str,
    number_bank: Dict[str, List[np.ndarray]],
    slash_crop: np.ndarray,
    img_h: int,
    img_w: int,
    slash_x_min: int,
    slash_x_max: int,
    space_px: int = 1,
) -> Optional[np.ndarray]:
    """
    Собирает новое изображение вида left_num / right_num:
      - выбираем случайный кроп числа left_num и right_num из number_bank;
      - используем ОДИН шаблон слеша (slash_crop), как ты хотел;
      - ставим слеш так, чтобы его центр был в [slash_x_min; slash_x_max].
    """
    if slash_crop is None:
        return None
    if left_num not in number_bank or right_num not in number_bank:
        return None

    H, W = img_h, img_w
    canvas = np.zeros((H, W), np.uint8)

    l_crop = _ensure_h(random.choice(number_bank[left_num]), H)
    r_crop = _ensure_h(random.choice(number_bank[right_num]), H)
    s_crop = _ensure_h(slash_crop, H)

    wl, ws, wr = l_crop.shape[1], s_crop.shape[1], r_crop.shape[1]

    # пробуем все x центра слеша, пока не найдём, куда оно влазит
    for cx in range(slash_x_min, slash_x_max + 1):
        x_s = max(0, min(W - ws, cx - ws // 2))
        x_l = x_s - space_px - wl
        x_r = x_s + ws + space_px
        if x_l < 0 or (x_r + wr) > W:
            continue

        out = canvas.copy()
        out[:, x_l : x_l + wl] = np.maximum(out[:, x_l : x_l + wl], l_crop)
        out[:, x_s : x_s + ws] = np.maximum(out[:, x_s : x_s + ws], s_crop)
        out[:, x_r : x_r + wr] = np.maximum(out[:, x_r : x_r + wr], r_crop)
        return out

    return None


def build_full_augmented_dataset(
    samples: List[Dict[str, Any]],
    img_h: int,
    img_w: int,
    max_len: int,
    bin_thr: Optional[int] = 200,
    xy_cross: bool = True,
    slash_x_min: int = 250,
    slash_x_max: int = 290,
) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    all_imgs: List[np.ndarray] = []
    all_labels: List[np.ndarray] = []
    all_texts: List[str] = []

    # 1) ОРИГИНАЛЫ
    for i, s in enumerate(samples):
        path = s["image_path"]
        text = s["text"].strip()
        try:
            base_bin = load_and_binarize_hp(path, img_h, img_w, thr=bin_thr)
        except Exception as e:
            print(f"[warn] skip {path}: {e}")
            continue

        lbl_ids = encode_text(text, max_len=max_len)
        all_imgs.append(base_bin)
        all_labels.append(lbl_ids.copy())
        all_texts.append(text)

    print(f"[aug] originals total: {len(all_imgs)}")

    # 2) БАНК ЧИСЕЛ + ЯКОРНЫЕ КООРДИНАТЫ
    (
        number_bank,
        slash_template,
        X,
        Y,
        anchor_lx1,
        anchor_rx0,
        anchor_sx0,
        anchor_sx1,
    ) = build_number_bank_and_xy(all_imgs, all_texts, slash_x_min, slash_x_max)

    if (
        not xy_cross
        or not number_bank
        or slash_template is None
        or anchor_lx1 is None
        or anchor_rx0 is None
        or anchor_sx0 is None
        or anchor_sx1 is None
    ):
        if xy_cross:
            print(
                "[aug] xy_cross requested, but bank/anchors incomplete -> only originals."
            )
        return all_imgs, all_labels

    # 3) ГЕНЕРАЦИЯ ВСЕХ КОМБИНАЦИЙ x/x, x/y, y/y, y/x
    H, W = img_h, img_w
    synth_cnt = 0

    def render_pair(left_num: str, right_num: str) -> Optional[np.ndarray]:
        if left_num not in number_bank or right_num not in number_bank:
            return None
        left_crop = random.choice(number_bank[left_num])
        right_crop = random.choice(number_bank[right_num])

        # нормализуем высоту
        def norm_h(crop: np.ndarray) -> np.ndarray:
            if crop.shape[0] == H:
                return crop
            new_w = int(round(crop.shape[1] * H / crop.shape[0]))
            return cv2.resize(crop, (new_w, H), interpolation=cv2.INTER_NEAREST)

        left_crop = norm_h(left_crop)
        right_crop = norm_h(right_crop)
        slash_crop = norm_h(slash_template)

        canvas = np.zeros((H, W), np.uint8)

        # --- ЛЕВОЕ ЧИСЛО: заканчивается в anchor_lx1 ---
        lw = left_crop.shape[1]
        lx1 = anchor_lx1
        lx0 = max(0, lx1 - lw + 1)
        if lx0 >= W:
            return None

        # --- СЛЕШ: ставим в anchor_sx0..sx1 ---
        sx0 = anchor_sx0
        sx1 = anchor_sx1
        sw_target = sx1 - sx0 + 1
        if sw_target <= 0:
            return None

        if slash_crop.shape[1] != sw_target:
            slash_crop = cv2.resize(
                slash_crop, (sw_target, H), interpolation=cv2.INTER_NEAREST
            )

        if sx0 < 0 or sx0 >= W:
            return None
        sx1 = min(W - 1, sx0 + slash_crop.shape[1] - 1)

        # --- ПРАВОЕ ЧИСЛО: начинается в anchor_rx0 ---
        rw = right_crop.shape[1]
        rx0 = anchor_rx0
        if rx0 >= W:
            return None
        if rx0 + rw > W:
            # чуть подожмём вправо, если не влезает
            rx0 = max(0, W - rw)

        # проверка на порядок (на всякий случай)
        if not (lx0 <= lx1 < sx0 <= sx1 < rx0 + rw):
            # если совсем всё плохо — просто не добавляем
            return None

        # укладка
        canvas[:, lx0 : lx0 + lw] = np.maximum(canvas[:, lx0 : lx0 + lw], left_crop)
        canvas[:, sx0 : sx0 + slash_crop.shape[1]] = np.maximum(
            canvas[:, sx0 : sx0 + slash_crop.shape[1]], slash_crop
        )
        canvas[:, rx0 : rx0 + rw] = np.maximum(canvas[:, rx0 : rx0 + rw], right_crop)

        return canvas

    # полное N*N по X,Y и комбинации x/x, x/y, y/y, y/x

    for x in X:
        for y in Y:
            combos = [
                (x, x),
                (x, y),
                (y, y),
                (y, x),
            ]
            for ln, rn in combos:
                label_str = f"{ln}/{rn}"
                canvas = render_pair(ln, rn)
                if canvas is None:
                    continue
                lbl_ids = encode_text(label_str, max_len=max_len)
                all_imgs.append(canvas)
                all_labels.append(lbl_ids)
                synth_cnt += 1

    print(f"[aug] xy_cross synthetic added: {synth_cnt}")
    print(f"[aug] total samples: {len(all_imgs)}")

    return all_imgs, all_labels


# ============================================================
# 9. Тренировка с auto-resume
# ============================================================


def train_hp_seq_all(
    ls_json: str,
    project_root: str,
    img_h: int = 32,
    img_w: int = 128,
    max_len: int = 8,
    batch_size: int = 128,
    epochs: int = 15,
    lr: float = 1e-3,
    bin_thr: Optional[int] = 200,
    val_split: float = 0.15,
    seed: int = 1337,
    out_dir: str = "runs/hp_seq",
    auto_resume: bool = True,
    dropout: float = 0.1,
    slash_x_min: int = 250,
    slash_x_max: int = 290,
    xy_cross: bool = True,
):
    set_seed(seed)

    print(f"[i] parsing LS export: {ls_json}")
    samples = parse_hp_ls_export(
        ls_json, project_root=project_root, textarea_name="hp_text"
    )
    print(f"[i] got {len(samples)} labeled HP bars")

    if not samples:
        raise RuntimeError("Нет размеченных сэмплов в LS JSON")

    print("[i] building dataset (originals + xy_cross if enabled)...")
    imgs, labels = build_full_augmented_dataset(
        samples,
        img_h=img_h,
        img_w=img_w,
        max_len=max_len,
        bin_thr=bin_thr,
        slash_x_min=slash_x_min,
        slash_x_max=slash_x_max,
        xy_cross=xy_cross,
    )
    ds_full = HpSeqOfflineDataset(imgs, labels)

    n_total = len(ds_full)
    n_val = max(1, int(val_split * n_total))
    n_train = n_total - n_val
    g = torch.Generator().manual_seed(seed)
    ds_train, ds_val = random_split(ds_full, [n_train, n_val], generator=g)
    print(f"[i] train_samples={n_train}  val_samples={n_val}")

    train_loader = DataLoader(
        ds_train, batch_size=batch_size, shuffle=True, num_workers=0, pin_memory=True
    )
    val_loader = DataLoader(
        ds_val, batch_size=batch_size, shuffle=False, num_workers=0, pin_memory=True
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[i] device={device}")

    net = HudHPSeqNet(
        in_ch=1, img_h=img_h, img_w=img_w, max_len=max_len, dropout=dropout
    ).to(device)

    crit = nn.CrossEntropyLoss(reduction="none")
    PAD_IDX = CHAR2IDX[PAD_TOKEN]
    pad_loss_weight = 1.0
    opt = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.ReduceLROnPlateau(
        opt, mode="min", factor=0.5, patience=5, min_lr=1e-5
    )
    use_amp = False
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)

    best_val_loss = float("inf")
    best_seq_acc = -1.0
    os.makedirs(out_dir, exist_ok=True)
    best_path = os.path.join(out_dir, "best.pt")
    last_path = os.path.join(out_dir, "last.pt")

    start_epoch = 1

    # ---------- AUTO-RESUME ----------
    if auto_resume and os.path.isfile(last_path):
        ckpt = torch.load(last_path, map_location="cpu")
        try:
            net.load_state_dict(ckpt["model"])
            opt.load_state_dict(ckpt["opt"])
            if "sched" in ckpt:
                sched.load_state_dict(ckpt["sched"])
            if "scaler" in ckpt and use_amp and ckpt["scaler"] is not None:
                scaler.load_state_dict(ckpt["scaler"])
            start_epoch = ckpt.get("epoch", 0) + 1
            best_val_loss = ckpt.get("best_val_loss", best_val_loss)
            best_seq_acc = ckpt.get("best_seq_acc", best_seq_acc)

            assert (
                ckpt["img_h"] == img_h
                and ckpt["img_w"] == img_w
                and ckpt["max_len"] == max_len
            ), "Параметры размера изображения/длины не совпадают с чекпоинтом"
            assert (
                ckpt["vocab"] == VOCAB and ckpt["pad_token"] == PAD_TOKEN
            ), "VOCAB/PAD в чекпоинте не совпадает"

            print(
                f"[i] auto-resume: эпоха {start_epoch} "
                f"(best_val_loss={best_val_loss:.4f}, best_seq_acc={best_seq_acc:.4f})"
            )
        except Exception as e:
            print(f"[warn] не удалось восстановить обучение: {e}")
            print("[warn] начнём с нуля.")

    pad_idx = CHAR2IDX[PAD_TOKEN]

    @torch.no_grad()
    def run_val(pad_idx: int):
        net.eval()
        val_loss_sum = 0.0
        correct = 0
        total = 0
        seq_correct = 0
        val_iter = tqdm(val_loader, desc=f"Train epoch {epoch}/{epochs}", leave=False)
        for x, y in val_iter:
            x = x.to(device)
            y = y.to(device)

            logits = net(x)
            B, T, C = logits.shape

            logits2d = logits.view(B * T, C)
            y2d = y.view(B * T)
            loss_all = crit(logits2d, y2d)
            w = torch.ones_like(y2d, dtype=torch.float32)
            w = torch.where(y2d == PAD_IDX, torch.full_like(w, pad_loss_weight), w)
            loss = (loss_all * w).sum() / w.sum().clamp_min(1.0)
            val_loss_sum += loss.item() * x.size(0)

            pred = logits.argmax(dim=2)
            mask2 = y != pad_idx
            correct += (pred.eq(y) & mask2).sum().item()
            total += mask2.sum().item()
            seq_ok_batch = ((pred == y) | (~mask2)).all(dim=1)
            seq_correct += seq_ok_batch.sum().item()

        val_loss = val_loss_sum / len(val_loader.dataset)
        char_acc = correct / max(1, total)
        seq_acc = seq_correct / len(val_loader.dataset)
        return val_loss, char_acc, seq_acc

    for epoch in range(start_epoch, epochs + 1):

        net.train()
        tr_loss_sum = 0.0
        seen = 0

        train_iter = tqdm(
            train_loader, desc=f"Train epoch {epoch}/{epochs}", leave=False
        )

        for x, y in train_iter:
            x = x.to(device)
            y = y.to(device)

            logits = net(x)
            B, T, C = logits.shape
            logits2d = logits.view(B * T, C)
            y2d = y.view(B * T)

            loss_all = crit(logits2d, y2d)
            w = torch.ones_like(y2d, dtype=torch.float32)
            w = torch.where(y2d == PAD_IDX, torch.full_like(w, pad_loss_weight), w)
            loss = (loss_all * w).sum() / w.sum().clamp_min(1.0)

            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            opt.zero_grad(set_to_none=True)

            bs = x.size(0)
            tr_loss_sum += loss.item() * bs
            seen += bs

        tr_loss = tr_loss_sum / max(1, seen)

        val_loss, char_acc, seq_acc = run_val(pad_idx)
        sched.step(val_loss)

        print(
            f"[epoch {epoch}] "
            f"train_loss={tr_loss:.4f}  val_loss={val_loss:.4f}  "
            f"char_acc={char_acc:.4f}  seq_acc={seq_acc:.4f}  lr={sched.get_last_lr()[0]:.6f}"
        )

        is_better = (seq_acc > best_seq_acc) or (
            seq_acc == best_seq_acc and val_loss < best_val_loss
        )
        if is_better:
            best_seq_acc = seq_acc
            best_val_loss = val_loss
            torch.save(
                {
                    "model": net.state_dict(),
                    "vocab": VOCAB,
                    "pad_token": PAD_TOKEN,
                    "img_h": img_h,
                    "img_w": img_w,
                    "max_len": max_len,
                    "best_val_loss": best_val_loss,
                    "best_seq_acc": best_seq_acc,
                },
                best_path,
            )
            print(
                f"[i] saved best (seq_acc={best_seq_acc:.4f}, val_loss={best_val_loss:.4f}) -> {best_path}"
            )

        torch.save(
            {
                "model": net.state_dict(),
                "opt": opt.state_dict(),
                "sched": sched.state_dict(),
                "scaler": scaler.state_dict() if use_amp else None,
                "epoch": epoch,
                "best_val_loss": best_val_loss,
                "best_seq_acc": best_seq_acc,
                "vocab": VOCAB,
                "pad_token": PAD_TOKEN,
                "img_h": img_h,
                "img_w": img_w,
                "max_len": max_len,
            },
            last_path,
        )

    print("[i] training finished.")


# ============================================================
# 10. Быстрый инференс одной картинки
# ============================================================


# ============================================================
# 11. CLI
# ============================================================


def main():
    ap = argparse.ArgumentParser(
        "Train HP HUD OCR with X/X, X/Y, Y/Y, Y/X synthesis + auto-resume"
    )

    ap.add_argument(
        "--ls_json",
        type=str,
        required=True,
        help="Путь к экспортированному JSON из Label Studio (hp_bar проект)",
    )
    ap.add_argument(
        "--project_root",
        type=str,
        default="",
        help="Корень проекта (если в LS пути были относительные через /data/local-files/?d=...)",
    )

    ap.add_argument("--img_h", type=int, default=56)
    ap.add_argument("--img_w", type=int, default=520)
    ap.add_argument("--max_len", type=int, default=9)
    ap.add_argument("--dropout", type=float, default=0.2)

    ap.add_argument("--batch_size", type=int, default=128)
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--lr", type=float, default=1e-3)

    ap.add_argument(
        "--bin_thr",
        type=int_or_none,
        default=200,
        help="Порог для бинаризации (0..255). Укажи None для Otsu.",
    )
    ap.add_argument("--val_split", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--out_dir", type=str, default="runs/hp_seq")
    ap.add_argument(
        "--auto_resume",
        type=int,
        default=1,
        help="1=включить авто-продолжение обучения с last.pt, 0=выключить",
    )

    ap.add_argument(
        "--slash_x_min",
        type=int,
        default=250,
        help="Минимальный X центра '/' (в resized картинке)",
    )
    ap.add_argument(
        "--slash_x_max",
        type=int,
        default=290,
        help="Максимальный X центра '/' (в resized картинке)",
    )

    ap.add_argument(
        "--xy_cross",
        type=int,
        default=1,
        help="1=генерить все комбинации X/X, X/Y, Y/Y, Y/X; 0=только оригиналы",
    )

    args = ap.parse_args()

    train_hp_seq_all(
        ls_json=args.ls_json,
        project_root=args.project_root,
        img_h=args.img_h,
        img_w=args.img_w,
        max_len=args.max_len,
        batch_size=args.batch_size,
        epochs=args.epochs,
        lr=args.lr,
        bin_thr=args.bin_thr,
        val_split=args.val_split,
        seed=args.seed,
        out_dir=args.out_dir,
        auto_resume=bool(args.auto_resume),
        dropout=args.dropout,
        slash_x_min=args.slash_x_min,
        slash_x_max=args.slash_x_max,
        xy_cross=bool(args.xy_cross),
    )


if __name__ == "__main__":

    main()
