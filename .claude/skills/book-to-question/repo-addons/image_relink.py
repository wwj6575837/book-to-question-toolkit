#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精简版按详细版回链图片：确定性修正 URL、补缩略参数、可选去重。

详细版是唯一权威来源。精简版里的图片按 canonical key（去 query、/images/tp/ 归一）
与图注对齐，匹配不到时按文档顺序兜底；表格内图片只换 src，不转箭头块。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from markdown_image_blocks import (
    _FLEX_BLOCK,
    _IMG_TAG,
    _SRC_ATTR,
    append_thumbnail_param,
    clean_caption,
    normalize_image_blocks,
)

_WIDTH_PX = re.compile(r"\bwidth\s*=\s*(['\"])(\d+(?:\.\d+)?)(?:px)?\1", re.IGNORECASE)

CDN_HOST = "cdn.maiziedu.cn"

_ARROW_IMG = re.compile(
    r"(?m)^[ \t]*(?:[-*+][ \t]+)?->!\[(?P<alt>[^\]]*)\]"
    r"\((?P<url>[^)\n]+)\)<-[ \t]*$"
)
_CAPTION_AFTER = re.compile(
    r"[ \t]*\n(?:[ \t]*\n)?[ \t]*->(?!!\[)(?P<cap>.*?)<-[ \t]*(?=\n|$)"
)
_MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\n]+)\)")
_HTML_TABLE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_PIPE_LINE = re.compile(r"(?m)^[ \t]*\|.*\|[ \t]*$")


# ============================================================
# 基础工具
# ============================================================

def canonical_key(url: str) -> str:
    """URL 归一：去 query/fragment，/images/tp/ 视为 /images/。"""
    u = (url or "").strip()
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.replace("/images/tp/", "/images/")


def caption_key(text: str) -> str:
    """图注归一：去空白与全/半角标点差异，用于匹配。"""
    s = clean_caption(text or "")
    s = re.sub(r"\s+", "", s)
    return (s.replace("（", "(").replace("）", ")")
             .replace("：", ":").replace("，", ",")
             .replace("；", ";").replace("～", "~"))


def _is_cdn(url: str) -> bool:
    try:
        return urlparse(url).netloc.lower() == CDN_HOST
    except ValueError:
        return False


def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n") + 1


# ============================================================
# 解析
# ============================================================

@dataclass
class ImageRef:
    src: str
    start: int                 # src 在原文中的起止
    end: int
    caption: str = ""
    kind: str = "md"           # flex | arrow | md | img
    in_table: bool = False
    block_start: int = -1      # 去重时可删除的整块范围
    block_end: int = -1
    line: int = 0
    raw_tag: str = ""

    @property
    def key(self) -> str:
        return canonical_key(self.src)

    @property
    def cap_key(self) -> str:
        return caption_key(self.caption)


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _is_pipe_line(text: str, pos: int) -> bool:
    ls = text.rfind("\n", 0, pos) + 1
    le = text.find("\n", pos)
    le = len(text) if le < 0 else le
    return text[ls:le].lstrip().startswith("|")


def parse_refs(text: str) -> list[ImageRef]:
    """解析所有图片引用；flex/箭头块优先，避免同一张图重复计数。"""
    refs: list[ImageRef] = []
    protected: list[tuple[int, int]] = []
    html_tables = [(m.start(), m.end()) for m in _HTML_TABLE.finditer(text)]

    def in_table(pos: int) -> bool:
        return _in_spans(pos, html_tables) or _is_pipe_line(text, pos)

    # 1) HTML flex 块
    for m in _FLEX_BLOCK.finditer(text):
        imgs = m.group("imgs")
        base = m.start("imgs")
        cap = clean_caption(m.group("cap") or "")
        for tag_m in _IMG_TAG.finditer(imgs):
            src_m = _SRC_ATTR.search(tag_m.group(0))
            if not src_m:
                continue
            s = base + tag_m.start() + src_m.start(2)
            e = base + tag_m.start() + src_m.end(2)
            refs.append(ImageRef(
                src=src_m.group(2), start=s, end=e, caption=cap,
                kind="flex", in_table=in_table(m.start()),
                block_start=m.start(), block_end=m.end(),
                line=_line_of(text, s), raw_tag=tag_m.group(0),
            ))
        protected.append((m.start(), m.end()))

    # 2) 箭头行（允许 "- " 列表前缀）
    for m in _ARROW_IMG.finditer(text):
        if _in_spans(m.start(), protected):
            continue
        block_end = m.end()
        cap = ""
        cm = _CAPTION_AFTER.match(text, m.end())
        if cm:
            cap = clean_caption(cm.group("cap"))
            block_end = cm.end()
        refs.append(ImageRef(
            src=m.group("url"), start=m.start("url"), end=m.end("url"),
            caption=cap, kind="arrow", in_table=in_table(m.start("url")),
            block_start=m.start(), block_end=block_end,
            line=_line_of(text, m.start("url")),
        ))
        protected.append((m.start(), block_end))

    # 3) 剩余的 Markdown 图片与 <img>
    for m in _MD_IMAGE.finditer(text):
        if _in_spans(m.start(), protected):
            continue
        refs.append(ImageRef(
            src=m.group("url"), start=m.start("url"), end=m.end("url"),
            caption=m.group("alt") or "", kind="md", in_table=in_table(m.start("url")),
            line=_line_of(text, m.start("url")),
        ))
    for m in _IMG_TAG.finditer(text):
        if _in_spans(m.start(), protected):
            continue
        src_m = _SRC_ATTR.search(m.group(0))
        if not src_m:
            continue
        refs.append(ImageRef(
            src=src_m.group(2), start=m.start() + src_m.start(2),
            end=m.start() + src_m.end(2), kind="img",
            in_table=in_table(m.start()), line=_line_of(text, m.start()),
            raw_tag=m.group(0),
        ))

    refs.sort(key=lambda r: r.start)
    return refs


