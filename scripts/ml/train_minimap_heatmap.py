# train_minimap_heatmap.py
# -*- coding: utf-8 -*-

import os
import json
import glob
import uuid
import random
import argparse
import datetime
from typing import List, Dict, Tuple, Any, Optional

import numpy as np
import cv2
from tqdm import tqdm

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

from metrics import find_peaks_per_channel

# ---------------------------
# Utils
# ---------------------------

def set_seed(seed: int = 1337):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def imread_rgb(path: str) -> np.ndarray:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(path)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def to_tensor(img_rgb: np.ndarray) -> torch.Tensor:
    """RGB HxWx3 uint8 -> float32 tensor 3xHxW 0..1"""
    return torch.from_numpy(img_rgb).permute(2, 0, 1).float() / 255.0


# ---------------------------
# Label Studio parsing
# ---------------------------

"""
Ожидаем экспорт Label Studio JSON (Tasks), где каждая задача имеет:
{
  "data": { "image": "<путь или url>" },
  "file_upload": "xxxx-mm_000123.png",
  "annotations": [
     {
       "result": [
         {
           "value": {
             "x": <0..100>, "y": <0..100>,
             "width": <0..100>, "height": <0..100>,
             "rectanglelabels": ["ally"]  # или "self"/"enemy"
           },
           "type":"rectanglelabels", ...
         },
         ...
       ]
     }
  ]
}
Координаты x,y,width,height в ПРОЦЕНТАХ относительно исходного изображения.
"""


def _parse_dt_maybe(v: Optional[str]) -> Optional[datetime.datetime]:
    if not v or not isinstance(v, str):
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.datetime.strptime(v, fmt)
        except Exception:
            pass
    return None


