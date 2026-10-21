#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精简版阶段：准备输入、打印/执行 chapter_pipeline1、校验产物。

输入：exports/multi_agent/final/书籍库/<id>-1 <书名>.md（详细版）
准备：复制到 exports/multi_agent/final/精简版-按章/<id>/<id>-1 <书名>.md
运行：python -m multi_agent.chapter_pipeline1 --ids <id> ...
输出：exports/multi_agent/final/精简版/<id>-2 <书名>.md

用法：
    python compact_stage.py prepare --id 2552 --book "市政工程识图与构造（第三版）"
    python compact_stage.py run --id 2552            # 只打印命令
    python compact_stage.py run --id 2552 --yes      # 实际执行
    python compact_stage.py verify --id 2552 --book "市政工程识图与构造（第三版）"
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DETAIL_DIR = REPO_ROOT / "exports" / "multi_agent" / "final" / "书籍库"
INPUT_ROOT = REPO_ROOT / "exports" / "multi_agent" / "final" / "精简版-按章"
OUTPUT_DIR = REPO_ROOT / "exports" / "multi_agent" / "final" / "精简版"


def find_one(root: Path, pattern: str, label: str) -> Path:
    hits = sorted(root.glob(pattern))
    if not hits:
        raise FileNotFoundError(f"找不到{label}：{pattern}（目录 {root}）")
    if len(hits) > 1:
        names = "、".join(p.name for p in hits)
        raise ValueError(f"{label}匹配到多个文件，请先消歧：{names}")
    return hits[0]


def find_detail(book_id: str, book: str | None, explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"详细版不存在：{p}")
        return p
    if book:
        try:
            return find_one(DETAIL_DIR, f"{book_id}-1 {book}.md", "详细版")
        except FileNotFoundError:
            pass
    return find_one(DETAIL_DIR, f"{book_id}-1 *.md", "详细版")


def find_compact(book_id: str, book: str | None, explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"精简版不存在：{p}")
        return p
    if book:
        try:
            return find_one(OUTPUT_DIR, f"{book_id}-2 {book}.md", "精简版")
        except FileNotFoundError:
            pass
    return find_one(OUTPUT_DIR, f"{book_id}-2 *.md", "精简版")


def h2_blocks(path: Path) -> list[tuple[str, str]]:
    """返回 [(## 标题, 正文)]，按 ## 切分。"""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    out = []
    for n, i in enumerate(idx):
        end = idx[n + 1] if n + 1 < len(idx) else len(lines)
        body = "\n".join(lines[i + 1:end]).strip()
        out.append((lines[i][3:].strip(), body))
    return out


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        src = find_detail(str(args.id), args.book, args.detail)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    if not re.match(rf"^{re.escape(str(args.id))}-1\s+.+\.md$", src.name):
        print(
            f"⚠ 文件名不是 <id>-1 <书名>.md，chapter_pipeline1 可能无法识别：{src.name}",
            file=sys.stderr,
        )
    target_dir = INPUT_ROOT / str(args.id)
    target = target_dir / src.name
    print(f"源：{src}")
    print(f"目标：{target}")
    if target.exists():
        same = target.read_bytes() == src.read_bytes()
        print("目标已存在，" + ("内容相同" if same else "内容不同"))
        if same and not args.force:
            print("内容相同，跳过复制。")
            return 0
        if not same and not args.force:
            print("拒绝覆盖；确认要更新加 --force（会先备份）。", file=sys.stderr)
            return 3
        if not same and args.force:
            print(f"将备份旧文件：{target.name}.bak-<时间戳>")
    if args.dry_run:
        print("当前是 dry-run；去掉 --dry-run 才会复制。")
        return 0
    target_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() and not target.read_bytes() == src.read_bytes():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target.rename(target.with_name(f"{target.name}.bak-{stamp}"))
    shutil.copy2(src, target)
    print(f"已复制：{target}")
    return 0