# ============================================================
# 索引与对齐
# ============================================================

@dataclass
class DetailIndex:
    by_key: dict[str, list[ImageRef]] = field(default_factory=dict)
    by_caption: dict[str, list[ImageRef]] = field(default_factory=dict)
    body_order: list[ImageRef] = field(default_factory=list)
    table_order: list[ImageRef] = field(default_factory=list)


def build_detail_index(refs: list[ImageRef]) -> DetailIndex:
    idx = DetailIndex()
    for r in refs:
        if not r.src:
            continue
        idx.by_key.setdefault(r.key, []).append(r)
        if r.cap_key:
            idx.by_caption.setdefault(r.cap_key, []).append(r)
        (idx.table_order if r.in_table else idx.body_order).append(r)
    return idx


@dataclass
class Stats:
    total: int = 0
    table: int = 0
    fixed_key: int = 0
    fixed_caption: int = 0
    fixed_order: int = 0
    wrong_links: int = 0
    duplicates: int = 0
    detail_only: int = 0
    unmatched: int = 0
    non_cdn: int = 0
    removed: int = 0
    changed: bool = False
    samples: dict[str, list[str]] = field(default_factory=dict)

    def add(self, tag: str, msg: str) -> None:
        self.samples.setdefault(tag, []).append(msg)


def _match(ref: ImageRef, idx: DetailIndex, used: set[int]) -> tuple[ImageRef | None, str]:
    caps = idx.by_caption.get(ref.cap_key, []) if ref.cap_key else []
    candidates = idx.by_key.get(ref.key, [])

    # 图注唯一且指向另一张图 —— 错链修正优先
    if len(caps) == 1 and ref.key and canonical_key(caps[0].src) != ref.key:
        if id(caps[0]) not in used:
            used.add(id(caps[0]))
            return caps[0], "caption"
    # key 命中
    if candidates:
        if ref.cap_key:
            for d in candidates:
                if id(d) not in used and canonical_key(d.src) == ref.key:
                    used.add(id(d))
                    return d, "key"
        for d in candidates:
            if id(d) not in used:
                used.add(id(d))
                return d, "key"
        return candidates[0], "duplicate"
    # key 未命中，图注唯一
    if len(caps) == 1:
        if id(caps[0]) not in used:
            used.add(id(caps[0]))
            return caps[0], "caption"
        return caps[0], "duplicate"
    # 顺序兜底：只对空图注或详细版里重复出现的图注，避免瞎猜
    if not ref.cap_key or caps:
        pool = idx.table_order if ref.in_table else idx.body_order
        for d in pool:
            if id(d) not in used:
                used.add(id(d))
                return d, "order"
    return None, "unmatched"


def _target_src(ref: ImageRef, detail: ImageRef, pct: int) -> str:
    url = detail.src
    if ref.kind == "img" and _WIDTH_PX.search(ref.raw_tag or ""):
        return url
    return append_thumbnail_param(url, pct)


