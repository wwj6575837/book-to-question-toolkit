#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按「章 → 节」检查详细版/精简版有没有漏内容。只读，不修改文件。

用法：
    python check_coverage.py --detail "exports/.../<id>-1 <书名>.md" \
        --source "exports/Organized_Books/<id>-<书名>_organized"
    python check_coverage.py --detail "exports/.../<id>-1 <书名>.md" \
        --compact "exports/.../精简版/<id>-2 <书名>.md"

检查：
- 原文有的节，详细版是否都有（标题归一化后匹配，允许轻微改写）
- 详细版每节相对原文是否过短（默认 <20% 告警）
- 精简版是否保留详细版的每一节，以及每节是否过短（默认 <10% 告警）
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from mdscan import read_text_any

REPO_ROOT = Path(__file__).resolve().parents[4]
HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
CHAP = re.compile(r"^第\s*[\d一二三四五六七八九十百]+\s*(?:章|单元|讲|编|课|模块|项目|部分|篇)\s*")
SEC = re.compile(r"^第\s*[\d一二三四五六七八九十百]+\s*节\s*")
NUM = re.compile(r"^\d+\.\d+(?![\d.])\s*")
PUNCT = re.compile(r"[\s，。、；：:,.!?！？（）()\[\]【】《》<>\"'“”‘’]+")
SOURCE_JUNK = (".deepseek_from_source", ".doubao_from_source", ".merged_from_source",
               ".source.md", ".slice.md", ".chunk_stats")


def norm_title(text: str) -> str:
    s = SEC.sub("", text.strip())
    s = NUM.sub("", s)
    return PUNCT.sub("", s)


def norm_chapter(text: str) -> str:
    s = CHAP.sub("", text.strip())
    return PUNCT.sub("", s)


def is_section_title(text: str) -> bool:
    t = text.strip()
    return bool(SEC.match(t) or NUM.match(t))


def visible_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


IGNORE_SECTION = re.compile(
    r"^(学习目标|本章小结|本章自测|自测|课后练习|课后习题|练习题?|工作情景与任务)"
)
SUB_LIKE = re.compile(
    r"^(?:[一二三四五六七八九十百]+、|[（(]?\d+[）)]|作法|步骤|上篇|下篇|复习思考题|思考题)"
)
TRAIN = re.compile(r"^实训[一二三四五六七八九十\d]+")


def _ignored(title: str) -> bool:
    return bool(IGNORE_SECTION.match(norm_title(title)) or SUB_LIKE.match(title.strip()))


def _pattern_section(title: str) -> bool:
    t = title.strip()
    return bool((SEC.match(t) or NUM.match(t) or TRAIN.match(t)) and not _ignored(t))


def parse_doc(text: str) -> list[dict]:
    """返回 [{title, sections:[{title, body}]}]。

    源文件 `_full.md` 可能把所有标题都压成 #，所以优先按标题类型识别章/节；
    章内有编号节时，不再把无编号的 ### 当节（多半是小节）。
    """
    lines = text.splitlines()
    heads = []
    for i, line in enumerate(lines):
        m = HEAD.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    flat = bool(heads) and max(h[1] for h in heads) == 1

    chapter_idx = [i for i, (_l, _n, t) in enumerate(heads)
                   if CHAP.match(t.strip()) or t.strip().startswith("附录")]
    segments = []
    if not chapter_idx:
        segments.append((0, len(heads), False))
    else:
        if chapter_idx[0] > 0:
            segments.append((0, chapter_idx[0], False))
        for pos, start in enumerate(chapter_idx):
            end = chapter_idx[pos + 1] if pos + 1 < len(chapter_idx) else len(heads)
            segments.append((start, end, True))

    kinds: dict[int, str] = {}
    for start, end, has_chapter in segments:
        has_pattern = any(_pattern_section(heads[i][2]) for i in range(start, end))
        for i in range(start, end):
            _line_no, level, title = heads[i]
            if has_chapter and i == start:
                kinds[i] = "chapter"
            elif _pattern_section(title):
                kinds[i] = "section"
            elif not has_pattern and not flat and level == 3 and not _ignored(title):
                kinds[i] = "section"
            else:
                kinds[i] = "other"

    chapters: list[dict] = []
    cur = {"title": "", "sections": []}
    for idx, (line_no, _level, title) in enumerate(heads):
        kind = kinds.get(idx, "other")
        if kind == "chapter":
            if cur["title"] or cur["sections"]:
                chapters.append(cur)
            cur = {"title": title, "sections": []}
        elif kind == "section":
            end = len(lines)
            for j in range(idx + 1, len(heads)):
                if kinds.get(j) in ("chapter", "section"):
                    end = heads[j][0]
                    break
            body = "\n".join(lines[line_no + 1:end]).strip()
            cur["sections"].append({"title": title, "body": body})
    if cur["title"] or cur["sections"]:
        chapters.append(cur)
    return chapters


