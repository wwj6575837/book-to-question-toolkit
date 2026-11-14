#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已有 Markdown 的 HTML flex 图片块转成箭头居中块（默认 dry-run）。"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from markdown_image_blocks import normalize_image_blocks  # noqa: E402

_FLEX_OPEN = re.compile(r'<div\b[^>]*\bstyle="[^"]*display\s*:\s*flex', re.IGNORECASE)


def _counts(text: str) -> dict:
    return {
        "flex": len(_FLEX_OPEN.findall(text)),
        "arrow": text.count("->![]("),
        "img_tag": text.count("<img"),
        "thumb": text.count("imageMogr2/thumbnail"),
        "table_box": text.count('style="margin:14px 0;"'),
    }


def collect_files(paths: list[str], all_md: bool) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            pattern = "*.md" if all_md else "*_full.md"
            files.extend(sorted(p.rglob(pattern)))
        elif p.is_file():
            files.append(p)
        else:
            print(f"⚠ 路径不存在，跳过: {p}")
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="HTML 图片块 → 箭头居中块")
    ap.add_argument("paths", nargs="+", help="Markdown 文件或目录")
    ap.add_argument("--apply", action="store_true", help="实际写回（默认 dry-run）")
    ap.add_argument("--pct", type=int, default=50, help="七牛缩放比例，默认 50")
    ap.add_argument("--all-md", action="store_true", help="目录下处理全部 *.md（默认仅 *_full.md）")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak 备份")
    args = ap.parse_args()

    files = collect_files(args.paths, args.all_md)
    if not files:
        print("没有可处理的文件")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    total_changed = 0
    total_flex_left = 0

    for path in files:
        text = path.read_text(encoding="utf-8")
        new_text = normalize_image_blocks(text, args.pct)
        before, after = _counts(text), _counts(new_text)
        changed = new_text != text
        total_changed += int(changed)
        total_flex_left += after["flex"]

        status = "改写" if changed else "不变"
        print(
            f"[{status}] {path}\n"
            f"    flex 块 {before['flex']} → {after['flex']} | "
            f"箭头行 {before['arrow']} → {after['arrow']} | "
            f"<img> {before['img_tag']} → {after['img_tag']} | "
            f"缩放参数 {before['thumb']} → {after['thumb']} | "
            f"表格容器 {before['table_box']} → {after['table_box']}"
        )
        if not changed:
            continue
        if args.apply:
            if not args.no_backup:
                path.with_name(path.name + f".bak-imgblock-{stamp}").write_text(text, encoding="utf-8")
            path.write_text(new_text, encoding="utf-8")

    mode = "已写回" if args.apply else "dry-run（加 --apply 写回）"
    print(f"\n{mode}：{total_changed}/{len(files)} 个文件有改动；残留 flex 块 {total_flex_left} 处")
    if total_flex_left:
        print("⚠ 残留 flex 块通常是 HTML 不完整（截断），脚本已原样保留，请人工确认")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
