from scripts.ml.train_hp_seq import *

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

class MiniMapNet(nn.Module):
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