def build_run_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable, "-m", "multi_agent.chapter_pipeline1",
        "--ids", str(args.id),
        "--auto-chunk",
        "--max-workers", "6",
        "--chapter-max-workers", "7",
        "--resume-partial",
        "--skip-merge-unchanged",
        "--limit-deepseek", "15",
        "--limit-doubao", "15",
        "--retries", "2",
        "--retry-backoff", "10",
    ]
    if args.force:
        cmd.append("--force")
    if args.split_by_section:
        cmd.append("--split-by-section")
    return cmd


def cmd_run(args: argparse.Namespace) -> int:
    if not any((INPUT_ROOT / str(args.id)).glob(f"{args.id}-1 *.md")):
        print(f"输入目录没有 {args.id}-1 *.md，先跑 prepare。", file=sys.stderr)
        return 2
    cmd = build_run_cmd(args)
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if not args.yes:
        print("\n当前只打印命令；确认后加 --yes 执行。")
        return 0
    return subprocess.call(cmd, cwd=str(REPO_ROOT))


def cmd_verify(args: argparse.Namespace) -> int:
    errors, warnings = [], []
    try:
        detail = find_detail(str(args.id), args.book, args.detail)
        compact = find_compact(str(args.id), args.book, args.compact)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2

    dblocks = h2_blocks(detail)
    cblocks = h2_blocks(compact)
    print(f"详细版：{detail.name}  ##={len(dblocks)}  {detail.stat().st_size // 1024}KB")
    print(f"精简版：{compact.name}  ##={len(cblocks)}  {compact.stat().st_size // 1024}KB")

    if not cblocks:
        errors.append("精简版没有任何 ## 章节")
    if len(cblocks) != len(dblocks):
        warnings.append(f"## 数量不一致：详细版 {len(dblocks)}，精简版 {len(cblocks)}")

    empty = [t for t, body in cblocks if not body]
    if empty:
        warnings.append(f"空章 {len(empty)} 个：{empty[:5]}")
    titles = [t for t, _ in cblocks]
    dup = sorted({t for t in titles if titles.count(t) > 1})
    if dup:
        warnings.append(f"重复标题：{dup[:5]}")

    ratio = compact.stat().st_size / max(1, detail.stat().st_size)
    print(f"体积比：{ratio:.0%}")
    if ratio > 1.0:
        warnings.append("精简版比详细版还大，可能没有压缩")
    elif ratio < 0.05:
        warnings.append("精简版过小，可能内容缺失")

    for w in warnings:
        print(f"⚠ {w}")
    for e in errors:
        print(f"✗ {e}")

    if args.book_json:
        p = Path(args.book_json).expanduser().resolve()
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            data.setdefault("stages", {})["compact"] = "failed" if errors else "done"
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"已更新：{p} stages.compact={data['stages']['compact']}")
        except (OSError, json.JSONDecodeError) as e:
            print(f"⚠ 无法更新 book.json：{e}", file=sys.stderr)

    if errors:
        return 1
    print("\n校验通过。" + ("（有警告，建议人工抽查）" if warnings else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="book-to-question：精简版阶段")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prepare", help="把详细版复制到 精简版-按章/<id>/")
    pp.add_argument("--id", required=True)
    pp.add_argument("--book")
    pp.add_argument("--detail", help="显式指定详细版文件")
    pp.add_argument("--force", action="store_true", help="目标不同时覆盖（先备份）")
    pp.add_argument("--dry-run", action="store_true")
    pp.set_defaults(func=cmd_prepare)

    pr = sub.add_parser("run", help="打印/执行 chapter_pipeline1")
    pr.add_argument("--id", required=True)
    pr.add_argument("--book")
    pr.add_argument("--force", action="store_true", help="透传 --force（全量重跑）")
    pr.add_argument("--split-by-section", action="store_true",
                    help="按 ### 节拆分（chapter_pipeline1 的 --split-by-section）")
    pr.add_argument("--yes", action="store_true", help="实际执行；默认只打印命令")
    pr.set_defaults(func=cmd_run)

    pv = sub.add_parser("verify", help="校验精简版产物")
    pv.add_argument("--id", required=True)
    pv.add_argument("--book")
    pv.add_argument("--detail", help="显式指定详细版文件")
    pv.add_argument("--compact", help="显式指定精简版文件")
    pv.add_argument("--book-json", help="更新 book.json 的 stages.compact")
    pv.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
