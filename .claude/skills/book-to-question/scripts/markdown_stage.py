#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆书上题 skill：转 Markdown 阶段包装。

convert  -> 调 run_converter.py（--pdf-dir，不写回 config.json）
organize -> organizer 前先备份已存在的 <书目录名>_organized，避免其 rmtree

用法：
    python markdown_stage.py convert  --pdf-dir "D:\\...\\1957-书名" [--dry-run]
    python markdown_stage.py organize --source "D:\\...\\1957-书名" [--backup-existing] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
ORGANIZED_ROOT = REPO_ROOT / "exports" / "Organized_Books"
_BOOK_DIR = re.compile(r"^(\d+)\s*[-‐‑‒–—―−]\s*(.+)$")


def run(cmd: list[str], cwd: Path, dry_run: bool) -> int:
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if dry_run:
        return 0
    return subprocess.call(cmd, cwd=str(cwd))


def load_state(source: Path) -> dict:
    path = source / "book.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(source: Path, data: dict) -> None:
    path = source / "book.json"
    try:
        old = load_state(source)

        def merge(dst: dict, src: dict) -> None:
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = v

        merge(old, data)
        path.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"⚠ 无法写入 {path}：{e}", file=sys.stderr)


def parse_book_dir(source: Path) -> tuple[str, str]:
    m = _BOOK_DIR.match(source.name)
    if not m:
        return "", source.name
    return m.group(1), m.group(2).strip()


def cmd_convert(args: argparse.Namespace) -> int:
    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    if not pdf_dir.is_dir():
        print(f"目录不存在：{pdf_dir}", file=sys.stderr)
        return 2
    config = Path(args.config)
    if not config.is_absolute():
        config = REPO_ROOT / config
    cmd = [
        sys.executable,
        str(REPO_ROOT / "run_converter.py"),
        "--pdf-dir", str(pdf_dir),
        "--config", str(config),
    ]
    rc = run(cmd, REPO_ROOT, args.dry_run)
    if rc == 0 and not args.dry_run:
        save_state(pdf_dir, {"stages": {"markdown": "converted"}})
    return rc


def cmd_organize(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        print(f"目录不存在：{source}", file=sys.stderr)
        return 2
    target_base = Path(args.target_base or ORGANIZED_ROOT).expanduser().resolve()
    target = target_base / f"{source.name}_organized"

    book_id, book = parse_book_dir(source)
    if not book_id:
        print(
            f"⚠ 书目录名不是 <id>-<书名> 格式：{source.name}\n"
            "  organizer 会用目录名当书名，输出目录可能不符合预期。",
            file=sys.stderr,
        )

    if target.exists():
        if args.backup_existing:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = target.with_name(f".{target.name}.bak-{stamp}")
            print(f"已存在目标目录，先备份：{target.name} -> {backup.name}")
            if not args.dry_run:
                target.rename(backup)
        elif args.force_delete:
            print(f"已存在目标目录，按 --force-delete 删除：{target}", file=sys.stderr)
            if not args.dry_run:
                import shutil
                shutil.rmtree(target)
        else:
            print(
                f"目标目录已存在：{target}\n"
                "拒绝直接覆盖（organizer 会 rmtree）。请加 --backup-existing 或 --force-delete。",
                file=sys.stderr,
            )
            return 3

    cmd = [
        sys.executable,
        str(REPO_ROOT / "advanced_markdown_reorganizer.py"),
        "-s", str(source),
        "-t", str(target_base),
    ]
    rc = run(cmd, REPO_ROOT, args.dry_run)
    if rc == 0 and not args.dry_run:
        save_state(source, {
            "stages": {"markdown": "done"},
            "organized_dir": str(target),
        })
        print(f"输出目录：{target}")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="拆书上题：转 Markdown 阶段")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("convert", help="PDF -> *_mineru_output（MinerU）")
    pc.add_argument("--pdf-dir", required=True)
    pc.add_argument("--config", default="config.json")
    pc.add_argument("--dry-run", action="store_true")
    pc.set_defaults(func=cmd_convert)

    po = sub.add_parser("organize", help="*_mineru_output -> exports/Organized_Books")
    po.add_argument("--source", required=True, help="书目录（含 *_mineru_output）")
    po.add_argument("--target-base", default=str(ORGANIZED_ROOT))
    po.add_argument("--backup-existing", action="store_true",
                    help="目标 <书目录名>_organized 已存在时先改名备份")
    po.add_argument("--force-delete", action="store_true",
                    help="目标已存在时直接删除（需用户明确确认）")
    po.add_argument("--dry-run", action="store_true")
    po.set_defaults(func=cmd_organize)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