def source_files(path: Path) -> list[Path]:
    """每章只取最完整的一份：_full > .merged_from_source > 普通 .md。"""
    groups: dict[str, tuple[int, Path]] = {}
    for p in sorted(path.rglob("*.md")):
        name = p.name
        if name.startswith("000-"):
            continue
        if any(j in name for j in (".deepseek_from_source", ".doubao_from_source",
                                   ".source.md", ".slice.md", ".chunk_stats")):
            continue
        if name.endswith(".merged_from_source.md"):
            base, prio = name[:-len(".merged_from_source.md")], 1
        elif name.endswith("_full.md"):
            base, prio = name[:-len("_full.md")], 2
        elif name.endswith(".md"):
            base, prio = name[:-3], 0
        else:
            continue
        if base not in groups or prio > groups[base][0]:
            groups[base] = (prio, p)
    return [p for _prio, p in groups.values()]


def read_source(path: Path) -> list[dict]:
    seen: dict[str, dict] = {}
    for p in source_files(path):
        parsed = parse_doc(read_text_any(p)) or [{"title": p.stem, "sections": []}]
        for ch in parsed:
            key = norm_chapter(ch["title"]) or p.stem
            if key not in seen:
                seen[key] = ch
                continue
            have = {norm_title(s["title"]) for s in seen[key]["sections"]}
            for sec in ch["sections"]:
                if norm_title(sec["title"]) not in have:
                    seen[key]["sections"].append(sec)
    return list(seen.values())


def match_chapters(src: list[dict], dst: list[dict]):
    used, pairs, missing = set(), [], []
    for s in src:
        best, score = None, 0.0
        for i, d in enumerate(dst):
            if i in used:
                continue
            a, b = norm_chapter(s["title"]), norm_chapter(d["title"])
            if not a or not b:
                continue
            r = 1.0 if (a == b or a in b or b in a) else SequenceMatcher(None, a, b).ratio()
            if r > score:
                best, score = i, r
        if best is not None and score >= 0.60:
            used.add(best)
            pairs.append((s, dst[best]))
        else:
            missing.append(s)
    extra = [d for i, d in enumerate(dst) if i not in used]
    return pairs, missing, extra


def match_sections(src_secs: list[dict], dst_secs: list[dict]):
    used, pairs, missing = set(), [], []
    for s in src_secs:
        best, score = None, 0.0
        sn = norm_title(s["title"])
        for i, d in enumerate(dst_secs):
            if i in used:
                continue
            dn = norm_title(d["title"])
            r = 1.0 if sn == dn else SequenceMatcher(None, sn, dn).ratio()
            if r > score:
                best, score = i, r
        if best is not None and score >= 0.80:
            used.add(best)
            pairs.append((s, dst_secs[best]))
        else:
            missing.append(s)
    extra = [d for i, d in enumerate(dst_secs) if i not in used]
    return pairs, missing, extra


def chapter_ranges(lines: list[str]) -> list[tuple[int, int, str]]:
    heads = [(i, m.group(2).strip()) for i, line in enumerate(lines)
             if (m := HEAD.match(line))]
    chapters = [(i, t) for i, t in heads
                if CHAP.match(t) or t.startswith("附录")]
    out = []
    for pos, (start, title) in enumerate(chapters):
        end = chapters[pos + 1][0] if pos + 1 < len(chapters) else len(lines)
        out.append((start, end, title))
    return out


def split_chapter_sections(lines: list[str], start: int, end: int):
    secs = []
    first = None
    cur = None
    for i in range(start + 1, end):
        m = HEAD.match(lines[i])
        if not m:
            continue
        title = m.group(2).strip()
        if _pattern_section(title) and not _ignored(title):
            if cur is not None:
                secs.append((cur[0], lines[cur[1]:i]))
            else:
                first = i
            cur = (title, i)
    if cur is not None:
        secs.append((cur[0], lines[cur[1]:end]))
    preamble_end = first if first is not None else end
    return lines[start:preamble_end], secs


