#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆书上题 skill：校验 补题/题目/ 下的出题 JSON。

用法：
    python validate_questions.py --id 1957 --book "逻辑思维训练教程" \
        --config "补题\\配置\\1957逻辑思维训练教程.json" \
        --book-json "D:\\...\\book.json"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TYPES = {"choice", "multiple", "judge", "fill", "short", "group"}
_DASH = re.compile(r"[‐‑‒–—―−]")
_FORBIDDEN = ("教材", "课本", "根据所学", "如上图", "如下图", "见图", "见下表", "见上图")
_BARE_HASH = re.compile(r"^\s*#(?!\\)", re.M)
_DUP_STRIP = re.compile(r"[\s，。！？、；：“”‘’（）()【】\[\]《》<>·.,!?;:'\"-]+")
SKIP_CHAPTERS = {"00_文件头"}
DIFF_BANDS = (("简单", 0.2, 0.4), ("中等", 0.5, 0.7), ("困难", 0.8, 0.9))


def norm(s: str) -> str:
    return _DASH.sub("-", s).strip()


def norm_title(s: str) -> str:
    return _DUP_STRIP.sub("", str(s or ""))


def expected_counts(config_path: Path | None) -> int | None:
    if not config_path:
        return None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        q = cfg.get("question", {}) if isinstance(cfg.get("question"), dict) else {}
        counts = cfg.get("counts", q.get("counts", {}))
        per_round = sum(sum(v.values()) for v in counts.values() if isinstance(v, dict))
        rounds = max(1, int(cfg.get("rounds", q.get("rounds", 1))))
        return per_round * rounds or None
    except (OSError, json.JSONDecodeError, AttributeError, ValueError, TypeError):
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
        elif typ == "multiple" and not re.fullmatch(r"[A-H]+", answer.strip()):
            warns.append(f"{where} multiple answer 含非字母：{answer!r}")
        if isinstance(options, list) and isinstance(answer, str) and answer.strip():
            limit = 65 + len(options) - 1  # A + n-1
            bad = [c for c in answer.strip() if not (65 <= ord(c) <= limit)]
            if bad:
                errs.append(f"{where} answer {answer!r} 超出 options 范围（{len(options)} 项）")
            elif typ == "multiple" and answer != "".join(sorted(set(answer))):
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
        else:
            if len(title) < 150:
                warns.append(f"{where} group 根题干不足 150 字（{len(title)}）")
            for si, sub in enumerate(subs, 1):
                if not isinstance(sub, dict):
                    errs.append(f"{where} group 子题 {si} 不是对象")
                    continue
                if not str(sub.get("title") or "").strip():
                    errs.append(f"{where} group 子题 {si} title 为空")
                if "tags" in sub or "difficulty" in sub:
                    errs.append(f"{where} group 子题 {si} 不应带 tags/difficulty")
                st = sub.get("type")
                if st in {"choice", "multiple"}:
                    if not isinstance(sub.get("options"), list) or len(sub["options"]) < 2:
                        errs.append(f"{where} group 子题 {si} options 少于 2 项")
                    if not str(sub.get("answer") or "").strip():
                        errs.append(f"{where} group 子题 {si} answer 为空")
                elif st == "judge":
                    if sub.get("answer") not in ("是", "否"):
                        errs.append(f"{where} group 子题 {si} judge answer 应为 是/否")
    return typ


