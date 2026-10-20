#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆书上题 skill：校验 补题/题目/ 下的出题 JSON。

用法：
    python validate_questions.py --id 1957 --book "逻辑思维训练教程" \
        --config "补题\\配置\\1957逻辑思维训练教程.json"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TYPES = {"choice", "multiple", "judge", "fill", "short", "group"}
_DASH = re.compile(r"[‐‑‒–—―−]")
_FORBIDDEN = ("教材", "课本", "根据所学", "如上图", "如下图", "见图", "见下表", "见上图")
_BARE_HASH = re.compile(r"^\s*#(?!\\)", re.M)


def norm(s: str) -> str:
    return _DASH.sub("-", s).strip()


def expected_counts(config_path: Path | None) -> int | None:
    if not config_path:
        return None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        counts = cfg.get("counts", cfg.get("question", {}).get("counts", {}))
        per_round = sum(sum(v.values()) for v in counts.values() if isinstance(v, dict))
        rounds = max(1, int(cfg.get("rounds", 1)))
        return per_round * rounds or None
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        return None


def check_one(q, idx: int, book_id: int, book: str, tag: str, errs: list[str], warns: list[str]):
    where = f"#{idx}"
    if not isinstance(q, dict):
        errs.append(f"{where} 不是对象")
        return None
    if q.get("know_id") != book_id:
        errs.append(f"{where} know_id={q.get('know_id')!r}，应为 {book_id}")
    typ = q.get("type")
    if typ not in TYPES:
        errs.append(f"{where} type 非法：{typ!r}")
        return typ

    actual_tag = q.get("tags")
    if norm(str(actual_tag or "")) != norm(tag):
        warns.append(f"{where} tags={actual_tag!r}，应为 {tag!r}")

    source = str(q.get("source") or "")
    if not source:
        errs.append(f"{where} source 为空")
    elif not source.startswith("《"):
        warns.append(f"{where} source 未以《开头：{source!r}")

    title = str(q.get("title") or "").strip()
    parse = str(q.get("parse") or "").strip()
    if not title:
        errs.append(f"{where} title 为空")
    if typ != "group" and not parse:
        errs.append(f"{where} parse 为空")

    for field, text in (("title", title), ("parse", parse)):
        for word in _FORBIDDEN:
            if word in text:
                errs.append(f"{where} {field} 含禁用来源/图片用语「{word}」")
        if _BARE_HASH.search(text):
            warns.append(f"{where} {field} 有行首裸 #，需写成 \\#")

    d = q.get("difficulty")
    if not isinstance(d, (int, float)) or isinstance(d, bool):
        errs.append(f"{where} difficulty 不是数字：{d!r}")
    elif not (0.2 <= float(d) <= 0.9):
        errs.append(f"{where} difficulty={d} 超出 0.2–0.9")
    elif abs(round(float(d), 1) - float(d)) > 1e-9:
        warns.append(f"{where} difficulty={d} 不是 1 位小数")

    options = q.get("options")
    answer = q.get("answer")
    if typ in {"choice", "multiple"}:
        if not isinstance(options, list) or len(options) < 2:
            errs.append(f"{where} {typ} options 少于 2 项")
        if not isinstance(answer, str) or not answer.strip():
            errs.append(f"{where} {typ} answer 为空")
        elif typ == "choice" and not re.fullmatch(r"[A-H]", answer.strip()):
            warns.append(f"{where} choice answer 不是单个字母：{answer!r}")
        elif typ == "multiple":
            if not re.fullmatch(r"[A-H]+", answer.strip()):
                warns.append(f"{where} multiple answer 含非字母：{answer!r}")
            elif answer != "".join(sorted(set(answer))):
                warns.append(f"{where} multiple answer 未按升序去重：{answer!r}")
    elif typ == "judge":
        if answer not in ("是", "否"):
            errs.append(f"{where} judge answer 应为 是/否：{answer!r}")
    elif typ == "fill":
        if answer != "":
            errs.append(f"{where} fill answer 必须为空字符串：{answer!r}")
    elif typ == "group":
        subs = q.get("subs")
        if not isinstance(subs, list) or not (2 <= len(subs) <= 5):
            errs.append(f"{where} group subs 应为 2–5 个")
        elif len(title) < 150:
            warns.append(f"{where} group 根题干不足 150 字（{len(title)}）")
    return typ


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="校验出题 JSON")
    p.add_argument("--id", required=True)
    p.add_argument("--book", required=True)
    p.add_argument("--config", help="命题配置 JSON（用于比对每章题数）")
    p.add_argument("--questions-dir", help="默认 补题/题目/<id><书名>")
    args = p.parse_args(argv)

    book_id = int(args.id)
    book = args.book.strip()
    qdir = Path(args.questions_dir) if args.questions_dir else REPO_ROOT / "补题" / "题目" / f"{book_id}{book}"
    if not qdir.is_dir():
        print(f"题目目录不存在：{qdir}", file=sys.stderr)
        return 2

    expected = expected_counts(Path(args.config)) if args.config else None
    errs: list[str] = []
    warns: list[str] = []
    type_dist: Counter = Counter()
    chapter_counts: dict[str, int] = {}

    skip = {"all.json", "config.json", "progress.json"}
    files = sorted(
        p for p in qdir.glob("*.json")
        if p.name not in skip and not p.name.startswith("_")
    )
    if not files:
        print(f"{qdir} 下没有章节 JSON", file=sys.stderr)
        return 2

    for path in files:
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            errs.append(f"{path.name}: 不是合法 JSON（失败章节原文？）{e}")
            continue
        if not isinstance(obj, list):
            errs.append(f"{path.name}: 顶层不是数组")
            continue
        chapter_counts[path.stem] = len(obj)
        tag = f"{book}-{path.stem}"
        for i, q in enumerate(obj, 1):
            typ = check_one(q, i, book_id, book, tag, errs, warns)
            if typ:
                type_dist[typ] += 1
        if expected is not None:
            dev = abs(len(obj) - expected) / expected
            if dev > 0.2:
                warns.append(f"{path.name}: {len(obj)} 题，配置 {expected} 题，偏差 {dev:.0%}")

    total = sum(chapter_counts.values())
    print(f"目录：{qdir}")
    print(f"章节：{len(chapter_counts)}  题目：{total}")
    for name, n in chapter_counts.items():
        print(f"  {n:>3}  {name}")
    if type_dist:
        print("题型分布：" + "，".join(f"{k}={v}" for k, v in sorted(type_dist.items())))

    failed = qdir / "_failed.txt"
    if failed.exists():
        errs.append(f"存在 _failed.txt：{failed}")

    all_json = qdir / "all.json"
    if all_json.exists():
        try:
            merged = json.loads(all_json.read_text(encoding="utf-8-sig"))
            if not isinstance(merged, list):
                errs.append("all.json 顶层不是数组")
            elif len(merged) != total:
                errs.append(f"all.json 有 {len(merged)} 题，章节合计 {total} 题")
        except (OSError, json.JSONDecodeError) as e:
            errs.append(f"all.json 不是合法 JSON：{e}")
    else:
        warns.append("没有 all.json（脚本会在结束时生成）")

    if warns:
        print(f"\n警告 {len(warns)} 条：")
        for w in warns[:100]:
            print(f"  ⚠ {w}")
        if len(warns) > 100:
            print(f"  … 其余 {len(warns) - 100} 条省略")
    if errs:
        print(f"\n错误 {len(errs)} 条：")
        for e in errs[:100]:
            print(f"  ✗ {e}")
        if len(errs) > 100:
            print(f"  … 其余 {len(errs) - 100} 条省略")
        return 1
    print("\n通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