def agent_section_blocks(source: Path, chapter_title: str, need: list[str]) -> dict[str, tuple[str, str]]:
    n = chapter_no(chapter_title, source)
    files: list[Path] = []
    if n:
        for ext in ("doubao_from_source.md", "deepseek_from_source.md"):
            files.extend(sorted(source.glob(f"*第{n}章*.{ext}")))
    found: dict[str, tuple[str, str]] = {}
    for p in files:
        for ch in parse_doc(read_text_any(p)):
            if ch["title"] and chapter_title:
                r = SequenceMatcher(None, norm_chapter(ch["title"]), norm_chapter(chapter_title)).ratio()
                if r < 0.60:
                    continue
            for sec in ch["sections"]:
                for title in need:
                    key = norm_title(title)
                    if key in found:
                        continue
                    r = SequenceMatcher(None, norm_title(sec["title"]), key).ratio()
                    if r >= 0.85:
                        found[key] = (sec["title"], sec["body"])
    return found


def agent_block(title: str, body: str) -> list[str]:
    out = [f"### {title}", ""]
    for ln in body.splitlines():
        m = HEAD.match(ln)
        if m:
            level = max(4, len(m.group(1)))
            out.append("#" * level + " " + m.group(2).strip())
        else:
            out.append(ln)
    return out


def fill_missing(detail: Path, source: Path, result: dict, apply: bool) -> tuple[int, list[tuple[str, str]]]:
    text = read_text_any(detail)
    lines = text.splitlines()
    src_by_norm = {norm_chapter(ch["title"]): ch for ch in read_source(source)}
    missing_by_ch: dict[str, list[str]] = {}
    for ch, sec in result.get("missing_items") or []:
        missing_by_ch.setdefault(ch, []).append(sec)
    replacements = []
    filled = 0
    still: list[tuple[str, str]] = []
    for start, end, ch_title in chapter_ranges(lines):
        if ch_title not in missing_by_ch:
            continue
        src_ch = src_by_norm.get(norm_chapter(ch_title))
        if src_ch is None:
            continue
        missing = {norm_title(t) for t in missing_by_ch[ch_title]}
        preamble, dsecs = split_chapter_sections(lines, start, end)
        dmap = {norm_title(t): (t, block) for t, block in dsecs}
        need = [s["title"] for s in src_ch["sections"] if norm_title(s["title"]) in missing]
        ab = agent_section_blocks(source, ch_title, need)
        new = list(preamble)
        used = set()
        for sec in src_ch["sections"]:
            k = norm_title(sec["title"])
            if k in dmap:
                new.extend(dmap[k][1])
                used.add(k)
            elif k in missing:
                if k in ab:
                    new.extend(agent_block(*ab[k]))
                    filled += 1
                    used.add(k)
                else:
                    still.append((ch_title, sec["title"]))
        src_keys = {norm_title(s["title"]) for s in src_ch["sections"]}
        for k, (_t, block) in dmap.items():
            if k not in used and k not in src_keys:
                new.extend(block)
        replacements.append((start, end, new))
    if apply and filled:
        for start, end, new in sorted(replacements, reverse=True):
            lines[start:end] = new
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(detail, detail.with_name(detail.name + f".bak-fill-{stamp}"))
        detail.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8-sig")
    return filled, still


def check_detail(detail: Path, source: Path, min_ratio: float, errors: list, warns: list):
    src_chapters = read_source(source)
    det_chapters = parse_doc(read_text_any(detail))
    pairs, missing_ch, extra_ch = match_chapters(src_chapters, det_chapters)
    for ch in missing_ch:
        errors.append(f"详细版缺少章节：{ch['title'] or '(无标题)'}")
    for ch in extra_ch:
        warns.append(f"详细版多出章节：{ch['title'] or '(无标题)'}")

    total = missing = short = 0
    missing_items, missing_items_for_rerun, extra_items = [], [], []
    for s_ch, d_ch in pairs:
        pairs_s, miss_s, extra_s = match_sections(s_ch["sections"], d_ch["sections"])
        total += len(s_ch["sections"])
        missing_items.extend((s_ch["title"], sec) for sec in miss_s)
        extra_items.extend((d_ch["title"], sec) for sec in extra_s)
        for s_sec, d_sec in pairs_s:
            if not d_sec["body"]:
                errors.append(f"详细版节为空：{d_ch['title']} / {d_sec['title']}")
                continue
            src_len = visible_len(s_sec["body"])
            det_len = visible_len(d_sec["body"])
            if src_len >= 80 and det_len / src_len < min_ratio:
                short += 1
                warns.append(
                    f"详细版节过短：{d_ch['title']} / {d_sec['title']} "
                    f"({det_len}/{src_len}，{det_len / src_len:.0%})"
                )
    # 节可能在详细版里被挪到别的章（或原文重复出现）：只要全书标题能对上，就不算漏
    all_dst = [(ch["title"], sec) for ch in det_chapters for sec in ch["sections"]]
    for s_ch, sec in missing_items:
        sn = norm_title(sec["title"])
        best, score = None, 0.0
        for i, (_d_ch, dsec) in enumerate(all_dst):
            dn = norm_title(dsec["title"])
            r = 1.0 if sn == dn else SequenceMatcher(None, sn, dn).ratio()
            if r > score:
                best, score = i, r
        if best is not None and score >= 0.90:
            d_ch, dsec = all_dst[best]
            warns.append(f"节位置不同：{s_ch} / {sec['title']} → 详细版 {d_ch} / {dsec['title']}")
        else:
            missing += 1
            missing_items_for_rerun.append((s_ch, sec["title"]))
            errors.append(f"详细版缺少节：{s_ch} / {sec['title']}")
    for d_ch, sec in extra_items:
        warns.append(f"详细版多出节：{d_ch} / {sec['title']}")
    return {"source_sections": total, "missing_sections": missing, "short_sections": short,
            "missing_chapters": [ch["title"] for ch in missing_ch],
            "missing_items": missing_items_for_rerun}


