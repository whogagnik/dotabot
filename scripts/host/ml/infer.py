# scripts/ml/infer_all.py
# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import Optional, Union, Dict, List, Tuple, cast

import cv2
import numpy as np
import torch
import torch.nn as nn

from scripts.host.ml.train_hp_seq import HudHPSeqNet, load_and_binarize_hp
from scripts.host.ml.models import ResUNetLite

DeviceLike = Union[str, torch.device, None]


def resolve_device(device: DeviceLike = None) -> torch.device:
    """
    Правило простое:
      - если CUDA доступна -> ВСЕГДА cuda
      - если ты ЯВНО попросил cpu -> cpu
      - иначе -> cuda если есть, иначе cpu
    """
    cuda_ok = torch.cuda.is_available()

    # ЯВНЫЙ CPU
    if device is not None:
        if isinstance(device, torch.device):
            if device.type == "cpu":
                return torch.device("cpu")
        else:
            s = str(device).strip().lower()
            if s == "cpu":
                return torch.device("cpu")

    # ВСЕ ОСТАЛЬНОЕ -> CUDA если можно
    if cuda_ok:
        if device is not None and not isinstance(device, torch.device):
            s = str(device).strip().lower()
            if s.startswith("cuda:"):
                return torch.device(s)
        if isinstance(device, torch.device) and device.type == "cuda":
            return device
        return torch.device("cuda")

    return torch.device("cpu")


def _assert_same_device(net: nn.Module, x: torch.Tensor) -> None:
    """Жесткая проверка, чтобы не было 'думаю cuda, а на деле cpu'."""
    pdev = next(net.parameters()).device
    if pdev != x.device:
        raise RuntimeError(f"Device mismatch: model on {pdev}, input on {x.device}")


def to_tensor(img_rgb: np.ndarray) -> torch.Tensor:
    """RGB HxWx3 uint8 -> float32 tensor 3xHxW 0..1"""
    return torch.from_numpy(img_rgb).permute(2, 0, 1).float() / 255.0


# ============================================================
# HP OCR: load once + infer many times
# ============================================================


def load_hp_ocr_net(
    ckpt: str = "runs/hp_seq/best.pt",
    device: DeviceLike = None,
) -> nn.Module:
    """
    Загружает HP OCR модель ОДИН РАЗ.
    Возвращает net, у которого проставлены атрибуты:
      - _hp_idx2char: Dict[int,str]
      - _hp_pad_token: str
      - _hp_H: int
      - _hp_W: int
      - _hp_T: int
    """
    dev = resolve_device(device)

    d = torch.load(ckpt, map_location="cpu")
    vocab = d["vocab"]
    pad_token = d["pad_token"]
    idx2char = {i: c for i, c in enumerate(vocab)}
    H, W, T = int(d["img_h"]), int(d["img_w"]), int(d["max_len"])

    net = HudHPSeqNet(in_ch=1, img_h=H, img_w=W, max_len=T)
    net.load_state_dict(d["model"])
    net.eval().to(dev)

    setattr(net, "_hp_idx2char", idx2char)
    setattr(net, "_hp_pad_token", pad_token)
    setattr(net, "_hp_H", H)
    setattr(net, "_hp_W", W)
    setattr(net, "_hp_T", T)

    return net


def _hp_meta(net: nn.Module) -> Tuple[Dict[int, str], str, int, int]:
    if not all(
        hasattr(net, k) for k in ("_hp_idx2char", "_hp_pad_token", "_hp_H", "_hp_W")
    ):
        raise RuntimeError(
            "HP net has no attached meta. Create it via load_hp_ocr_net(ckpt, device)."
        )
    idx2char = cast(Dict[int, str], getattr(net, "_hp_idx2char"))
    pad_token = cast(str, getattr(net, "_hp_pad_token"))
    H = int(getattr(net, "_hp_H"))
    W = int(getattr(net, "_hp_W"))
    return idx2char, pad_token, H, W


@torch.no_grad()
def infer_one_hp(
    net: nn.Module,
    path: str,
    *,
    bin_thr: Optional[int] = 200,
    device: DeviceLike = None,
    debug: bool = False,
) -> str:
    """
    Инференс OCR HP строки:
      - net передаётся снаружи (чекпоинт не грузим)
      - декодинг берём из net._hp_* (проставлено в load_hp_ocr_net)
    """
    dev = resolve_device(device)
    idx2char, pad_token, H, W = _hp_meta(net)

    net = net.eval().to(dev)

    img = load_and_binarize_hp(path, H, W, thr=bin_thr).astype(np.float32) / 255.0
    x = torch.from_numpy(img).unsqueeze(0).unsqueeze(0).to(dev)  # 1x1xHxW

    _assert_same_device(net, x)

    logits = net(x)  # 1 x T x C
    pred = logits.argmax(-1).squeeze(0).detach().cpu().numpy().tolist()
    s = "".join(idx2char[i] for i in pred if idx2char[i] != pad_token)
    return s


# ============================================================
# Minimap: load model like in train_minimap_heatmap.py
# ============================================================


@torch.no_grad()
def load_minimap_model(
    ckpt_path: str,
    device: Optional[str] = None,
):
    """
    Полностью как в train_minimap_heatmap.py::load_model:
      return net, classes, size
    """
    ckpt = torch.load(ckpt_path, map_location=device)
    classes = ckpt.get("classes", ["self", "ally", "enemy"])
    size = ckpt.get("size", 128)
    base = ckpt.get("base", 32)

    net = ResUNetLite(in_ch=3, base=base, out_ch=len(classes))
    net.load_state_dict(ckpt["model"])

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    net.to(device).eval()

    return net, classes, size


@torch.no_grad()
def infer_one_minimap(
    net: nn.Module,
    img_rgb: np.ndarray,
    size: int,
    device: DeviceLike = None,
) -> np.ndarray:
    """
    img_rgb: HxWx3 RGB uint8
    return: CxSxS prob heatmaps (float32 0..1) в размере size x size
    """
    dev = resolve_device(device)

    img_resized = cv2.resize(img_rgb, (size, size), interpolation=cv2.INTER_AREA)
    x = to_tensor(img_resized).unsqueeze(0).to(dev)  # 1x3xSxS

    net = net.eval().to(dev)

    _assert_same_device(net, x)

    logits = net(x)
    prob = torch.sigmoid(logits).squeeze(0).detach().cpu().numpy().astype(np.float32)
    return prob


def find_peaks_per_channel(
    prob: np.ndarray,
    thr: float = 0.4,
    nms_kernel: int = 5,
) -> Dict[int, List[Tuple[float, float, float]]]:
    """
    prob: CxHxW (float32 0..1)
    Возвращает: channel -> [(x_norm, y_norm, score), ...] sorted desc
    """
    C, H, W = prob.shape
    out: Dict[int, List[Tuple[float, float, float]]] = {}

    k = int(nms_kernel)
    pad = k // 2

    for c in range(C):
        p = prob[c]
        p_pad = np.pad(p, ((pad, pad), (pad, pad)), mode="edge")
        pooled = np.maximum.reduce(
            [p_pad[i : i + H, j : j + W] for i in range(k) for j in range(k)]
        )
        keep = (p >= float(thr)) & (p >= pooled)
        ys, xs = np.where(keep)

        pts = [(float(x / W), float(y / H), float(p[y, x])) for (y, x) in zip(ys, xs)]
        pts.sort(key=lambda t: t[2], reverse=True)
        out[c] = pts

    return out
