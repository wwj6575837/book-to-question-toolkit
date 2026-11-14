#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""箭头居中图片块：生成、七牛缩放参数、旧 HTML 块归一化。"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

DEFAULT_THUMBNAIL_PCT = 50
CDN_HOST = "cdn.maiziedu.cn"
_THUMB_PARAM = "imageMogr2/thumbnail/"

_FLEX_BLOCK = re.compile(
    r'<div\b[^>]*\bstyle\s*=\s*"[^"]*display\s*:\s*flex[^"]*"[^>]*>'
    r"\s*(?P<imgs>(?:<img\b[^>\n]*>\s*)+)"
    r"(?P<capblock><div\b[^>]*>(?P<cap>.*?)</div>\s*)?"
    r"</div>",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_BLOCK = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_IMG_TAG = re.compile(r"<img\b[^>\n]*>", re.IGNORECASE)
_SRC_ATTR = re.compile(r"\bsrc\s*=\s*(['\"])([^'\"]+)\1", re.IGNORECASE)
_MD_IMAGE_ANY = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")
_MD_IMAGE_ONLY_LINE = re.compile(r"^[ \t]*!\[([^\]]*)\]\(([^)\s]+)\)[ \t]*$", re.MULTILINE)
_ARROW_IMAGE_LINE = re.compile(
    r"^(?P<pre>[ \t]*->[ \t]*)"
    r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\n]+)\)"
    r"(?P<post>[ \t]*<-[ \t]*)$",
    re.MULTILINE,
)
_ARROW_CAPTION_GAP = re.compile(
    r"(?m)^(->!\[[^\]]*\]\([^)]*\)<-)[ \t]*\n[ \t]*(->[^\n]*<-)$"
)
_ARROW_ADJACENT_GAP = re.compile(r"(?m)^(->.*?<-)[ \t]*\n(?=->)")
_WIDTH_PCT = re.compile(r"\bwidth\s*=\s*(['\"])(\d+(?:\.\d+)?)%\1", re.IGNORECASE)
_WIDTH_PX = re.compile(r"\bwidth\s*=\s*(['\"])(\d+(?:\.\d+)?)(?:px)?\1", re.IGNORECASE)
_PUA = re.compile(r"[\ue000-\uf8ff]")


def _is_cdn_url(url: str) -> bool:
    try:
        return urlparse(url).netloc.lower() == CDN_HOST
    except ValueError:
        return False


def append_thumbnail_param(url: str, pct: int = DEFAULT_THUMBNAIL_PCT) -> str:
    """给七牛 CDN 图片追加 ?imageMogr2/thumbnail/!Np；幂等，非 CDN 域名不动。"""
    url = (url or "").strip()
    if not url or pct <= 0 or not _is_cdn_url(url) or _THUMB_PARAM in url:
        return url
    frag = ""
    if "#" in url:
        url, frag = url.split("#", 1)
        frag = "#" + frag
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{_THUMB_PARAM}!{pct}p{frag}"


def clean_caption(raw: str) -> str:
    if not raw:
        return ""
    s = html.unescape(raw)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    s = _PUA.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _pct_from_width(tag: str, default: int) -> int:
    """优先沿用 <img width="N%"> 的缩放比例，无百分比宽度时回退默认值。"""
    m = _WIDTH_PCT.search(tag or "")
    if not m:
        return default
    try:
        value = int(float(m.group(2)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def arrow_image_block(src: str, caption: str = "", pct: int = DEFAULT_THUMBNAIL_PCT) -> str:
    """输出 ->![](url?imageMogr2/thumbnail/!50p)<- ，有图注时再接一个空行和 ->图注<-。

    返回值不带末尾换行，由调用点的上下文换行决定空行数。
    """
    url = append_thumbnail_param(src, pct)
    line = f"->![]({url})<-"
    cap = clean_caption(caption)
    return f"{line}\n\n->{cap}<-" if cap else line


def thumbnail_img_tag(tag: str, pct: int = DEFAULT_THUMBNAIL_PCT) -> str:
    """保留 <img> 标签其余属性，只给 src 追加七牛缩放参数。"""
    return _SRC_ATTR.sub(
        lambda m: f"src={m.group(1)}{append_thumbnail_param(m.group(2), pct)}{m.group(1)}",
        tag,
        count=1,
    )


def normalize_image_blocks(text: str, pct: int = DEFAULT_THUMBNAIL_PCT) -> str:
    """把旧 HTML flex 图片块、孤立 Markdown 图片行统一成箭头居中块。

    表格内的图片只追加缩放参数，保持行内，避免破坏表格。
    """
    if not text:
        return text

    table_spans = [(m.start(), m.end()) for m in _TABLE_BLOCK.finditer(text)]

    def in_table(pos: int) -> bool:
        return any(a <= pos < b for a, b in table_spans)

    def flex_repl(m: re.Match) -> str:
        imgs = m.group("imgs")
        pairs = []
        for tag in _IMG_TAG.findall(imgs):
            src = _SRC_ATTR.search(tag)
            if src:
                pairs.append((src.group(2), _pct_from_width(tag, pct)))
        if not pairs:
            return m.group(0)
        if in_table(m.start()):
            return thumbnail_img_tag(imgs, pct)
        pieces = [arrow_image_block(url, "", image_pct) for url, image_pct in pairs]
        cap = clean_caption(m.group("cap") or "")
        if cap:
            pieces.append(f"->{cap}<-")
        return "\n\n".join(pieces)

    text = _FLEX_BLOCK.sub(flex_repl, text)

    # 已有箭头块里的图片：补缩放参数
    text = _ARROW_IMAGE_LINE.sub(
        lambda m: (
            f"{m.group('pre')}![{m.group('alt')}]"
            f"({append_thumbnail_param(m.group('url'), pct)}){m.group('post')}"
        ),
        text,
    )

    # 独占一行的 Markdown 图片：包成箭头块
    def standalone_repl(m: re.Match) -> str:
        url = m.group(2)
        if not _is_cdn_url(url):
            return m.group(0)
        return arrow_image_block(url, "", pct)

    text = _MD_IMAGE_ONLY_LINE.sub(standalone_repl, text)

    # 管道表格内的 Markdown 图片：只追加参数
    def table_line_repl(m: re.Match) -> str:
        line = m.group(0)
        return _MD_IMAGE_ANY.sub(
            lambda mm: f"![{mm.group(1)}]({append_thumbnail_param(mm.group(2), pct)})",
            line,
        )

    text = re.sub(r"(?m)^[ \t]*\|.*\|[ \t]*$", table_line_repl, text)

    # HTML <img>：保留标签；像素宽度（如表格内 width="100px"）原样不动
    def img_tag_repl(m: re.Match) -> str:
        tag = m.group(0)
        if _WIDTH_PX.search(tag):
            return tag
        return thumbnail_img_tag(tag, _pct_from_width(tag, pct))

    text = _IMG_TAG.sub(img_tag_repl, text)

    # 图片行与图注行之间补空行
    text = _ARROW_CAPTION_GAP.sub(r"\1\n\n\2", text)
    # 相邻箭头块之间补空行，避免图注与下一张图片挤在同一段落
    text = _ARROW_ADJACENT_GAP.sub(r"\1\n\n", text)
    return text
