#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Markdown 标题扫描小工具：跳过 ``` 代码围栏。"""

from __future__ import annotations

import re
from pathlib import Path

HEAD = re.compile(r"^(#{1,6})\s*(.*)$")


def is_fence(line: str) -> bool:
    return line.lstrip().startswith("```")


def iter_headings(lines: list[str], level: int | None = None):
    """按顺序产出 (行号, 级别, 标题文字)，跳过代码围栏内的 #。"""
    in_fence = False
    for i, line in enumerate(lines):
        if is_fence(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEAD.match(line)
        if not m:
            continue
        lv = len(m.group(1))
        if level is None or lv == level:
            yield i, lv, m.group(2).strip()


def first_heading(lines: list[str]):
    return next(iter(iter_headings(lines)), None)


def split_at_headings(lines: list[str], level: int = 2):
    """返回 (preamble, [(标题, 该标题起的行列表)])，跳过代码围栏。"""
    idxs = [i for i, _, _ in iter_headings(lines, level)]
    if not idxs:
        return lines, []
    pre = lines[: idxs[0]]
    blocks = []
    for n, i in enumerate(idxs):
        end = idxs[n + 1] if n + 1 < len(idxs) else len(lines)
        m = HEAD.match(lines[i])
        blocks.append((m.group(2).strip() if m else lines[i], lines[i:end]))
    return pre, blocks


def read_text_any(path: str | Path) -> str:
    """UTF-8（含 BOM）优先，失败回退 GBK。"""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码（既不是 UTF-8 也不是 GBK）：{path}")
