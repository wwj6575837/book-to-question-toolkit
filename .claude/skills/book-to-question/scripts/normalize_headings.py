#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MinerU 产出的章节 Markdown 标题层级归一化。

问题：MinerU 常把所有标题都输出成 `##`，有的文件单元标题是 `#`，
拆分后的第 2/3 部分甚至直接以小节标题开头。后续 chapter_pipeline 会把
小节误当章，拆分章节.py 会把一本书拆成几十份。

规则：
- 每个文件只保留一个 `##`：章节标题取文件名（去掉 <id>-<书名>- 前缀和
  _full/-部分N 后缀），例如 `第8章-城市道路工程图-第1节-...`。
- 文件内其余标题统一降一级：`##` -> `###`，`###` -> `####`。
- 清理标题里的全角点 `．`、`<sub>．</sub>`、多余空格。

用法：
    python normalize_headings.py --dir "exports/Organized_Books/<...>_organized"
    python normalize_headings.py --dir "<...>" --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from mdscan import first_heading, read_text_any

_HEAD = re.compile(r"^(#{1,6})\s*(.*)$")
_UNIT = re.compile(
    r"^(?:教学)?单元\s*\d+"
    r"|^第\s*\d+\s*(?:章|单元|讲|编|课|模块|项目|部分|篇)"
    r"|^(?:项目|模块|单元)\s*[〇一二三四五六七八九十\d]+"
)
_SUB = re.compile(r"<sub>(.*?)</sub>", re.I)
_NUM = re.compile(r"^(\d+(?:\.\d+)+)\s*(.*)$")


def clean_heading(text: str) -> str:
    text = _SUB.sub(r"\1", text)
    text = text.replace("．", ".")
    text = re.sub(r"\s+", " ", text).strip()
    m = _NUM.match(text)
    if m:
        return f"{m.group(1)} {m.group(2)}".strip()
    return text


def heading_level(text: str) -> int:
    """按编号深度定层级，保证幂等：3.1→###，3.1.1→####，1./（1）→#####。"""
    m = re.match(r"^(\d+(?:\.\d+)+)\s+", text)
    if m:
        dots = m.group(1).count(".")
        return max(3, min(6, 2 + dots))
    if re.match(r"^\d+[.、]\s*", text) or re.match(r"^[（(]\d+[）)]", text):
        return 5
    return 3


def chapter_title(path: Path, book_prefix: str) -> str:
    stem = path.stem
    if stem.startswith(book_prefix + "-"):
        stem = stem[len(book_prefix) + 1:]
    else:
        # 目录名不规范时，至少剥掉开头的 id
        stem = re.sub(r"^\d+\s*[-‐‑‒–—―−]\s*", "", stem)
    stem = re.sub(r"_full(?:[-_]部分\d+)?$", "", stem)
    return stem


def normalize(text: str, title: str) -> tuple[str, dict]:
    lines = text.splitlines()
    first = first_heading(lines)
    head_idx = first[0] if first else None
    stats = {"demoted": 0, "replaced_unit": False, "inserted_title": False}
    if head_idx is None:
        return f"## {title}\n\n{text}", stats

    first_text = first[2]
    is_unit = bool(_UNIT.match(clean_heading(first_text)))

    out: list[str] = []
    out.extend(lines[:head_idx])
    out.append(f"## {title}")
    stats["replaced_unit" if is_unit else "inserted_title"] = True
    if not is_unit:
        out.append("")

    rest = lines[head_idx + 1:] if is_unit else lines[head_idx:]
    in_fence = False
    for line in rest:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = _HEAD.match(line)
        if not m:
            out.append(line)
            continue
        text = clean_heading(m.group(2))
        level = heading_level(text)
        out.append("#" * level + " " + text)
        stats["demoted"] += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="归一化 Organized_Books 章节标题层级")
    p.add_argument("--dir", required=True, help="<id>-<书名>_organized 目录")
    p.add_argument("--apply", action="store_true", help="默认 dry-run；加此参数才写")
    p.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    p.add_argument("--no-backup", action="store_true", help="覆盖前不备份")
    args = p.parse_args(argv)

    folder = Path(args.dir).expanduser().resolve()
    if not folder.is_dir():
        print(f"目录不存在：{folder}", file=sys.stderr)
        return 2
    book_prefix = folder.name[:-len("_organized")] if folder.name.endswith("_organized") else folder.name
    files = sorted(folder.glob("*_full*.md"))
    if not files:
        print(f"{folder} 下没有 *_full*.md", file=sys.stderr)
        return 2

    total_changed = 0
    for f in files:
        text = read_text_any(f)
        title = chapter_title(f, book_prefix)
        new, stats = normalize(text, title)
        h2 = len(re.findall(r"^## ", new, re.M))
        changed = new != text
        total_changed += int(changed)
        flag = "改" if changed else "不变"
        print(f"[{flag}] {f.name}")
        print(f"      -> ## {title}")
        print(f"      降级 {stats['demoted']} 个标题，## 总数 {h2}")
        if args.apply and changed:
            if not args.no_backup:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(f, f.with_name(f"{f.name}.bak-{stamp}"))
            f.write_text(new, encoding="utf-8-sig")

    print(f"\n共 {len(files)} 个文件，{total_changed} 个需要改。")
    if args.apply:
        print("已写回（默认先备份 .bak-时间戳）。")
    else:
        print("当前是 dry-run；确认后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
