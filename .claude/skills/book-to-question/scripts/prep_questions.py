#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆书上题 skill：为上题准备 补题/详细版/<id><书名>.md。

优先用 exports/multi_agent/final/书籍库/<id>-1 <书名>.md；
缺失时回退 exports/Organized_Books/<id>-*_organized/*_full.md，
把每章包一层 '## 章节标题'，并把内部标题整体降两级
（_full.md 里所有标题都是 ##，不降级会被 拆分章节.py 误当成章节）。

用法：
    python prep_questions.py --id 1957 --book "逻辑思维训练教程" --dry-run
    python prep_questions.py --id 1957 --book "逻辑思维训练教程" \
        --book-json "D:\\...\\1957-逻辑思维训练教程\\book.json" --apply
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DETAIL_DIR = REPO_ROOT / "exports" / "multi_agent" / "final" / "书籍库"
ORGANIZED_ROOT = REPO_ROOT / "exports" / "Organized_Books"
TARGET_DIR = REPO_ROOT / "补题" / "详细版"
CONFIG_DIR = REPO_ROOT / "补题" / "配置"

_HEAD = re.compile(r"^(#{1,6})(\s+.*)$")
_CHAPTER = re.compile(r"(第\s*\d+\s*章.*|单元\s*\d+.*|第\s*\d+\s*单元.*|项目\s*[〇一二三四五六七八九十\d]+.*)$")
_CHAPTER_NO = re.compile(r"第\s*(\d+)\s*章|单元\s*(\d+)|项目\s*([〇一二三四五六七八九十\d]+)")
_CN = {"〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# Windows 非法字符换成全角（与 rename_split_pdfs.py 的口径一致）
_FILENAME_MAP = str.maketrans({":": "：", "?": "？", "*": "＊", '"': "＂",
                               "<": "＜", ">": "＞", "|": "｜", "/": "／", "\\": "＼"})


def safe_filename(name: str) -> str:
    return name.translate(_FILENAME_MAP)


def cn_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    total = section = 0
    for ch in s:
        if ch in _CN and ch != "十":
            section = _CN[ch]
        elif ch == "十":
            total += (section or 1) * 10
            section = 0
        else:
            return 9999
    return total + section


def chapter_no(label: str) -> tuple[int, int]:
    m = _CHAPTER_NO.search(label)
    if not m:
        return (9999, 0)
    for i, g in enumerate(m.groups(), 1):
        if g:
            return (i, cn_int(g))
    return (9999, 0)


def demote_headings(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            level = min(6, len(m.group(1)) + 2)
            out.append("#" * level + m.group(2))
        else:
            out.append(line)
    return "\n".join(out)


def label_from_filename(stem: str, book_id: str) -> str:
    """1957-书名-第4章-判断_full -> 第4章-判断。"""
    s = stem
    if s.endswith("_full"):
        s = s[:-5]
    s = re.sub(rf"^{re.escape(book_id)}\s*[-‐‑‒–—―−]\s*", "", s)
    m = _CHAPTER.search(s)
    if m:
        return m.group(1).strip()
    return s


def find_detail(book_id: str) -> list[Path]:
    hits = sorted(DETAIL_DIR.glob(f"{book_id}-1 *.md"))
    return [p for p in hits if p.is_file()]


def find_fallback(book_id: str) -> list[Path]:
    dirs = sorted(ORGANIZED_ROOT.glob(f"{book_id}-*_organized"))
    dirs = [d for d in dirs if d.is_dir()]
    if not dirs:
        return []
    best = max(dirs, key=lambda d: len(list(d.glob("*_full.md"))))
    files = list(best.glob("*_full.md"))
    files.sort(key=lambda p: chapter_no(label_from_filename(p.stem, book_id)))
    return files


def build_from_fallback(files: list[Path], book_id: str) -> tuple[str, list[str]]:
    parts, labels = [], []
    for p in files:
        label = label_from_filename(p.stem, book_id)
        body = p.read_text(encoding="utf-8-sig")
        parts.append(f"## {label}\n\n{demote_headings(body).strip()}\n")
        labels.append(label)
    return "\n".join(parts), labels


def load_question_config(path: Path) -> dict | None:
    if not path or not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    q = data.get("question") if "question" in data else data
    if not isinstance(q, dict) or "counts" not in q:
        return None
    counts = q["counts"]
    required = {"单选题", "多选题", "判断题", "组合题", "填空题", "简答题"}
    missing = required - set(counts)
    if missing:
        raise ValueError(f"question.counts 缺少题型：{sorted(missing)}")
    total = sum(sum(v.values()) for v in counts.values() if isinstance(v, dict))
    batch = q.get("batch")
    if batch and total > int(batch):
        raise ValueError(f"counts 合计 {total} 超过本批题量 {batch}")
    return {
        "counts": counts,
        "ratio": q.get("ratio", ""),
        "special": q.get("special", ""),
        "overwrite": False,
        "concurrency": int(q.get("concurrency", 3)),
        "rounds": max(1, int(q.get("rounds", 1))),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="准备 补题/详细版/<id><书名>.md")
    p.add_argument("--id", required=True)
    p.add_argument("--book", required=True)
    p.add_argument("--book-json", help="书目录下的 book.json，用其中的 question 生成命题配置")
    p.add_argument("--config-out", help="命题配置输出路径（默认 补题/配置/<id><书名>.json）")
    p.add_argument("--apply", action="store_true", help="默认 dry-run；加此参数才写文件")
    p.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    p.add_argument("--force", action="store_true", help="覆盖已存在的目标文件（先备份）")
    args = p.parse_args(argv)

    book_id = str(args.id).strip()
    book = args.book.strip()
    if not book_id.isdigit():
        print(f"id 必须是纯数字：{book_id}", file=sys.stderr)
        return 2

    safe_book = safe_filename(book)
    if safe_book != book:
        print(f"书名含 Windows 非法字符，写盘时替换为全角：{book} -> {safe_book}")
    target = TARGET_DIR / f"{book_id}{safe_book}.md"
    others = [p for p in TARGET_DIR.glob(f"{book_id}*.md") if p.resolve() != target.resolve()]
    if others:
        print(
            f"补题/详细版/ 下已有同 id 的其他文件，先消歧（find_source_md 会因多命中报错）：\n"
            + "\n".join(f"  {p.name}" for p in others),
            file=sys.stderr,
        )
        return 2

    detail = find_detail(book_id)
    fallback = find_fallback(book_id) if not detail else []
    if len(detail) > 1:
        print("匹配到多个详细版，先消歧：\n" + "\n".join(f"  {p.name}" for p in detail), file=sys.stderr)
        return 2

    if detail:
        source = detail[0]
        text = source.read_text(encoding="utf-8-sig")
        labels = [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]
        mode = f"详细版：{source.name}"
    elif fallback:
        text, labels = build_from_fallback(fallback, book_id)
        mode = f"_full 回退：{fallback[0].parent.name}（{len(fallback)} 个文件）"
    else:
        print(f"找不到 {book_id} 的详细版或 Organized_Books/*_full.md", file=sys.stderr)
        return 2

    print(f"来源：{mode}")
    print(f"目标：{target}")
    print(f"章节数：{len(labels)}")
    for i, label in enumerate(labels, 1):
        print(f"  {i:>2}. {label}")

    bad_heads = [x for x in labels if re.match(r"^第\s*[〇一二三四五六七八九十\d]+\s*节", x)]
    bare_heads = [x for x in labels if re.fullmatch(r"第\s*[〇一二三四五六七八九十\d]+\s*章", x)]
    if bad_heads:
        print(f"⚠ 详细版疑似标题层级错乱：{len(bad_heads)} 个节标题出现在 ## 层，例如：{bad_heads[:3]}")
    if bare_heads:
        print(f"⚠ 有 {len(bare_heads)} 个章标题没有章名，例如：{bare_heads[:3]}")

    if not labels:
        print("没有解析到任何 '## ' 章节标题，中止。", file=sys.stderr)
        return 2

    cfg = load_question_config(Path(args.book_json)) if args.book_json else None

    if not args.apply:
        if cfg:
            print(f"\n命题配置：counts 合计 {sum(sum(v.values()) for v in cfg['counts'].values())} 题/章")
        print("\n当前是 dry-run；确认后加 --apply 才写入。")
        return 0

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.force:
        print(f"目标已存在，拒绝覆盖：{target}\n如需覆盖加 --force（会先备份）。", file=sys.stderr)
        return 3
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        target.rename(backup)
        print(f"已备份旧文件：{backup.name}")

    target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    print(f"已写入：{target}")

    cfg_path = None
    if cfg:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg_path = Path(args.config_out) if args.config_out else CONFIG_DIR / f"{book_id}{safe_book}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入命题配置：{cfg_path}")

    print("\n下一步：")
    print(f'  .venv\\Scripts\\python.exe 补题\\命题官批量生成.py {book_id} --config "{cfg_path or "<配置json>"}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