def relink_text(compact: str, detail: str, pct: int = 50,
                dedupe: bool = False, normalize: bool = True) -> tuple[str, Stats]:
    stats = Stats()
    if not compact or not detail:
        return compact, stats

    c_refs = parse_refs(compact)
    d_refs = parse_refs(detail)
    idx = build_detail_index(d_refs)
    used: set[int] = set()
    edits: list[tuple[int, int, str]] = []
    remove_spans: set[tuple[int, int]] = set()
    used_keys: set[str] = set()
    refs_per_block: dict[tuple[int, int], int] = {}
    for r in c_refs:
        if r.block_start >= 0:
            key = (r.block_start, r.block_end)
            refs_per_block[key] = refs_per_block.get(key, 0) + 1

    for ref in c_refs:
        stats.total += 1
        if ref.in_table:
            stats.table += 1
        if not ref.src:
            continue
        if not _is_cdn(ref.src):
            stats.non_cdn += 1
            continue

        match, how = _match(ref, idx, used)
        if match is None:
            stats.unmatched += 1
            stats.add("unmatched", f"L{ref.line} {ref.caption or ref.key}")
            continue

        used_keys.add(match.key)
        if how == "duplicate":
            stats.duplicates += 1
            stats.add("duplicate", f"L{ref.line} {ref.caption or match.key}")
            block = (ref.block_start, ref.block_end)
            # 只删单图块；多图块里可能还有别的图，交给人工
            if dedupe and ref.block_start >= 0 and refs_per_block.get(block) == 1:
                remove_spans.add(block)
            continue

        if how == "caption":
            stats.fixed_caption += 1
            stats.wrong_links += 1
            stats.add("wrong_links",
                      f"L{ref.line} {ref.caption!r}: {ref.key.rsplit('/', 1)[-1]}"
                      f" → {match.key.rsplit('/', 1)[-1]}")
        elif how == "order":
            stats.fixed_order += 1
            stats.add("order", f"L{ref.line} {ref.caption or '(无图注)'} → "
                               f"{match.key.rsplit('/', 1)[-1]}")

        new_src = _target_src(ref, match, pct)
        if new_src != ref.src:
            edits.append((ref.start, ref.end, new_src))
            if how == "key":
                stats.fixed_key += 1

    # 详细版有、精简版没有的图
    detail_only = [d for d in d_refs
                   if d.src and d.key and d.key not in used_keys]
    stats.detail_only = len(detail_only)
    for d in detail_only[:10]:
        stats.add("detail_only", f"L{d.line} {d.caption or d.key.rsplit('/', 1)[-1]}")

    if not edits and not remove_spans:
        stats.changed = False
        return compact, stats

    # 去重块与 URL 替换一起按位置倒序应用；只清理块尾空行，不全局压空行
    for s, e in remove_spans:
        tail = re.match(r"[ \t]*(?:\n[ \t]*)*", compact[e:])
        e2 = e + (tail.end() if tail else 0)
        pre = compact[:s]
        repl = "\n\n" if pre and not pre.endswith("\n") else ""
        edits.append((s, e2, repl))
    edits.sort(key=lambda x: x[0], reverse=True)
    text = compact
    for s, e, repl in edits:
        text = text[:s] + repl + text[e:]
    stats.removed = len(remove_spans)
    if normalize:
        text = normalize_image_blocks(text, pct)
    stats.changed = text != compact
    return text, stats


# ============================================================
# 文件与报告
# ============================================================

def _read(path: str | Path) -> tuple[str, bool]:
    raw = Path(path).read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig"), bom


def relink_file(compact_path: str | Path, detail_path: str | Path,
                dedupe: bool = False, pct: int = 50, normalize: bool = True,
                backup: bool = False) -> Stats:
    ctext, bom = _read(compact_path)
    dtext, _ = _read(detail_path)
    new, stats = relink_text(ctext, dtext, pct=pct, dedupe=dedupe,
                             normalize=normalize)
    if not stats.changed:
        return stats
    path = Path(compact_path)
    if backup:
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path.with_name(f"{path.name}.bak-relink-{stamp}").write_bytes(path.read_bytes())
    path.write_text(new, encoding="utf-8-sig" if bom else "utf-8")
    return stats


def format_report(stats: Stats, title: str = "") -> str:
    lines = []
    if title:
        lines.append(title)
    lines.append(
        f"图片引用 {stats.total}（表格 {stats.table}）｜"
        f"key 命中修正 {stats.fixed_key}｜图注修正 {stats.fixed_caption}｜"
        f"顺序兜底 {stats.fixed_order}"
    )
    lines.append(
        f"错链 {stats.wrong_links}｜重复 {stats.duplicates}"
        f"{f'（删除 {stats.removed}）' if stats.removed else ''}｜"
        f"详细版缺图 {stats.detail_only}｜无法对齐 {stats.unmatched}｜"
        f"非 CDN {stats.non_cdn}"
    )
    labels = {
        "wrong_links": "错链",
        "duplicate": "重复图",
        "detail_only": "详细版有、精简版无",
        "unmatched": "无法对齐",
        "order": "顺序兜底",
    }
    for tag, label in labels.items():
        items = stats.samples.get(tag) or []
        if not items:
            continue
        lines.append(f"  {label}（前 {min(len(items), 10)} 条）：")
        lines.extend(f"    - {x}" for x in items[:10])
    return "\n".join(lines)
