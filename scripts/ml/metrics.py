from train_hp_seq_all import *
from train_minimap_heatmap import *
@torch.no_grad()
def infer_one_hp(path, ckpt="runs/hp_seq/best.pt", bin_thr: Optional[int] = 200, device=None):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    d = torch.load(ckpt, map_location="cpu")
    vocab = d["vocab"]; pad = d["pad_token"]
    idx2char = {i:c for i,c in enumerate(vocab)}
    H, W, T = d["img_h"], d["img_w"], d["max_len"]
    net = HudHPSeqNet(in_ch=1, img_h=H, img_w=W, max_len=T)
    net.load_state_dict(d["model"])
    net.eval().to(device)

    img = load_and_binarize_hp(path, H, W, thr=bin_thr).astype(np.float32)/255.0
    x = torch.from_numpy(img).unsqueeze(0).unsqueeze(0).to(device)  # 1x1xHxW
    logits = net(x)                                                # 1 x T x C
    pred = logits.argmax(-1).squeeze(0).cpu().numpy().tolist()
    s = "".join(idx2char[i] for i in pred if idx2char[i] != pad)
    return s
@torch.no_grad()
def infer_one_minimap(net: nn.Module, img_rgb: np.ndarray, size: int, device: str = "cpu") -> np.ndarray:
    """ img_rgb: HxWx3 RGB uint8 return: CxHxW prob heatmaps (float32 0..1) в том же размере size x size """
    img_resized = cv2.resize(img_rgb, (size, size), interpolation=cv2.INTER_AREA)
    x = to_tensor(img_resized).unsqueeze(0).to(device)
    logits = net(x)
    prob = torch.sigmoid(logits).squeeze(0).cpu().numpy()
    return prob # CxHxW
def find_peaks_per_channel(prob: np.ndarray, thr: float = 0.4, nms_kernel: int = 5) -> Dict[int, List[Tuple[float, float, float]]]:
    C, H, W = prob.shape
    out: Dict[int, List[Tuple[float, float, float]]] = {}
    for c in range(C):
        p = prob[c]
        k = nms_kernel
        pad = k // 2
        p_pad = np.pad(p, ((pad, pad), (pad, pad)), mode="edge")
        pooled = np.maximum.reduce([p_pad[i:i + H, j:j + W] for i in range(k) for j in range(k)])
        keep = (p >= thr) & (p >= pooled)
        ys, xs = np.where(keep)
        pts = [(float(x / W), float(y / H), float(p[y, x])) for (y, x) in zip(ys, xs)]
        pts.sort(key=lambda t: t[2], reverse=True)
        out[c] = pts
    return out