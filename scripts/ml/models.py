from scripts.ml.train_hp_seq import *
import torch.nn.functional as F

#for HudHPSeqNet
ALPHABET = "0123456789/"
PAD_TOKEN = "<PAD>"
VOCAB = list(ALPHABET) + [PAD_TOKEN]
CHAR2IDX = {c: i for i, c in enumerate(VOCAB)}
IDX2CHAR = {i: c for i, c in enumerate(VOCAB)}
NUM_CLASSES = len(VOCAB)


class HudHPSeqNet(nn.Module):
    """
    Простой CNN -> (B, T, C) для последовательности символов.
    """

    def __init__(
        self,
        in_ch: int = 1,
        channels=(32, 64, 128),
        kernel_size: int = 3,
        dropout: float = 0.15,
        norm: str | None = 'bn',
        gn_groups: int = 8,
        use_adapt: bool = True,
        adapt_out: tuple[int, int] = (2, 32),
        img_h: int = 23,
        img_w: int = 260,
        max_len: int = 9,
        fc_hidden: int | None = 128,
    ):
        super().__init__()
        self.max_len = max_len
        pad = kernel_size // 2

        def norm_layer(c: int) -> nn.Module:
            if norm is None:
                return nn.Identity()
            if norm.lower() == 'bn':
                return nn.BatchNorm2d(c)
            if norm.lower() == 'gn':
                g = min(max(1, gn_groups), c)
                return nn.GroupNorm(g, c)
            raise ValueError(f"Unknown norm='{norm}', use 'bn'|'gn'|None")

        blocks: list[nn.Module] = []
        in_c = in_ch
        for c in channels:
            blocks += [
                nn.Conv2d(in_c, c, kernel_size, padding=pad, bias=(norm is None)),
                norm_layer(c),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2, 2),
                nn.Dropout2d(p=dropout),
            ]
            in_c = c
        self.backbone = nn.Sequential(*blocks)

        self.use_adapt = bool(use_adapt)
        if self.use_adapt:
            self.adapt = nn.AdaptiveAvgPool2d(adapt_out)
            feat_dim = channels[-1] * adapt_out[0] * adapt_out[1]
        else:
            with torch.no_grad():
                dummy = torch.zeros(1, in_ch, img_h, img_w)
                feat = self._forward_features(dummy, do_adapt=False)
                feat_dim = feat.shape[1]

        if fc_hidden is not None and fc_hidden > 0:
            self.head = nn.Sequential(
                nn.Linear(feat_dim, fc_hidden),
                nn.ReLU(inplace=True),
                nn.Dropout(p=dropout),
                nn.Linear(fc_hidden, self.max_len * NUM_CLASSES),
            )
        else:
            self.head = nn.Linear(feat_dim, self.max_len * NUM_CLASSES)

    def _forward_features(self, x: torch.Tensor, do_adapt: bool | None = None) -> torch.Tensor:
        x = self.backbone(x)
        if (self.use_adapt if do_adapt is None else do_adapt):
            x = self.adapt(x)
        x = x.view(x.size(0), -1)
        return x

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        feat = self._forward_features(x)
        logits = self.head(feat).view(-1, self.max_len, NUM_CLASSES)
        return logits

class ResBlock(nn.Module):
    def __init__(self, in_ch: int, out_ch: int, stride: int = 1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_ch, out_ch, 3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, 3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(out_ch)

        self.proj = None
        if stride != 1 or in_ch != out_ch:
            self.proj = nn.Sequential(
                nn.Conv2d(in_ch, out_ch, 1, stride=stride, bias=False),
                nn.BatchNorm2d(out_ch),
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        identity = x
        out = F.relu(self.bn1(self.conv1(x)), inplace=True)
        out = self.bn2(self.conv2(out))
        if self.proj is not None:
            identity = self.proj(identity)
        out = F.relu(out + identity, inplace=True)
        return out


class UpBlock(nn.Module):
    def __init__(self, in_ch: int, skip_ch: int, out_ch: int):
        super().__init__()
        # после concat(in, skip) -> ResBlock
        self.block = ResBlock(in_ch + skip_ch, out_ch, stride=1)

    def forward(self, x: torch.Tensor, skip: torch.Tensor) -> torch.Tensor:
        x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear", align_corners=False)
        x = torch.cat([x, skip], dim=1)
        x = self.block(x)
        return x


class ResUNetLite(nn.Module):
    """
    Нормальный UNet-подобный сегментатор/heatmap детектор:
      - encoder: ResBlock + stride=2 downsample
      - decoder: bilinear upsample + skip connections
      - output: logits CxHxW


    """
    def __init__(self, in_ch: int = 3, base: int = 32, out_ch: int = 3, dropout: float = 0.0):
        super().__init__()
        self.stem = ResBlock(in_ch, base)

        self.enc1 = ResBlock(base, base * 2, stride=1)      # 1/2
        self.enc2 = ResBlock(base * 2, base * 4, stride=1)  # 1/4
        self.enc3 = ResBlock(base * 4, base * 8, stride=1)  # 1/8

        self.bottleneck = ResBlock(base * 8, base * 8, stride=1)

        self.dec3 = UpBlock(base * 8, base * 4, base * 4)
        self.dec2 = UpBlock(base * 4, base * 2, base * 2)
        self.dec1 = UpBlock(base * 2, base, base)

        self.drop = nn.Dropout2d(p=float(dropout)) if dropout and dropout > 0 else nn.Identity()
        self.head = nn.Conv2d(base, out_ch, kernel_size=1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        s0 = self.stem(x)      # base
        s1 = self.enc1(s0)     # base*2
        s2 = self.enc2(s1)     # base*4
        s3 = self.enc3(s2)     # base*8

        b = self.bottleneck(s3)

        x = self.dec3(b, s2)
        x = self.dec2(x, s1)
        x = self.dec1(x, s0)

        x = self.drop(x)
        logits = self.head(x)
        return logits


# -----------------------------------------
# MiniMapNet (оставляем для совместимости)
# -----------------------------------------
class ConvBNAct(nn.Module):
    def __init__(self, in_ch: int, out_ch: int, k: int = 3, s: int = 1, p: int = 1):
        super().__init__()
        self.conv = nn.Conv2d(in_ch, out_ch, k, stride=s, padding=p, bias=False)
        self.bn = nn.BatchNorm2d(out_ch)
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.act(self.bn(self.conv(x)))


