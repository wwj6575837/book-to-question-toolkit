#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从豆包产物补齐详细版里被融合环节丢掉的小节。

chapter_pipeline 的 merge 偶尔会截断后面的节（本书：2.5、4.3、8.4、8.5、
8.7-8.9）。豆包产物里通常还在，本脚本按 book.json 的章节顺序：
- 找到对应豆包文件；
- 把详细版里缺失的顶级节（X.Y）整段补到该章末尾；
- 统一重排标题层级、去掉重复标题。

用法：
    python supplement_detail.py --file "exports/.../2552-1 ....md" \
        --organized-dir "exports/Organized_Books/2552-..._organized" \
        --book-json "拆书/2552-.../book.json" --apply
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

_HEAD = re.compile(r"^(#{1,6})\s*(.*)$")
_CHAPTER = re.compile(r"^第\s*(\d+)\s*章")
_PART = re.compile(r"第\s*(\d+)\s*节")
_TOP = re.compile(r"^(\d+)\.(\d+)(?![\d.])")
_DASH = re.compile(r"[‐‑‒–—―−]")


def clean(text: str) -> str:
    text = re.sub(r"^#+\s*", "", text.strip()).replace("．", ".")
    return re.sub(r"\s+", " ", _DASH.sub("-", text)).strip()


def level_of(text: str) -> int:
    m = re.match(r"^(\d+(?:\.\d+)+)\s+", text)
    if m:
        return max(3, min(6, 2 + m.group(1).count(".")))
    if re.match(r"^\d+[.、]\s*", text) or re.match(r"^[（(]\d+[）)]", text):
        return 5
    if text.startswith("复习思考题"):
        return 3
    return 4


def key(title: str):
    m = _CHAPTER.match(title)
    if not m:
        return None
    p = _PART.search(title)
    return int(m.group(1)), int(p.group(1)) if p else 0


def load_expected(path: Path | None) -> list[str]:
    if not path or not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out = []
    for ch in data.get("chapters", []):
        t = str(ch.get("title", ""))
        m = re.search(r"第\s*\d+\s*章.*$", t)
        out.append(_DASH.sub("-", m.group(0) if m else t))
    return out


def split_h2(lines: list[str]) -> list[list[str]]:
    blocks, cur = [], []
    for ln in lines:
        if ln.startswith("## "):
            if cur:
                blocks.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def headings(lines: list[str]):
    for i, ln in enumerate(lines):
        m = _HEAD.match(ln)
        if m:
            yield i, m.group(1), clean(m.group(2))


def section_blocks(lines: list[str]) -> dict[str, list[str]]:
    """提取顶级节 X.Y（含其下所有子标题），键为 'X.Y'。"""
    hs = list(headings(lines))
    out: dict[str, list[str]] = {}
    for n, (i, _lvl, txt) in enumerate(hs):
        m = _TOP.match(txt)
        if not m:
            continue
        num = f"{m.group(1)}.{m.group(2)}"
        end = len(lines)
        for j, _lvl2, txt2 in hs[n + 1:]:
            if _TOP.match(txt2):
                end = j
                break
        out.setdefault(num, lines[i:end])
    return out


def norm_block(block: list[str], title: str) -> list[str]:
    """第一行固定为 ## title；其余标题按编号深度重排；连续重复标题去重。"""
    body, out, seen = block[1:], [f"## {title}"], set()
    for ln in body:
        m = _HEAD.match(ln)
        if not m:
            out.append(ln)
            continue
        txt = clean(m.group(2))
        lv = level_of(txt)
        if (lv, txt) in seen:
            continue
        seen.add((lv, txt))
        out.append("#" * lv + " " + txt)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="从豆包产物补齐详细版缺失小节")
    p.add_argument("--file", required=True)
    p.add_argument("--organized-dir", required=True)
    p.add_argument("--book-json")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    path = Path(args.file).expanduser().resolve()
    org = Path(args.organized_dir).expanduser().resolve()
    expected = load_expected(Path(args.book_json) if args.book_json else None)
    if not path.is_file() or not org.is_dir():
        print("文件或目录不存在", file=sys.stderr)
        return 2
    if not expected:
        print("需要 --book-json 提供章节标题", file=sys.stderr)
        return 2

    chosen: dict[int, list[str]] = {}
    for block in split_h2(path.read_text(encoding="utf-8-sig").splitlines()):
        title = clean(_HEAD.match(block[0]).group(2))
        k = key(title)
        if not k:
            continue
        n, part = k
        same = [i for i, t in enumerate(expected) if key(t) and key(t)[0] == n]
        if not same:
            continue
        idx = next((i for i in same if part and key(expected[i])[1] == part), None)
        if idx is None:
            idx = same[-1]
        cur = chosen.get(idx)
        if cur is None or sum(len(x) for x in block) > sum(len(x) for x in cur):
            chosen[idx] = block

    added_total = 0
    for idx, title in enumerate(expected):
        block = chosen.get(idx)
        if block is None:
            print(f"⚠ 缺章：{title}", file=sys.stderr)
            continue
        k = key(title)
        n, part = k
        pats = [f"*第{n}章*"]
        if part:
            pats.append(f"*第{part}节*")
        cands = [f for f in org.glob(f"*第{n}章*.doubao_from_source.md")
                 if (not part or f"第{part}节" in f.name)]
        if not cands:
            block = norm_block(block, title)
            chosen[idx] = block
            continue
        doubao = max(cands, key=lambda f: f.stat().st_size)
        dlines = doubao.read_text(encoding="utf-8-sig").splitlines()
        dsecs = section_blocks(dlines)
        existing = set()
        for _i, _l, txt in headings(block):
            m = _TOP.match(txt)
            if m:
                existing.add(f"{m.group(1)}.{m.group(2)}")
        missing = sorted(
            (num for num in dsecs if num not in existing),
            key=lambda x: tuple(int(v) for v in x.split("."))
        )
        if missing:
            print(f"{title} <- {doubao.name[-38:]} 补: {', '.join(missing)}")
            for num in missing:
                block.extend([""])
                block.extend(dsecs[num])
                added_total += 1
        chosen[idx] = norm_block(block, title)

    out = []
    for i, title in enumerate(expected):
        if i in chosen:
            if out:
                out.append("")
            out.extend(chosen[i])
    new_text = "\n".join(out).rstrip() + "\n"
    h2 = sum(1 for ln in new_text.splitlines() if ln.startswith("## "))
    print(f"\n补齐 {added_total} 个小节；## 数量 {h2}/{len(expected)}")
    if not args.apply:
        print("当前是 dry-run；确认后加 --apply。")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-supp-{stamp}")
    shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8-sig")
    print(f"已写回：{path}\n备份：{backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
