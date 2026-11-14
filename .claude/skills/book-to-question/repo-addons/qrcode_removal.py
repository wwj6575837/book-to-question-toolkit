#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""去除书页插图里的扫码二维码：模板匹配定位后用白色矩形覆盖。

纯 numpy + Pillow 实现（FFT 归一化互相关 + 积分图），不依赖 OpenCV。
二维码在图片中的位置不固定（可能在右下角，也可能嵌在表格单元格里），
因此不能按固定区域裁剪。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent

DEFAULTS = {
    "enabled": True,
    "templates": ["qrcode_templates/maiziedu.png"],
    "threshold": 0.55,
    "scales": [0.9, 0.95, 1.0, 1.05, 1.1],
    "pad": 4,
    "content_inset": 4,
    "border_pad": 150,
    # 去码后非白像素占比低于该值，视为「整张图只有二维码」
    "qrcode_only_ratio": 0.01,
}

_ACTIVE_CFG: dict | None = None


def configure(cfg: dict | None) -> None:
    """设置全局去码配置；cfg 为 None 或 enabled=false 时关闭。"""
    global _ACTIVE_CFG
    merged = dict(DEFAULTS)
    if cfg:
        merged.update(cfg)
    _ACTIVE_CFG = merged if merged.get("enabled", True) else None


@lru_cache(maxsize=16)
def _load_template(path: str) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("L"), dtype=np.float64)


def load_templates(paths: Sequence[str] | None = None) -> list[np.ndarray]:
    cfg = _ACTIVE_CFG or DEFAULTS
    paths = list(paths or cfg.get("templates") or DEFAULTS["templates"])
    templates = []
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.is_file():
            print(f"   ⚠ 二维码模板不存在，跳过：{path}")
            continue
        templates.append(_load_template(str(path)))
    return templates


def _integral(a: np.ndarray) -> np.ndarray:
    return np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)))


def _box_sum(ii: np.ndarray, h: int, w: int) -> np.ndarray:
    return ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]


def _ncc_map(img: np.ndarray, tpl: np.ndarray) -> np.ndarray | None:
    """归一化互相关图；返回形状 (H-h+1, W-w+1)。"""
    H, W = img.shape
    h, w = tpl.shape
    if h > H or w > W:
        return None
    ii = _integral(img)
    ii2 = _integral(img * img)
    n = float(h * w)
    mu = _box_sum(ii, h, w) / n
    var = np.maximum(_box_sum(ii2, h, w) - n * mu * mu, 1e-6)
    t = tpl - tpl.mean()
    tvar = float((t * t).sum())
    if tvar < 1e-6:
        return None
    F = np.fft.rfft2(img, (H, W))
    T = np.fft.rfft2(t[::-1, ::-1], (H, W))
    corr = np.fft.irfft2(F * T, (H, W))[h - 1 : H, w - 1 : W]
    return corr / np.sqrt(tvar * var)


def detect_qrcodes(
    gray: np.ndarray,
    templates: Sequence[np.ndarray],
    scales: Sequence[float] = (0.9, 0.95, 1.0, 1.05, 1.1),
    threshold: float = 0.55,
    border_pad: int = 150,
) -> list[tuple[int, int, int, int]]:
    """返回 [(x, y, w, h), ...]，坐标为原图像素。

    四周补白边后再匹配，贴图片边缘或被裁掉一部分的二维码也能定位。
    """
    if not templates or gray.size == 0:
        return []
    factor = 2
    padded = np.pad(gray, border_pad, constant_values=255)
    small = padded[::factor, ::factor]
    candidates: list[tuple[float, int, int, int, int]] = []

    for tpl in templates:
        for scale in scales:
            tw = max(8, int(round(tpl.shape[1] * scale / factor)))
            th = max(8, int(round(tpl.shape[0] * scale / factor)))
            t = np.asarray(
                Image.fromarray(tpl.astype(np.uint8)).resize((tw, th)),
                dtype=np.float64,
            )
            score = _ncc_map(small, t)
            if score is None:
                continue
            ys, xs = np.where(score >= threshold)
            for y, x in zip(ys, xs):
                candidates.append(
                    (float(score[y, x]),
                     int(x * factor) - border_pad,
                     int(y * factor) - border_pad,
                     tw * factor, th * factor)
                )

    candidates.sort(reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for score, x, y, w, h in candidates:
        cx, cy = x + w / 2, y + h / 2
        if any(
            (cx - (kx + kw / 2)) ** 2 + (cy - (ky + kh / 2)) ** 2
            < (max(w, h) * 0.5) ** 2
            for kx, ky, kw, kh in kept
        ):
            continue
        kept.append((x, y, w, h))
    return kept


def remove_qrcodes(
    im: Image.Image,
    templates: Sequence[np.ndarray] | None = None,
    threshold: float | None = None,
    scales: Sequence[float] | None = None,
    pad: int | None = None,
    content_inset: int | None = None,
    border_pad: int | None = None,
) -> tuple[Image.Image, list[tuple[int, int, int, int]]]:
    """返回 (处理后图像, 命中框列表)；无命中时原样返回。"""
    cfg = _ACTIVE_CFG or DEFAULTS
    templates = templates if templates is not None else load_templates()
    if not templates:
        return im, []
    threshold = cfg["threshold"] if threshold is None else threshold
    scales = cfg["scales"] if scales is None else scales
    pad = cfg["pad"] if pad is None else pad
    inset = cfg["content_inset"] if content_inset is None else content_inset
    border_pad = cfg["border_pad"] if border_pad is None else border_pad

    gray = np.asarray(im.convert("L"), dtype=np.float64)
    boxes = detect_qrcodes(
        gray, templates, scales=scales, threshold=threshold,
        border_pad=border_pad,
    )
    if not boxes:
        return im, []

    out = im.copy()
    if out.mode == "P":
        out = out.convert("RGB")
    draw = ImageDraw.Draw(out)
    max_x, max_y = out.size
    for x, y, w, h in boxes:
        cx, cy = x + w / 2, y + h / 2
        half_w = max(1.0, w / 2 - inset + pad)
        half_h = max(1.0, h / 2 - inset + pad)
        draw.rectangle(
            [max(0, int(cx - half_w)), max(0, int(cy - half_h)),
             min(max_x - 1, int(cx + half_w)), min(max_y - 1, int(cy + half_h))],
            fill="white",
        )
    return out, boxes


def qrcode_only(
    im: Image.Image,
    threshold: float | None = None,
) -> tuple[bool, list[tuple[int, int, int, int]]]:
    """判断整张图是否只有二维码（去码后几乎没有其它深色内容）。"""
    cfg = _ACTIVE_CFG or DEFAULTS
    if threshold is None:
        threshold = float(cfg.get("qrcode_only_ratio", 0.01))
    out, boxes = remove_qrcodes(im)
    if not boxes:
        return False, []
    gray = np.asarray(out.convert("L"), dtype=np.uint8)
    return float((gray < 200).mean()) < threshold, boxes


def remove_qrcodes_if_enabled(im: Image.Image) -> tuple[Image.Image, list]:
    """供上传流程调用；未 configure 或关闭时直接返回原图。"""
    if _ACTIVE_CFG is None:
        return im, []
    return remove_qrcodes(im)