def expected_diff_ratios(config_path: Path | None) -> dict[str, float] | None:
    """从命题配置的 counts×rounds 算简单/中等/困难目标占比。"""
    if not config_path:
        return None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        q = cfg.get("question", {}) if isinstance(cfg.get("question"), dict) else {}
        counts = cfg.get("counts", q.get("counts", {}))
        rounds = max(1, int(cfg.get("rounds", q.get("rounds", 1))))
        tally = {name: 0.0 for name, _, _ in DIFF_BANDS}
        for row in counts.values():
            if not isinstance(row, dict):
                continue
            for name, _, _ in DIFF_BANDS:
                value = row.get(name, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    tally[name] += float(value) * rounds
        total = sum(tally.values())
        if not total:
            return None
        return {name: value / total for name, value in tally.items()}
    except (OSError, json.JSONDecodeError, AttributeError, ValueError, TypeError):
        return None


def collect_difficulties(q: dict) -> list[float]:
    out: list[float] = []

    def take(item) -> None:
        d = item.get("difficulty") if isinstance(item, dict) else None
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            out.append(float(d))

    take(q)
    subs = q.get("subs")
    if isinstance(subs, list):
        for sub in subs:
            take(sub)
    return out


def band_of(d: float) -> str | None:
    for name, lo, hi in DIFF_BANDS:
        if lo <= d <= hi:
            return name
    return None


def band_counts(values: list[float]) -> dict[str, int]:
    counts = {name: 0 for name, _, _ in DIFF_BANDS}
    for value in values:
        name = band_of(value)
        if name:
            counts[name] += 1
    return counts


def _band_text(values: list[float]) -> str:
    n = len(values)
    counts = band_counts(values)
    return "，".join(f"{name}{counts[name]}({counts[name] / n:.0%})" for name, _, _ in DIFF_BANDS)


def check_difficulty_distribution(
    chapter_diffs: dict[str, list[float]],
    all_diffs: list[float],
    target: dict[str, float] | None,
    errs: list[str],
    warns: list[str],
    strict: bool,
) -> None:
    """只报告难度分布，不修改任何题目的 difficulty。"""
    def add(msg: str, hard: bool = False) -> None:
        if hard and strict:
            errs.append(msg)
        else:
            warns.append(msg)

    if len(all_diffs) >= 100:
        counts = band_counts(all_diffs)
        n = len(all_diffs)
        if target:
            for name, ratio in target.items():
                actual = counts[name] / n
                diff = abs(actual - ratio)
                if diff > 0.25:
                    add(f"全书难度「{name}」占比 {actual:.0%}，目标 {ratio:.0%}，偏差 {diff:.0%}", hard=True)
                elif diff > 0.15:
                    add(f"全书难度「{name}」占比 {actual:.0%}，目标 {ratio:.0%}，偏差 {diff:.0%}")
        else:
            worst = max(counts.values()) / n
            if worst > 0.70:
                add(f"全书难度分布单一：{_band_text(all_diffs)}")

    for chapter, values in chapter_diffs.items():
        n = len(values)
        if n < 20:
            continue
        counts = band_counts(values)
        worst = max(counts.values()) / n
        if worst > 0.70:
            add(f"{chapter} 难度分布单一：{_band_text(values)}")
        if target:
            for name, ratio in target.items():
                actual = counts[name] / n
                diff = abs(actual - ratio)
                if diff > 0.20:
                    add(f"{chapter} 难度「{name}」占比 {actual:.0%}，目标 {ratio:.0%}，偏差 {diff:.0%}")


def cross_chapter_dups(records: list[tuple[str, str]], errs: list[str], warns: list[str]) -> None:
    """records = [(chapter_stem, title)]。跨章完全相同记错误，≥0.90 记警告。"""
    exact: dict[str, str] = {}
    buckets: dict[str, list[tuple[str, str, str]]] = {}
    reported = 0
    for chapter, title in records:
        n = norm_title(title)
        if not n:
            continue
        prev = exact.get(n)
        if prev is not None and prev != chapter:
            errs.append(f"跨章重复题：{prev} 与 {chapter} 题干完全相同：{title[:40]}")
        else:
            exact.setdefault(n, chapter)
        for n2, ch2, t2 in buckets.get(n[:10], []):
            if ch2 == chapter or n2 == n:
                continue
            if abs(len(n) - len(n2)) > 30:
                continue
            if SequenceMatcher(None, n, n2).ratio() >= 0.90:
                warns.append(
                    f"跨章近似题：{ch2} / {chapter} 相似度≥0.90：{t2[:30]} ↔ {title[:30]}"
                )
                reported += 1
                if reported >= 20:
                    warns.append("… 跨章近似题超过 20 对，其余省略")
                    return
        buckets.setdefault(n[:10], []).append((n, chapter, title))


def update_state(path: Path, ok: bool) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        stages = data.setdefault("stages", {})
        stages["questions"] = "done" if ok else "failed"
        stages.setdefault("compact", "pending")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已更新：{path} stages.questions={stages['questions']}")
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠ 无法更新 book.json：{e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="校验出题 JSON")
    p.add_argument("--id", required=True)
    p.add_argument("--book", required=True)
    p.add_argument("--config", help="命题配置 JSON（用于比对每章题数）")
    p.add_argument("--questions-dir", help="默认 补题/题目/<id><书名>")
    p.add_argument("--split-dir", help="拆分后的章节 md 目录，用于检查漏章")
    p.add_argument("--book-json", help="通过后写 stages.questions=done")
    p.add_argument("--strict-difficulty", action="store_true",
                   help="把严重的难度分布偏差视为错误（默认只警告）")
    args = p.parse_args(argv)

    if not re.fullmatch(r"\d+", str(args.id).strip()):
        print(f"id 必须是纯数字：{args.id}", file=sys.stderr)
        return 2
    book_id = int(args.id)
    book = args.book.strip()
    qdir = Path(args.questions_dir) if args.questions_dir else REPO_ROOT / "补题" / "题目" / f"{book_id}{book}"
    if not qdir.is_dir():
        print(f"题目目录不存在：{qdir}", file=sys.stderr)
        return 2

    expected = expected_counts(Path(args.config)) if args.config else None
    expected_ratio = expected_diff_ratios(Path(args.config)) if args.config else None
    errs: list[str] = []
    warns: list[str] = []
    type_dist: Counter = Counter()
    chapter_counts: dict[str, int] = {}
    chapter_diffs: dict[str, list[float]] = {}
    all_diffs: list[float] = []
    dedup_records: list[tuple[str, str]] = []

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
            if isinstance(q, dict):
                dedup_records.append((path.stem, str(q.get("title") or "")))
                diffs = collect_difficulties(q)
                chapter_diffs.setdefault(path.stem, []).extend(diffs)
                all_diffs.extend(diffs)
        if expected is not None:
            dev = abs(len(obj) - expected) / expected
            if dev > 0.2:
                warns.append(f"{path.name}: {len(obj)} 题，配置 {expected} 题，偏差 {dev:.0%}")

    # 章节齐全性
    split_dir = Path(args.split_dir) if args.split_dir else REPO_ROOT / "补题" / "拆分后" / f"{book_id}{book}"
    if split_dir.is_dir():
        expected_ch = {p.stem for p in split_dir.glob("*.md")} - SKIP_CHAPTERS
        missing = sorted(expected_ch - set(chapter_counts))
        if missing:
            errs.append(f"缺少章节 JSON {len(missing)} 个：{missing[:10]}")

    cross_chapter_dups(dedup_records, errs, warns)
    check_difficulty_distribution(
        chapter_diffs, all_diffs, expected_ratio, errs, warns, args.strict_difficulty
    )

    total = sum(chapter_counts.values())
    print(f"目录：{qdir}")
    print(f"章节：{len(chapter_counts)}  题目：{total}")
    for name, n in chapter_counts.items():
        print(f"  {n:>3}  {name}")
    if type_dist:
        print("题型分布：" + "，".join(f"{k}={v}" for k, v in sorted(type_dist.items())))
    if all_diffs:
        print("难度分布：" + _band_text(all_diffs))

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
        warns.append("没有 all.json（先运行命题官批量生成）")

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
        if args.book_json:
            update_state(Path(args.book_json), False)
        return 1
    if args.book_json:
        update_state(Path(args.book_json), True)
    print("\n通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