def check_compact(detail: Path, compact: Path, min_ratio: float, errors: list, warns: list):
    d_chapters = parse_doc(read_text_any(detail))
    c_chapters = parse_doc(read_text_any(compact))
    pairs, missing_ch, extra_ch = match_chapters(d_chapters, c_chapters)
    for ch in missing_ch:
        errors.append(f"精简版缺少章节：{ch['title'] or '(无标题)'}")
    for ch in extra_ch:
        warns.append(f"精简版多出章节：{ch['title'] or '(无标题)'}")

    total = missing = short = 0
    missing_items, extra_items = [], []
    for d_ch, c_ch in pairs:
        pairs_s, miss_s, extra_s = match_sections(d_ch["sections"], c_ch["sections"])
        total += len(d_ch["sections"])
        missing_items.extend((d_ch["title"], sec) for sec in miss_s)
        extra_items.extend((c_ch["title"], sec) for sec in extra_s)
        for d_sec, c_sec in pairs_s:
            if not c_sec["body"]:
                errors.append(f"精简版节为空：{c_ch['title']} / {c_sec['title']}")
                continue
            det_len = visible_len(d_sec["body"])
            cmp_len = visible_len(c_sec["body"])
            if det_len >= 80 and cmp_len / det_len < min_ratio:
                short += 1
                warns.append(
                    f"精简版节过短：{c_ch['title']} / {c_sec['title']} "
                    f"({cmp_len}/{det_len}，{cmp_len / det_len:.0%})"
                )
    all_dst = [(ch["title"], sec) for ch in c_chapters for sec in ch["sections"]]
    for d_ch, sec in missing_items:
        sn = norm_title(sec["title"])
        best, score = None, 0.0
        for i, (_c_ch, csec) in enumerate(all_dst):
            cn = norm_title(csec["title"])
            r = 1.0 if sn == cn else SequenceMatcher(None, sn, cn).ratio()
            if r > score:
                best, score = i, r
        if best is not None and score >= 0.90:
            c_ch, csec = all_dst[best]
            warns.append(f"节位置不同：{d_ch} / {sec['title']} → 精简版 {c_ch} / {csec['title']}")
        else:
            missing += 1
            errors.append(f"精简版缺少节：{d_ch} / {sec['title']}")
    for c_ch, sec in extra_items:
        warns.append(f"精简版多出节：{c_ch} / {sec['title']}")
    return {"detail_sections": total, "missing_sections": missing, "short_sections": short}


_CN = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def cn_to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    total = section = 0
    for ch in text:
        if ch in _CN and ch not in ("十", "百"):
            section = _CN[ch]
        elif ch == "十":
            total += (section or 1) * 10
            section = 0
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return None
    return total + section


def chapter_no(title: str, source: Path | None) -> int | None:
    m = re.search(r"第\s*([0-9一二三四五六七八九十百]+)\s*章", title)
    if m:
        return cn_to_int(m.group(1))
    if source and "附录" in title:
        for p in source_files(source):
            if "附录" in p.name:
                m2 = re.search(r"第\s*(\d+)\s*章", p.name)
                if m2:
                    return int(m2.group(1))
    return None


