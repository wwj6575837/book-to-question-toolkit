#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 chapter_pipeline 生成的详细版：去重复章、压平跑偏的 ## 小节。

chapter_pipeline 对拆分过的章（第3/8/9章）会把「章级汇总」和「分节结果」
拼在一起，常见问题：
- 同一章出现两次（第1章）；
- 章级标题重复（`## 第8章 城市道路工程图`）；
- 小节标题跑成 `## 8.2`、`## # 8.1`。

本脚本按 book.json 的 chapters 顺序重建：每章/每节只保留一个 `##`，
其余标题按编号深度降为 ###/####/#####，重复块保留内容更多的那个。

用法：
    python clean_detail.py --file "exports/multi_agent/final/书籍库/2552-1 ...md" \
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

REPO_ROOT = Path(__file__).resolve().parents[4]
_HEAD = re.compile(r"^(#{1,6})\s*(.*)$")
_CHAPTER = re.compile(
    r"^第\s*([〇一二三四五六七八九十百\d]+)\s*(?:章|单元|讲|编|课|模块|项目|部分|篇)"
    r"|^(?:单元|项目|模块)\s*([〇一二三四五六七八九十百\d]+)"
)
_PART = re.compile(r"第\s*(\d+)\s*节")
_DASH = re.compile(r"[‐‑‒–—―−]")
_NUM = re.compile(r"^(\d+(?:\.\d+)+)\s*(.*)$")
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


def clean_text(text: str) -> str:
    text = re.sub(r"^#+\s*", "", text.strip())
    text = text.replace("．", ".")
    text = _DASH.sub("-", text)
    return re.sub(r"\s+", " ", text).strip()


def heading_level(text: str) -> int:
    m = re.match(r"^(\d+(?:\.\d+)+)\s+", text)
    if m:
        return max(3, min(6, 2 + m.group(1).count(".")))
    if re.match(r"^\d+[.、]\s*", text) or re.match(r"^[（(]\d+[）)]", text):
        return 5
    return 3


def load_expected(path: Path | None) -> list[str]:
    if not path or not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out = []
    for ch in data.get("chapters", []):
        title = str(ch.get("title", "")).strip()
        m = re.search(r"第\s*\d+\s*章.*$", title)
        out.append(_DASH.sub("-", m.group(0) if m else title))
    return out


def key_of(title: str) -> tuple[int, int] | None:
    m = _CHAPTER.match(title)
    if not m:
        return None
    n = _to_int(m.group(1) or m.group(2))
    p = _PART.search(title)
    return (n, int(p.group(1)) if p else 0)


def expected_index(title: str, expected: list[str]) -> int | None:
    k = key_of(title)
    if k is None:
        return None
    n, part = k
    same = [(i, key_of(t)) for i, t in enumerate(expected) if key_of(t) and key_of(t)[0] == n]
    if not same:
        return None
    if part:
        for i, kk in same:
            if kk and kk[1] == part:
                return i
        return None
    # 章级标题：同章只有一条就用它；拆过节的用最后一份（章级标题通常是最后一段）
    return same[-1][0]


def split_blocks(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    pre, blocks = split_at_headings(lines, 2)
    return pre, [block for _title, block in blocks]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="清理详细版的重复章和跑偏标题")
    p.add_argument("--file", required=True)
    p.add_argument("--book-json", help="书目录下的 book.json（提供 16 章期望标题）")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-unmatched", action="store_true",
                   help="允许把未匹配块追加到末尾/缺少期望章节时继续")
    args = p.parse_args(argv)

    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    expected = load_expected(Path(args.book_json) if args.book_json else None)
    text = read_text_any(path)
    preamble, blocks = split_blocks(text.splitlines())

    chosen: dict[int, list[str]] = {}
    sections: dict[int, list[str]] = {}
    unmatched: list[list[str]] = []
    current: int | None = None

    for block in blocks:
        head = block[0]
        m = _HEAD.match(head)
        raw = m.group(2) if m else head[3:]
        cleaned = clean_text(raw)
        idx = expected_index(cleaned, expected) if expected else None
        is_chapter = key_of(cleaned) is not None and bool(_CHAPTER.match(cleaned))
        if idx is not None:
            current = idx
            if idx in chosen:
                # 重复章：保留字符更多的那个
                if sum(len(x) for x in block) > sum(len(x) for x in chosen[idx]):
                    chosen[idx] = block
            else:
                chosen[idx] = block
        elif is_chapter and not expected:
            unmatched.append(block)
        else:
            # 只有标题、没有正文的跑偏小节（如 `## # 8.1`）直接丢掉
            if not any(x.strip() for x in block[1:]):
                continue
            sec = f"{'#' * heading_level(cleaned)} {cleaned}"
            target = current
            mnum = re.match(r"^(\d+)\.", cleaned)
            if mnum and expected:
                ch = int(mnum.group(1))
                same = [i for i, t in enumerate(expected)
                        if key_of(t) and key_of(t)[0] == ch]
                if same and (current is None or key_of(expected[current])[0] != ch):
                    target = same[0]
            if target is not None:
                sections.setdefault(target, []).append(sec)
                sections[target].extend(block[1:])
            else:
                unmatched.append(block)

    missing = [expected[i] for i in range(len(expected)) if i not in chosen] if expected else []
    if missing:
        print(f"缺少期望章节 {len(missing)} 个：{missing[:10]}", file=sys.stderr)
    if unmatched:
        print(f"未匹配块 {len(unmatched)} 个：", file=sys.stderr)
        for block in unmatched[:10]:
            print(f"  - {block[0][:80]}", file=sys.stderr)
    if (missing or unmatched) and not args.allow_unmatched:
        print("默认不写入；确认后可加 --allow-unmatched。", file=sys.stderr)
        return 3

    out: list[str] = []
    if any(x.strip() for x in preamble):
        out.extend(preamble)
    order = list(range(len(expected))) if expected else []
    for i in order:
        if i not in chosen:
            continue
        block = chosen[i].copy()
        block[0] = f"## {expected[i]}"
        out.extend(block)
        if i in sections:
            out.append("")
            out.extend(sections[i])
    for block in unmatched:
        if out:
            out.append("")
        out.extend(block)

    new_text = "\n".join(out).rstrip() + "\n"
    h2 = [ln for ln in new_text.splitlines() if ln.startswith("## ")]
    print(f"文件：{path}")
    print(f"期望章节：{len(expected)}  实际 ##：{len(h2)}")
    for h in h2:
        print("  ", h)
    if expected and len(h2) != len(expected):
        print("⚠ ## 数量与期望不一致", file=sys.stderr)

    if not args.apply:
        print("\n当前是 dry-run；确认后加 --apply。")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-clean-{stamp}")
    shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8-sig")
    print(f"已写回：{path}\n备份：{backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
