#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从豆包/DeepSeek 产物补齐详细版里被融合环节丢掉的小节。

按 book.json 的章节顺序：
- 找对应章/节的 doubao 产物，缺失时回退 deepseek；
- 把详细版里缺失的顶级节（X.Y）整段补进来；
- 按节号重排整章，统一标题层级、去重。

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

from mdscan import iter_headings, read_text_any, split_at_headings

_HEAD = re.compile(r"^(#{1,6})\s*(.*)$")
_CHAPTER = re.compile(
    r"^第\s*([〇一二三四五六七八九十百\d]+)\s*(?:章|单元|讲|编|课|模块|项目|部分|篇)"
    r"|^(?:单元|项目|模块)\s*([〇一二三四五六七八九十百\d]+)"
)
_PART = re.compile(r"第\s*(\d+)\s*节")
_TOP = re.compile(r"^(\d+)\.(\d+)\.?(?![\d.])")
_DASH = re.compile(r"[‐‑‒–—―−]")
_CN = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    total = section = 0
    for ch in s:
        if ch in _CN and ch != "十":
            section = _CN[ch]
        elif ch == "十":
            total += (section or 1) * 10
            section = 0
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return 9999
    return total + section


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
    n = _to_int(m.group(1) or m.group(2))
    p = _PART.search(title)
    return n, int(p.group(1)) if p else 0


def load_expected(path: Path | None) -> list[str]:
    if not path or not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out = []
    for ch in data.get("chapters", []):
        t = str(ch.get("title", ""))
        m = re.search(
            r"第\s*[〇一二三四五六七八九十百\d]+\s*(?:章|单元|讲|编|课|模块|项目|部分|篇).*$",
            t,
        )
        out.append(_DASH.sub("-", m.group(0) if m else t))
    return out


def headings(lines: list[str]):
    for i, _lv, txt in iter_headings(lines):
        yield i, txt


def split_sections(lines: list[str]):
    """返回 (章标题及前导, [(节号, 行列表)], 尾部非节内容)。"""
    hs = list(headings(lines))
    starts = [(i, txt) for i, txt in hs if _TOP.match(txt)]
    if not starts:
        return lines, [], []
    pre = lines[: starts[0][0]]
    sections = []
    for n, (i, txt) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        m = _TOP.match(txt)
        sections.append((f"{m.group(1)}.{m.group(2)}", lines[i:end]))
    # 最后一个节之后若出现“复习思考题/本章小结”等，提到尾部
    tail = []
    last_start = starts[-1][0]
    for i, txt in hs:
        if i > last_start and not _TOP.match(txt) and re.match(
            r"^(复习|本章|思考题|附录)", txt
        ):
            tail = lines[i:]
            num, body = sections[-1]
            sections[-1] = (num, body[: i - last_start])
            break
    return pre, sections, tail


def section_blocks(lines: list[str]) -> dict[str, list[str]]:
    """提取顶级节 X.Y（含其下所有子标题），键为 'X.Y'。"""
    hs = list(headings(lines))
    out: dict[str, list[str]] = {}
    for n, (i, txt) in enumerate(hs):
        m = _TOP.match(txt)
        if not m:
            continue
        num = f"{m.group(1)}.{m.group(2)}"
        end = len(lines)
        for j, txt2 in hs[n + 1:]:
            if _TOP.match(txt2):
                end = j
                break
        out.setdefault(num, lines[i:end])
    return out


def norm_block(block: list[str], title: str) -> list[str]:
    """第一行固定为 ## title；其余标题按编号深度重排；重复标题去重。"""
    body, out, seen = block[1:], [f"## {title}"], set()
    in_fence = False
    for ln in body:
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(ln)
            continue
        if in_fence:
            out.append(ln)
            continue
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


def pick_source(org: Path, n: int, part: int, ext: str) -> Path | None:
    all_c = sorted(org.glob(f"*第{n}章*.{ext}"))
    if part:
        pref = [f for f in all_c if f"第{part}节" in f.name]
        if pref:
            all_c = pref
    if not all_c:
        return None
    return max(all_c, key=lambda f: f.stat().st_size)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="从豆包/DeepSeek 产物补齐详细版小节")
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
    preamble, blocks = split_at_headings(read_text_any(path).splitlines(), 2)
    for title_text, block in blocks:
        title = clean(title_text)
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
        src = pick_source(org, n, part, "doubao_from_source.md")
        if src is None:
            src = pick_source(org, n, part, "deepseek_from_source.md")
        if src is None:
            print(f"⚠ 找不到 {title} 的 doubao/deepseek 产物，跳过补齐", file=sys.stderr)
            chosen[idx] = norm_block(block, title)
            continue

        dsecs = section_blocks(read_text_any(src).splitlines())
        pre, existing_secs, tail = split_sections(block)
        existing = {num for num, _ in existing_secs}
        missing = sorted(
            (num for num in dsecs if num not in existing),
            key=lambda x: tuple(int(v) for v in x.split(".")),
        )
        if missing:
            print(f"{title} <- {src.name[-38:]} 补: {', '.join(missing)}")
            existing_secs.extend((num, dsecs[num]) for num in missing)
            added_total += len(missing)
            existing_secs.sort(key=lambda x: tuple(int(v) for v in x[0].split(".")))
            block = pre[:]
            for _num, lines in existing_secs:
                block.extend(lines)
            block.extend(tail)
        chosen[idx] = norm_block(block, title)

    out: list[str] = []
    if any(x.strip() for x in preamble):
        out.extend(preamble)
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
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak-supp-{stamp}")
    shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8-sig")
    print(f"已写回：{path}\n备份：{backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