def rerun_missing(detail: Path, source: Path, result: dict, yes: bool) -> None:
    m = re.match(r"^(\d+)-1\s+(.+)\.md$", detail.name)
    if not m:
        print("无法从文件名解析书籍 id，跳过重跑", file=sys.stderr)
        return
    book_id = int(m.group(1))
    titles = set(result.get("missing_chapters") or [])
    titles |= {ch for ch, _sec in (result.get("missing_items") or [])}
    nums = sorted({n for t in titles if (n := chapter_no(t, source))})
    if not nums:
        print("无法定位缺失节所属章号，跳过重跑", file=sys.stderr)
        return
    cmd = [sys.executable, "-m", "multi_agent.chapter_pipeline",
           "--ids", str(book_id), "--chapters", *[str(n) for n in nums],
           "--force", "--auto-chunk", "--max-workers", "6",
           "--chapter-max-workers", "7", "--resume-partial",
           "--limit-deepseek", "15", "--limit-doubao", "15",
           "--limit-merge", "8", "--retries", "2", "--retry-backoff", "10"]
    print("\n重跑命令（整章重跑）：\n  " + " ".join(cmd))
    if not yes:
        print("确认后加 --yes 执行。")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(detail, detail.with_name(detail.name + f".bak-rerun-{stamp}"))
    rc = subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode
    print(f"重跑结束，退出码 {rc}")
    if rc != 0:
        return
    errors, warns = [], []
    stats = check_detail(detail, source, 0.20, errors, warns)
    print(f"重跑后：缺失节 {stats['missing_sections']}，过短 {stats['short_sections']}")
    for e in errors[:20]:
        print(f"  ✗ {e}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="按章/节检查详细版或精简版的完整性（只读）")
    p.add_argument("--detail", required=True, help="详细版 Markdown")
    p.add_argument("--source", help="原书 organized/拆分 目录")
    p.add_argument("--compact", help="精简版 Markdown；给出时额外做详细版→精简版比对")
    p.add_argument("--min-ratio", type=float, default=0.20,
                   help="详细版节长度 / 原文节长度 的最低比例（默认 0.20）")
    p.add_argument("--compact-min-ratio", type=float, default=0.10,
                   help="精简版节长度 / 详细版节长度 的最低比例（默认 0.10）")
    p.add_argument("--fill", action="store_true",
                   help="先从豆包/DeepSeek 分节产物补缺失节；不调模型")
    p.add_argument("--rerun", action="store_true",
                   help="对仍缺失的节所在章重跑 chapter_pipeline（整章重跑，会调模型）")
    p.add_argument("--yes", action="store_true", help="与 --fill/--rerun 一起用，真正写盘/执行")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    detail = Path(args.detail).expanduser()
    if not detail.is_file():
        print(f"详细版不存在：{detail}", file=sys.stderr)
        return 2
    source = Path(args.source).expanduser() if args.source else None
    compact = Path(args.compact).expanduser() if args.compact else None
    if source and not source.is_dir():
        print(f"源目录不存在：{source}", file=sys.stderr)
        return 2
    if compact and not compact.is_file():
        print(f"精简版不存在：{compact}", file=sys.stderr)
        return 2
    if not source and not compact:
        p.error("--source 与 --compact 至少给一个")

    errors: list[str] = []
    warns: list[str] = []
    result = {}
    if source:
        result["detail"] = check_detail(detail, source, args.min_ratio, errors, warns)
    if compact:
        result["compact"] = check_compact(detail, compact, args.compact_min_ratio, errors, warns)

    if (args.fill or args.rerun) and source and result.get("detail", {}).get("missing_sections"):
        filled, still = fill_missing(detail, source, result["detail"], apply=args.yes)
        print(f"从 agent 产物补节：{filled} 节；仍缺 {len(still)} 节")
        if args.yes and filled:
            errors, warns = [], []
            result["detail"] = check_detail(detail, source, args.min_ratio, errors, warns)
            if compact:
                e2, w2 = [], []
                result["compact"] = check_compact(detail, compact, args.compact_min_ratio, e2, w2)
                errors += e2
                warns += w2

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warns, "stats": result},
                         ensure_ascii=False, indent=2))
    else:
        print(f"详细版：{detail.name}")
        if source:
            s = result["detail"]
            print(f"原文节：{s['source_sections']}  缺失：{s['missing_sections']}  过短：{s['short_sections']}")
        if compact:
            s = result["compact"]
            print(f"详细版节：{s['detail_sections']}  精简版缺失：{s['missing_sections']}  过短：{s['short_sections']}")
        for e in errors[:80]:
            print(f"  ✗ {e}")
        if len(errors) > 80:
            print(f"  … 其余 {len(errors) - 80} 条省略")
        for w in warns[:80]:
            print(f"  ⚠ {w}")
        if len(warns) > 80:
            print(f"  … 其余 {len(warns) - 80} 条省略")
        print("\n通过。" if not errors else f"\n错误 {len(errors)} 条。")
    if args.rerun and errors and source and "detail" in result:
        rerun_missing(detail, source, result["detail"], args.yes)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