def parse_ls_export(json_path: str,
                    classes: List[str],
                    image_root: Optional[str] = None,
                    strict_exists: bool = True,
                    min_results: int = 1) -> List[Dict[str, Any]]:
    """
    Возвращает список сэмплов:
      {
        "image_path": <resolved or original>,
        "file_upload": <str|None>,
        "boxes": [ {cls,x,y,w,h}, ... ],
        "exists": bool,
        "total_annotations": int,
        "picked_ann_meta": { ... }
      }

    Политика выбора аннотации:
      - валидная: ann с result!=[], и без was_cancelled
      - берём ПОСЛЕДНЮЮ по updated_at; если пусто — по created_at; если пусто у всех — последнюю в массиве
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cls_set = set(classes)
    items: List[Dict[str, Any]] = []

    def _resolve_path(img_path: Optional[str], file_upload: Optional[str]) -> Tuple[Optional[str], bool]:
        p = (img_path or "").strip()
        fu = (file_upload or "").strip()
        root = (image_root or "").strip()

        cands: List[str] = []

        # 1) /data/local-files/?d=...
        if root and p.startswith("/data/local-files/?d="):
            rel = p.split("?d=")[-1].lstrip("/\\")
            cands.append(os.path.join(root, rel))

        # 2) /data/upload/... -> root/file_upload (или basename)
        if root and p.startswith("/data/upload/"):
            name = fu or os.path.basename(p)
            if name:
                cands.append(os.path.join(root, name))

        # 3) как есть
        if p:
            cands.append(p)

        # 4) root + basename(image)
        if root and p:
            base = os.path.basename(p)
            if base:
                cands.append(os.path.join(root, base))

        # 5) root + file_upload
        if root and fu:
            cands.append(os.path.join(root, fu))

        # 6) обрезка префикса-хэша "xxxx-" -> root/suffix, и glob по суффиксу
        def add_suffix_variants(name: str):
            if not root or not name:
                return
            if "-" in name:
                suffix = name.split("-", 1)[1]
                cands.append(os.path.join(root, suffix))
                cands.extend(glob.glob(os.path.join(root, "**", suffix), recursive=True))

        add_suffix_variants(fu)
        if p:
            add_suffix_variants(os.path.basename(p))

        for c in cands:
            c_norm = os.path.normpath(c)
            if os.path.exists(c_norm):
                return c_norm, True

        best = os.path.normpath(cands[0]) if cands else None
        return best, False

    def _collect_valid_annotations(task: dict) -> List[dict]:
        anns = task.get("annotations") or task.get("completions") or []
        out = []
        for ann in anns:
            if ann.get("was_cancelled"):
                continue
            res = ann.get("result") or []
            if not res:
                continue
            out.append(ann)
        return out

    def _pick_latest_annotation(valid_anns: List[dict]) -> Optional[dict]:
        if not valid_anns:
            return None

        def sort_key(a: dict):
            upd = _parse_dt_maybe(a.get("updated_at")) or datetime.datetime.min
            crt = _parse_dt_maybe(a.get("created_at")) or datetime.datetime.min
            return (upd, crt)

        try:
            return sorted(valid_anns, key=sort_key)[-1]
        except Exception:
            return valid_anns[-1]

    for task in data:
        data_field = task.get("data", {}) or {}
        img_path = data_field.get("image") or data_field.get("img") or data_field.get("path")
        file_upload = task.get("file_upload")

        valid_anns = _collect_valid_annotations(task)
        total_annotations = len(valid_anns)
        if total_annotations < min_results:
            continue

        picked = _pick_latest_annotation(valid_anns)
        result = (picked or {}).get("result") or []

        resolved, exists = _resolve_path(img_path, file_upload)
        if strict_exists and not exists:
            continue

        boxes = []
        for r in result:
            if (r.get("type") or "").lower() != "rectanglelabels":
                continue
            v = r.get("value") or {}
            labels = v.get("rectanglelabels") or v.get("labels") or []
            if not labels:
                continue
            lab = labels[0]
            if lab not in cls_set:
                continue
            try:
                x = float(v["x"]); y = float(v["y"])
                w = float(v["width"]); h = float(v["height"])
            except Exception:
                continue
            boxes.append({"cls": lab, "x": x, "y": y, "w": w, "h": h})

        item = {
            "image_path": resolved if resolved else (img_path or ""),
            "file_upload": file_upload,
            "boxes": boxes,
            "exists": bool(exists),
            "total_annotations": total_annotations,
        }
        if picked:
            item["picked_ann_meta"] = {
                "updated_at": picked.get("updated_at"),
                "created_at": picked.get("created_at"),
                "ground_truth": picked.get("ground_truth"),
                "id": picked.get("id"),
                "completed_by": picked.get("completed_by"),
            }

        items.append(item)

    return items


# ---------------------------
# Dataset
# ---------------------------

class MinimapHeatmapDataset(Dataset):
    """
    Аугментации ТОЛЬКО:
      - synthetic rotations (0/90/180/270) через rot_k (если synthetic=True)
      - pixel shift dx,dy in [-shift_max, shift_max] (только если augment=True)
    Никаких микроротаций, блюра, шума, изменения яркости и т.д.

    Debug:
      - если debug_dump_dir задан, то сохраняет КАЖДЫЙ сэмпл (после resize+shift) в PNG + JSON с боксами
    """
    def __init__(self,
                 samples: List[Dict[str, Any]],
                 classes: List[str],
                 out_size: int = 128,
                 augment: bool = False,
                 synthetic: bool = False,
                 shift_max: int = 50,
                 shift_prob: float = 1.0,
                 shift_border_value: int = 0,
                 drop_tiny_boxes_px: float = 1.0,
                 debug_dump_dir: Optional[str] = None,
                 debug_dump_limit: int = -1):
        self.samples = samples
        self.classes = classes
        self.cls2idx = {c: i for i, c in enumerate(classes)}
        self.out_size = out_size
        self.augment = augment
        self.synthetic = synthetic

        self.shift_max = int(shift_max)
        self.shift_prob = float(shift_prob)
        self.shift_border_value = int(shift_border_value)
        self.drop_tiny_boxes_px = float(drop_tiny_boxes_px)

        self.debug_dump_dir = debug_dump_dir
        self.debug_dump_limit = int(debug_dump_limit)
        self._dump_count = 0
        if self.debug_dump_dir:
            os.makedirs(self.debug_dump_dir, exist_ok=True)

    def __len__(self):
        return len(self.samples) * 4 if self.synthetic else len(self.samples)

    def _rotate_boxes_ccw(self, boxes: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
        """Поворот боксов на k * 90° CCW (как np.rot90). Координаты в процентах."""
        if k % 4 == 0:
            return [dict(b) for b in boxes]

        k = k % 4
        out = []
        for b in boxes:
            x = b["x"] / 100.0
            y = b["y"] / 100.0
            w = b["w"] / 100.0
            h = b["h"] / 100.0

            cx = x + 0.5 * w
            cy = y + 0.5 * h

            if k == 1:  # 90 CCW
                cx2 = cy
                cy2 = 1.0 - cx
                w2, h2 = h, w
            elif k == 2:  # 180
                cx2 = 1.0 - cx
                cy2 = 1.0 - cy
                w2, h2 = w, h
            else:  # k == 3: 270 CCW
                cx2 = 1.0 - cy
                cy2 = cx
                w2, h2 = h, w

            x2 = cx2 - 0.5 * w2
            y2 = cy2 - 0.5 * h2

            nb = dict(b)
            nb["x"] = x2 * 100.0
            nb["y"] = y2 * 100.0
            nb["w"] = w2 * 100.0
            nb["h"] = h2 * 100.0
            out.append(nb)
        return out

    def _shift_img_and_boxes(self,
                             img: np.ndarray,
                             boxes: List[Dict[str, Any]],
                             dx: int,
                             dy: int) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
        """
        img: HxWx3 uint8 (уже out_size x out_size)
        boxes: проценты 0..100
        dx,dy: пиксельный сдвиг (dx вправо, dy вниз)
        """
        H, W = img.shape[:2]

        M = np.array([[1, 0, dx],
                      [0, 1, dy]], dtype=np.float32)
        img_shift = cv2.warpAffine(
            img, M, (W, H),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(self.shift_border_value, self.shift_border_value, self.shift_border_value),
        )

        new_boxes: List[Dict[str, Any]] = []
        for b in boxes:
            x1 = (b["x"] / 100.0) * W
            y1 = (b["y"] / 100.0) * H
            x2 = x1 + (b["w"] / 100.0) * W
            y2 = y1 + (b["h"] / 100.0) * H

            x1 += dx; x2 += dx
            y1 += dy; y2 += dy

            nx1 = max(0.0, min(float(W), x1))
            ny1 = max(0.0, min(float(H), y1))
            nx2 = max(0.0, min(float(W), x2))
            ny2 = max(0.0, min(float(H), y2))

            if nx2 <= nx1 or ny2 <= ny1:
                continue

            if (nx2 - nx1) < self.drop_tiny_boxes_px or (ny2 - ny1) < self.drop_tiny_boxes_px:
                continue

            nb = dict(b)
            nb["x"] = (nx1 / W) * 100.0
            nb["y"] = (ny1 / H) * 100.0
            nb["w"] = ((nx2 - nx1) / W) * 100.0
            nb["h"] = ((ny2 - ny1) / H) * 100.0
            new_boxes.append(nb)

        return img_shift, new_boxes

    def _build_heatmap(self, H: int, W: int, boxes: List[Dict[str, Any]]) -> np.ndarray:
        """
        boxes: проценты (0..100). Теплокарта: CxHxW, максимум гауссиан по боксам класса.
        """
        C = len(self.classes)
        hm = np.zeros((C, H, W), dtype=np.float32)

        xs = np.linspace(0, 1, W, dtype=np.float32)[None, None, :]
        ys = np.linspace(0, 1, H, dtype=np.float32)[None, :, None]

        for b in boxes:
            ci = self.cls2idx[b["cls"]]
            u = (b["x"] + b["w"] * 0.5) / 100.0
            v = (b["y"] + b["h"] * 0.5) / 100.0
            uw = max(1e-3, b["w"] / 100.0)
            vh = max(1e-3, b["h"] / 100.0)

            sigma_u = max(2.5 / W, uw * 0.8)
            sigma_v = max(2.5 / H, vh * 0.8)

            du2 = (xs - u) ** 2 / (2 * (sigma_u ** 2))
            dv2 = (ys - v) ** 2 / (2 * (sigma_v ** 2))
            g = np.exp(-(du2 + dv2))
            hm[ci] = np.maximum(hm[ci], g)

        return hm

    def _dump_augmented(self,
                        img_rgb: np.ndarray,
                        boxes: List[Dict[str, Any]],
                        src_path: str,
                        rot_k: int,
                        dx: int,
                        dy: int):
        """Сохраняем PNG + JSON (boxes) в debug_dump_dir."""
        if not self.debug_dump_dir:
            return
        if self.debug_dump_limit >= 0 and self._dump_count >= self.debug_dump_limit:
            return

        base = os.path.splitext(os.path.basename(src_path))[0] if src_path else "img"
        uid = uuid.uuid4().hex[:10]
        name = f"{base}_rot{rot_k}_dx{dx}_dy{dy}_{uid}"

        bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        cv2.imwrite(os.path.join(self.debug_dump_dir, name + ".png"), bgr)

        meta = {"src": src_path, "rot_k": rot_k, "dx": dx, "dy": dy, "boxes": boxes}
        with open(os.path.join(self.debug_dump_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        self._dump_count += 1

    def __getitem__(self, idx: int):
        if self.synthetic:
            base_idx = idx // 4
            rot_k = idx % 4
        else:
            base_idx = idx
            rot_k = 0

        sample = self.samples[base_idx]
        img = imread_rgb(sample["image_path"])  # RGB

        # 1) 0/90/180/270
        if rot_k != 0:
            img = np.rot90(img, k=rot_k)

        boxes = sample["boxes"]
        if rot_k != 0:
            boxes = self._rotate_boxes_ccw(boxes, rot_k)

        # 2) resize to out_size
        img_resized = cv2.resize(img, (self.out_size, self.out_size), interpolation=cv2.INTER_AREA)

        # 3) pixel shift
        dx = 0
        dy = 0
        if self.augment and (self.shift_max > 0) and (random.random() < self.shift_prob):
            max_s = min(self.shift_max, self.out_size)
            dx = random.randint(-max_s, max_s)
            dy = random.randint(-max_s, max_s)
            img_resized, boxes = self._shift_img_and_boxes(img_resized, boxes, dx, dy)

        # 4) dump every augmented sample (optional)
        self._dump_augmented(
            img_rgb=img_resized,
            boxes=boxes,
            src_path=sample["image_path"],
            rot_k=rot_k,
            dx=dx,
            dy=dy
        )

        # 5) heatmap
        hm = self._build_heatmap(self.out_size, self.out_size, boxes)

        x = to_tensor(img_resized)
        y = torch.from_numpy(hm)

        meta = {"boxes": boxes, "image": sample["image_path"], "rot_k": rot_k, "dx": dx, "dy": dy}
        return x, y, meta


def collate_keep_meta(batch):
    xs, ys, metas = zip(*batch)
    return torch.stack(xs, 0), torch.stack(ys, 0), list(metas)


# ---------------------------
# Model
# ---------------------------

class MiniMapDet(nn.Module):
    def __init__(self, in_ch=3, base=32, out_ch=3, dropout: float = 0.0):
        super().__init__()

        def block(cin, cout, down=False):
            k, s, p = (3, 2, 1) if down else (3, 1, 1)
            return nn.Sequential(
                nn.Conv2d(cin, cout, k, s, p), nn.BatchNorm2d(cout), nn.ReLU(inplace=True),
                nn.Conv2d(cout, cout, 3, 1, 1), nn.ReLU(inplace=True),
            )

        self.enc1 = block(in_ch, base, down=False)
        self.enc2 = block(base, base * 2, down=True)
        self.enc3 = block(base * 2, base * 4, down=True)

        self.dec2 = nn.Sequential(
            nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
            nn.Conv2d(base * 4, base * 2, 3, 1, 1), nn.ReLU(inplace=True),
            nn.Dropout2d(dropout) if dropout > 0 else nn.Identity(),
        )
        self.dec1 = nn.Sequential(
            nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
            nn.Conv2d(base * 2, base, 3, 1, 1), nn.ReLU(inplace=True),
            nn.Dropout2d(dropout) if dropout > 0 else nn.Identity(),
        )
        self.head = nn.Conv2d(base, out_ch, 1)

    def forward(self, x):
        x1 = self.enc1(x)
        x2 = self.enc2(x1)
        x3 = self.enc3(x2)
        y2 = self.dec2(x3)
        y1 = self.dec1(y2)
        return self.head(y1)


class MultiHMLoss(nn.Module):
    """BCEWithLogits + Dice."""
    def __init__(self, pos_weight: Optional[torch.Tensor] = None, dice_w: float = 0.3, eps: float = 1e-6):
        super().__init__()
        self.bce = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
        self.dice_w = dice_w
        self.eps = eps

    def forward(self, logits, targets):
        bce = self.bce(logits, targets)
        prob = torch.sigmoid(logits)
        inter = (prob * targets).sum(dim=(2, 3))
        union = (prob + targets).sum(dim=(2, 3))
        dice = 1.0 - (2 * inter + self.eps) / (union + self.eps)
        dice = dice.mean()
        return (1 - self.dice_w) * bce + self.dice_w * dice


# ---------------------------
# Peak metrics & inference utils
# ---------------------------

def centers_from_boxes_percent(boxes: List[Dict[str, float]],
                               size: int,
                               cls2idx: Dict[str, int]) -> List[List[Tuple[float, float]]]:
    C = len(cls2idx)
    gts = [[] for _ in range(C)]
    for b in boxes:
        ci = cls2idx[b["cls"]]
        cx = (b["x"] + b["w"] * 0.5) / 100.0 * size
        cy = (b["y"] + b["h"] * 0.5) / 100.0 * size
        gts[ci].append((cx, cy))
    return gts


def match_peaks_to_gt(peaks_xy: List[Tuple[float, float]],
                      gts_xy: List[Tuple[float, float]],
                      radius: float = 6.0) -> Tuple[int, int, int]:
    used = set()
    TP = 0
    for x, y in peaks_xy:
        j_best, d_best = -1, 1e9
        for j, (gx, gy) in enumerate(gts_xy):
            if j in used:
                continue
            d = ((x - gx) ** 2 + (y - gy) ** 2) ** 0.5
            if d < d_best:
                d_best, j_best = d, j
        if d_best <= radius and j_best >= 0:
            TP += 1
            used.add(j_best)
    FP = max(0, len(peaks_xy) - TP)
    FN = max(0, len(gts_xy) - TP)
    return TP, FP, FN





def eval_peaks_batch(prob: np.ndarray,
                     metas: List[Dict[str, Any]],
                     classes: List[str],
                     thr: float = 0.35,
                     nms_kernel: int = 5,
                     radius: float = 6.0) -> Tuple[float, float]:
    B, C, H, W = prob.shape
    cls2idx = {c: i for i, c in enumerate(classes)}
    TP = FP = FN = 0
    for b in range(B):
        peaks_dict = find_peaks_per_channel(prob[b], thr=thr, nms_kernel=nms_kernel)
        gts = centers_from_boxes_percent(metas[b]["boxes"], size=H, cls2idx=cls2idx)
        for c in range(C):
            peaks_xy = [(u * W, v * H) for (u, v, _s) in peaks_dict.get(c, [])]
            TPc, FPc, FNc = match_peaks_to_gt(peaks_xy, gts[c], radius)
            TP += TPc; FP += FPc; FN += FNc
    P = TP / (TP + FP + 1e-9)
    R = TP / (TP + FN + 1e-9)
    return P, R


# ---------------------------
# Training
# ---------------------------

def train(args):
    set_seed(args.seed)

    classes = ["self", "ally", "enemy"]

    print(f"[i] Loading Label Studio export: {args.ls_json}")
    samples = parse_ls_export(args.ls_json, classes=classes, image_root=args.image_root, strict_exists=True)

    if not samples:
        dbg = parse_ls_export(args.ls_json, classes=classes, image_root=args.image_root, strict_exists=False)
        print(f"[debug] parsed={len(dbg)}, found_on_disk={sum(int(s['exists']) for s in dbg)}")
        for s in dbg[:5]:
            print(" image_path:", s["image_path"], "exists:", s["exists"], "file_upload:", s.get("file_upload"))
        raise RuntimeError("Нет валидных сэмплов. Проверь --image_root и пути.")

    random.shuffle(samples)
    n_total = len(samples)
    n_val = max(1, int(n_total * args.val_split))
    n_train = n_total - n_val
    train_samples = samples[:n_train]
    val_samples = samples[n_train:]

    debug_dump_dir = os.path.join(args.out_dir, "debug_dump") if args.debug_dump_aug else None

    train_ds = MinimapHeatmapDataset(
        train_samples, classes,
        out_size=args.size,
        augment=True,              # <-- включает shift
        synthetic=True,            # <-- 0/90/180/270
        shift_max=args.shift_max,
        shift_prob=args.shift_prob,
        shift_border_value=args.shift_border_value,
        debug_dump_dir=debug_dump_dir,
        debug_dump_limit=args.debug_dump_limit
    )

    val_ds = MinimapHeatmapDataset(
        val_samples, classes,
        out_size=args.size,
        augment=False,
        synthetic=False
    )

    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True, collate_fn=collate_keep_meta
    )
    val_loader = DataLoader(
        val_ds, batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True, collate_fn=collate_keep_meta
    )

    device = torch.device("cuda" if torch.cuda.is_available() and not args.cpu else "cpu")
    print(f"[i] Device: {device}")

    net = MiniMapDet(in_ch=3, base=args.base, out_ch=len(classes), dropout=args.dropout).to(device)
    criterion = MultiHMLoss(pos_weight=None, dice_w=args.dice_w).to(device)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, mode='min', patience=3, factor=0.5)

    os.makedirs(args.out_dir, exist_ok=True)
    best_val = 1e9
    best_p = 0.0
    best_r = 0.0
    best_path = os.path.join(args.out_dir, "best.pt")
    last_lr = None

    # resume
    start_epoch = 1
    if args.auto_resume and args.resume is None:
        maybe_best = os.path.join(args.out_dir, "best.pt")
        if os.path.exists(maybe_best):
            args.resume = maybe_best

    if args.resume:
        print(f"[i] Resuming from: {args.resume}")
        ckpt = torch.load(args.resume, map_location="cpu")

        ckpt_classes = ckpt.get("classes", classes)
        ckpt_size = ckpt.get("size", args.size)
        ckpt_base = ckpt.get("base", args.base)
        if args.strict_resume:
            if ckpt_classes != classes or ckpt_size != args.size or ckpt_base != args.base:
                raise RuntimeError(
                    f"Incompatible checkpoint: classes/size/base differ.\n"
                    f"ckpt: classes={ckpt_classes}, size={ckpt_size}, base={ckpt_base}\n"
                    f"args: classes={classes}, size={args.size}, base={args.base}"
                )

        missing, unexpected = net.load_state_dict(ckpt["model"], strict=False)
        if missing or unexpected:
            print(f"[warn] load_state_dict: missing={missing}, unexpected={unexpected}")

        best_val = ckpt.get("best_val", best_val)
        best_r = ckpt.get("best_r", best_r)
        best_p = ckpt.get("best_p", best_p)
        start_epoch = int(ckpt.get("epoch", 0)) + 1

        try:
            opt.load_state_dict(ckpt.get("optimizer", {}))
        except Exception as e:
            print(f"[warn] optimizer state not loaded: {e}")
        try:
            if hasattr(sched, "load_state_dict"):
                sched.load_state_dict(ckpt.get("scheduler", {}))
        except Exception as e:
            print(f"[warn] scheduler state not loaded: {e}")

        print(f"[i] Resume ok. start_epoch={start_epoch}, best_val={best_val:.4f}")

    # train loop
    for epoch in range(start_epoch, args.epochs + 1):
        net.train()
        tr_loss = 0.0

        for x, y, _meta in tqdm(train_loader, desc=f"Epoch {epoch}/{args.epochs} [train]"):
            x = x.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)

            logits = net(x)
            loss = criterion(logits, y)

            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), max_norm=5.0)
            opt.step()

            tr_loss += loss.item() * x.size(0)

        tr_loss /= len(train_loader.dataset)

        # validation
        net.eval()
        val_loss = 0.0
        P_pk_list, R_pk_list = [], []

        with torch.no_grad():
            for x, y, metas in tqdm(val_loader, desc=f"Epoch {epoch}/{args.epochs} [valid]"):
                x_dev = x.to(device, non_blocking=True)
                y_dev = y.to(device, non_blocking=True)

                logits = net(x_dev)
                loss = criterion(logits, y_dev)
                val_loss += loss.item() * x.size(0)

                prob_np = torch.sigmoid(logits).cpu().numpy()
                Ppk, Rpk = eval_peaks_batch(
                    prob_np, metas, classes,
                    thr=args.thr, nms_kernel=args.nms_kernel, radius=args.radius
                )
                P_pk_list.append(Ppk)
                R_pk_list.append(Rpk)

        val_loss /= len(val_loader.dataset)
        Ppk_mean = float(np.mean(P_pk_list)) if P_pk_list else 0.0
        Rpk_mean = float(np.mean(R_pk_list)) if R_pk_list else 0.0

        print(f"[epoch {epoch}] train_loss={tr_loss:.4f}  val_loss={val_loss:.4f}  "
              f"P_peaks={Ppk_mean:.3f} R_peaks={Rpk_mean:.3f}")

        # save best (по сумме P+R)
        if (best_r + best_p) < (Rpk_mean + Ppk_mean):
            best_val = val_loss
            best_r = Rpk_mean
            best_p = Ppk_mean
            ckpt = {
                "model": net.state_dict(),
                "classes": classes,
                "size": args.size,
                "base": args.base,
                "epoch": epoch,
                "best_val": best_val,
                "best_r": best_r,
                "best_p": best_p,
                "optimizer": opt.state_dict(),
                "scheduler": getattr(sched, "state_dict", lambda: {})(),
            }
            torch.save(ckpt, best_path)
            print(f"[i] saved best → {best_path}")

        sched.step(val_loss)
        current_lr = opt.param_groups[0]['lr']
        if last_lr is None or abs(current_lr - last_lr) > 1e-12:
            print(f"[lr] → {current_lr:.6g}")
            last_lr = current_lr

    final_path = os.path.join(args.out_dir, "last.pt")
    ckpt = {
        "model": net.state_dict(),
        "classes": classes,
        "size": args.size,
        "base": args.base,
        "epoch": args.epochs,
        "best_val": best_val,
        "optimizer": opt.state_dict(),
        "scheduler": getattr(sched, "state_dict", lambda: {})(),
    }
    torch.save(ckpt, final_path)
    print(f"[i] saved last → {final_path}")

# --------------------------- # Inference helper # ---------------------------
@torch.no_grad()
def load_model(ckpt_path: str, device: Optional[str] = None):
    ckpt = torch.load(ckpt_path, map_location=device)
    classes = ckpt.get("classes", ["self", "ally", "enemy"])
    size = ckpt.get("size", 128)
    base = ckpt.get("base", 32)
    net = MiniMapDet(in_ch=3, base=base, out_ch=len(classes))
    net.load_state_dict(ckpt["model"])
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    net.to(device).eval()
    return net, classes, size

# ---------------------------
# CLI
# ---------------------------

def build_argparser():
    ap = argparse.ArgumentParser("Train multi-heatmap detector for minimap (Label Studio rectangles)")
    ap.add_argument("--ls_json", type=str, required=True, help="Путь к экспортированному JSON из Label Studio")
    ap.add_argument("--image_root", type=str, default=None, help="Корень, где лежат PNG из экспорта (без префиксов)")
    ap.add_argument("--out_dir", type=str, default="runs/minimap", help="куда писать чекпоинты и debug")
    ap.add_argument("--size", type=int, default=128, help="размер входа / heatmap (например 100)")
    ap.add_argument("--batch_size", type=int, default=32)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--base", type=int, default=32)
    ap.add_argument("--val_split", type=float, default=0.15)
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--seed", type=int, default=1337)

    # loss/metrics
    ap.add_argument("--dice_w", type=float, default=0.3)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--thr", type=float, default=0.35)
    ap.add_argument("--radius", type=float, default=6.0)
    ap.add_argument("--nms_kernel", type=int, default=5)

    # shift augmentation (единственная аугментация кроме 90° rotations)
    ap.add_argument("--shift_max", type=int, default=50, help="dx,dy в [-shift_max, shift_max]")
    ap.add_argument("--shift_prob", type=float, default=1.0, help="вероятность применения shift на train")
    ap.add_argument("--shift_border_value", type=int, default=0, help="цвет заполнения при сдвиге (0..255)")

    # debug dump of ALL augmented images
    ap.add_argument("--debug_dump_aug", action="store_true",
                    help="Сохранять все аугментированные train-картинки в out_dir/debug_dump (PNG + JSON)")
    ap.add_argument("--debug_dump_limit", type=int, default=-1,
                    help="Лимит сохранённых картинок (-1 = без лимита)")

    # resume
    ap.add_argument("--resume", type=str, default=None)
    ap.add_argument("--auto_resume", action="store_true")
    ap.add_argument("--strict_resume", action="store_true")

    return ap


if __name__ == "__main__":
    args = build_argparser().parse_args()
    train(args)
