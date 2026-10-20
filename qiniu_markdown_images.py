#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 MinerU 输出 Markdown 中的本地图片无损压缩后上传七牛云，并替换为指定 HTML 片段。
凭证仅从环境变量读取：QINIU_ACCESS_KEY、QINIU_SECRET_KEY。

默认仅处理「有意义」插图：``![](...)`` 的**图题或表题**可在插图**上一行或下一行**（中间至多约 2 个空行），
匹配「图/表/Figure/Table」等编号格式（含行首 ▲、附表等）。否则视为装饰图，删除引用且不上传。
内嵌 HTML 中的 `<table>...</table>`：可在表**上/下**各配对一行「表注」文案（须以 表/附表/表注/Table 等开头，**不要**求「几-几」编号；见 pair_table_captions）。
内嵌 HTML 中的 `<img>`（含表格内）：上传后与 Markdown 插图相同，输出 `_html_block`（flex 居中 + img_width_pct + 脚注 div）；**不使用**标签 `alt` 作为脚注（脚注为空，与插图题注解析无关）。
"""

from __future__ import annotations

import html
import io
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

_MD_IMG_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# MinerU 在 Markdown 里常内嵌 HTML 表格，单元格为 <img src="images/xxx.jpg"/>；此类不被 _MD_IMG_PATTERN 匹配
_HTML_IMG_TAG_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_IMG_SRC_ATTR = re.compile(r"\bsrc\s*=\s*(['\"])([^'\"]+)\1", re.IGNORECASE)

# 成对 <table>...</table>（非贪婪；嵌套表时自内向外处理）
_HTML_TABLE_BLOCK = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)

# LaTeX：$\blacktriangle$ / $\triangle$ 后再写「图 x-x-x」（MinerU 导出常见）
_RE_LATEX_TRIANGLE_CAPTION = r"(?:\$\s*\\(?:black)?triangle\s*\$\s*)?"

# 表注行：须以「表/附表/表注/Table」起头；**编号（几-几）整段可选**，便于「表 某某说明」无序号
_TABLE_CAPTION_PREFIX = re.compile(
    r"^[\s　]*"
    + _RE_LATEX_TRIANGLE_CAPTION
    + r"(?:[▲▼▶◀●○◎※＊*\u25b2\u25bc\u25ba\u25c4]+[\s　]*)?"
    r"(?:附表|表注|表|Table|Tab\.?)\s*"
    r"(?:"
    r"[（(][^)）\n]{1,48}[）)]\s*"
    r"|[\d０-９]+(?:\s*[-－–—−．.]\s*[\d０-９]+)*\s*"
    r")?"  # 无「表 1-2-3」仅「表 学生信息」亦匹配
    r"[\s:：、，,．.]*",
    re.IGNORECASE | re.UNICODE,
)


# 题注行：去掉「图/表/Figure + 编号」前缀；允许行首教材常用标记（▲▼等），否则「▲图1-2-3」无法匹配会被当成无题注并删除插图
_FIG_LABEL_STRIP = re.compile(
    r"^[\s　]*"
    + _RE_LATEX_TRIANGLE_CAPTION
    + r"(?:[▲▼▶◀●○◎※＊*\u25b2\u25bc\u25ba\u25c4]+[\s　]*)?"
    r"(?:"
    r"附表|附图|图|表|插图|(?:Figure|Fig\.?|Table|Tab\.?)"
    r")\s*"
    r"(?:"
    r"[（(][^)）\n]{1,48}[）)]\s*"  # （1-1-3）或 (1-1-3)
    r"|[\d０-９]+(?:\s*[-－–—−．.]\s*[\d０-９]+)*\s*"  # 1-1-3、1．1．3、全角数字
    r")"
    r"[\s:：、，,．.]*",
    re.IGNORECASE | re.UNICODE,
)


def _strip_figure_label(line: str) -> Optional[str]:
    """
    若本行为图表题注行，返回去掉编号后的说明；否则返回 None（整行保留，不参与脚注）。
    说明可为空字符串（仅编号无正文时）。
    """
    s = line.strip()
    if not s:
        return None
    m = _FIG_LABEL_STRIP.match(s)
    if not m:
        return None
    return s[m.end() :].strip()


def _strip_table_caption_line(line: str) -> Optional[str]:
    """
    表注专用：行须以 附表/表注/表/Table 等开头；编号可选。
    返回展示用文案（去掉前缀后；若仅「表」无正文则返回整行）。
    """
    s = line.strip()
    if not s:
        return None
    m = _TABLE_CAPTION_PREFIX.match(s)
    if not m:
        return None
    rest = s[m.end() :].strip()
    return rest if rest else s


def _caption_after_image(after: str) -> Optional[Tuple[str, int]]:
    """
    判定紧跟 ![](...) **下方**的第一条非空行是否为图题/表题（与上一行对称逻辑见 _caption_before_image）。
    """
    if not after:
        return None
    gap = re.match(r"^[ \t]*(?:\n[ \t]*){0,2}", after)
    i = gap.end() if gap else 0
    line_end = after.find("\n", i)
    raw_line = after[i:] if line_end == -1 else after[i:line_end]
    stripped = raw_line.strip()
    if not stripped:
        return None
    desc = _strip_figure_label(stripped)
    if desc is None:
        return None
    if line_end == -1:
        skip = len(after)
    else:
        skip = line_end + 1
    return (desc, skip)


def _caption_before_image(before: str, max_blank_gap: int = 2) -> Optional[Tuple[str, int]]:
    """
    判定 ``![](...)`` **上方**、与其相隔至多 max_blank_gap 个空行内的第一条非空行是否为图题/表题。
    返回 (写入 div 的说明, 需从 m.start() 之前删除的字符数，含该题注行与其后到插图前的空白)。
    """
    if not before:
        return None
    parts = before.splitlines(keepends=True)
    if not parts:
        return None
    total_len = len(before)
    idx = len(parts) - 1
    blank = 0
    while idx >= 0:
        body = parts[idx].strip()
        if not body:
            blank += 1
            if blank > max_blank_gap:
                return None
            idx -= 1
            continue
        desc = _strip_figure_label(body)
        if desc is None:
            return None
        line_start = sum(len(parts[j]) for j in range(idx))
        skip_before = total_len - line_start
        return (desc, skip_before)
    return None


def _resolve_md_image_caption(before: str, after: str) -> Optional[Tuple[str, int, int]]:
    """
    合并「题注在图下」「题注在图上」两种 MinerU/教材版式。
    返回 (说明, skip_before, skip_after)；优先采用图下方的题注（与常见导出一致），否则采用上方。
    """
    ca = _caption_after_image(after)
    if ca is not None:
        cap, skip_after = ca
        return (cap, 0, skip_after)
    cb = _caption_before_image(before)
    if cb is not None:
        cap, skip_before = cb
        return (cap, skip_before, 0)
    return None


def _table_caption_after(after: str) -> Optional[Tuple[str, int]]:
    """`<table>` 下方紧邻（至多 2 空行）第一行是否为表注。"""
    if not after:
        return None
    gap = re.match(r"^[ \t]*(?:\n[ \t]*){0,2}", after)
    i = gap.end() if gap else 0
    line_end = after.find("\n", i)
    raw_line = after[i:] if line_end == -1 else after[i:line_end]
    stripped = raw_line.strip()
    if not stripped:
        return None
    cap = _strip_table_caption_line(stripped)
    if cap is None:
        return None
    if line_end == -1:
        skip = len(after)
    else:
        skip = line_end + 1
    return (cap, skip)


def _table_caption_before(before: str, max_blank_gap: int = 2) -> Optional[Tuple[str, int]]:
    """`<table>` 上方紧邻（至多 max_blank_gap 空行）第一行是否为表注。"""
    if not before:
        return None
    parts = before.splitlines(keepends=True)
    if not parts:
        return None
    total_len = len(before)
    idx = len(parts) - 1
    blank = 0
    while idx >= 0:
        body = parts[idx].strip()
        if not body:
            blank += 1
            if blank > max_blank_gap:
                return None
            idx -= 1
            continue
        cap = _strip_table_caption_line(body)
        if cap is None:
            return None
        line_start = sum(len(parts[j]) for j in range(idx))
        skip_before = total_len - line_start
        return (cap, skip_before)
    return None


def _resolve_table_captions(
    before: str, after: str
) -> Tuple[Optional[str], Optional[str], int, int]:
    """(表上文案, 表下文案, skip_before, skip_after)；可同时有上下表注。"""
    cb = _table_caption_before(before)
    ca = _table_caption_after(after)
    cap_above = cb[0] if cb else None
    skip_before = cb[1] if cb else 0
    cap_below = ca[0] if ca else None
    skip_after = ca[1] if ca else 0
    if cap_above is None and cap_below is None:
        return (None, None, 0, 0)
    return (cap_above, cap_below, skip_before, skip_after)


def _wrap_html_table_with_captions(
    table_html: str,
    cap_above: Optional[str],
    cap_below: Optional[str],
) -> str:
    parts = ['<div align="center" style="margin:14px 0;">']
    if cap_above:
        parts.append(
            '<div style="color:#333;font-size:14px;margin-bottom:8px;">'
            f"{html.escape(cap_above)}</div>"
        )
    parts.append(table_html.strip())
    if cap_below:
        parts.append(
            '<div style="color:#333;font-size:14px;margin-top:8px;">'
            f"{html.escape(cap_below)}</div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


def _process_html_table_captions(text: str, enabled: bool) -> str:
    """给内嵌 HTML 表配对上下表注（删除原独立题注行以免重复）。"""
    if not enabled:
        return text
    for m in reversed(list(_HTML_TABLE_BLOCK.finditer(text))):
        before = text[: m.start()]
        after = text[m.end() :]
        cap_a, cap_b, sb, sa = _resolve_table_captions(before, after)
        if cap_a is None and cap_b is None:
            continue
        wrapped = _wrap_html_table_with_captions(m.group(0), cap_a, cap_b)
        text = text[: m.start() - sb] + wrapped + text[m.end() + sa :]
    return text


# 七牛 SDK 只尝试导入一次，避免每张图都打印未安装提示
_qiniu_import_ok: bool = False
_qiniu_import_checked: bool = False
_qiniu_auth_class = None
_qiniu_put_data = None
_qiniu_missing_sdk_printed: bool = False


def _ensure_qiniu_imports() -> bool:
    """成功则缓存 Auth/put_data；失败只打印一次提示。"""
    global _qiniu_import_ok, _qiniu_import_checked, _qiniu_auth_class, _qiniu_put_data
    global _qiniu_missing_sdk_printed
    if _qiniu_import_checked:
        return _qiniu_import_ok
    _qiniu_import_checked = True
    try:
        from qiniu import Auth, put_data  # type: ignore

        _qiniu_auth_class = Auth
        _qiniu_put_data = put_data
        _qiniu_import_ok = True
        return True
    except ImportError:
        _qiniu_import_ok = False
        if not _qiniu_missing_sdk_printed:
            print("   ✗ 无法导入 qiniu SDK（当前解释器里未安装或不可用）")
            print(f"      实际使用的 Python: {sys.executable}")
            print(
                "      解决：在该解释器中安装 → "
                f'"{sys.executable}" -m pip install qiniu'
            )
            print(
                "      若在终端已安装但仍报错，多半是 IDE/快捷方式用了别的 Python；"
                "请在 PyCharm/Cursor 里把项目解释器设为 MinerU/.venv/bin/python"
            )
            _qiniu_missing_sdk_printed = True
        return False


def qiniu_credentials_available() -> bool:
    return bool(os.environ.get("QINIU_ACCESS_KEY") and os.environ.get("QINIU_SECRET_KEY"))


_qiniu_dotenv_loaded: bool = False


def _ensure_project_dotenv_for_qiniu() -> None:
    """与 run_converter / agent_runner 一致：从项目根 `.env` 注入环境变量后再读七牛凭证。"""
    global _qiniu_dotenv_loaded
    if _qiniu_dotenv_loaded:
        return
    _qiniu_dotenv_loaded = True
    try:
        from project_dotenv import load_project_dotenv

        load_project_dotenv()
        return
    except ImportError:
        pass
    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.is_file():
            load_dotenv(env_path, override=False)
    except ImportError:
        pass


def _normalize_qiniu_config(cfg: Optional[dict]) -> dict:
    base = {
        "enabled": False,
        "bucket": os.environ.get("QINIU_BUCKET", ""),
        "domain": os.environ.get("QINIU_DOMAIN", "https://cdn.maiziedu.cn"),
        "key_prefix": "images/tp",
        "img_width_pct": 30,
        "require_figure_caption": True,
        # 内嵌 <table>：把紧挨表上/下的「表注」行并入排版（须以 表/附表/表注/Table 开头，编号可选）
        "pair_table_captions": True,
    }
    if isinstance(cfg, dict):
        for k in (
            "enabled",
            "bucket",
            "domain",
            "key_prefix",
            "img_width_pct",
            "require_figure_caption",
            "pair_table_captions",
        ):
            if k in cfg and cfg[k] is not None:
                base[k] = cfg[k]
    # 环境变量覆盖域名、存储桶（便于不写 config）
    if os.environ.get("QINIU_BUCKET"):
        base["bucket"] = os.environ["QINIU_BUCKET"].strip()
    if os.environ.get("QINIU_DOMAIN"):
        base["domain"] = os.environ["QINIU_DOMAIN"].strip().rstrip("/")
    return base


def compress_image_lossless(raw: bytes, suffix: str) -> Tuple[bytes, str]:
    """
    无损体积优化：PNG/WebP 无损；JPEG 使用 Pillow optimize（哈夫曼表优化，像素不变）。
    返回 (压缩后字节, 上传用扩展名，不含点)。
    """
    from PIL import Image

    suf = suffix.lower().lstrip(".")
    buf = io.BytesIO(raw)
    im = Image.open(buf)
    im.load()

    out = io.BytesIO()

    if suf in ("jpg", "jpeg"):
        rgb = im.convert("RGB") if im.mode in ("RGBA", "P", "LA") else im
        try:
            # quality='keep' 保留量化表，optimize 做哈夫曼优化，像素级无损（需 Pillow 较新版本）
            rgb.save(
                out,
                format="JPEG",
                optimize=True,
                quality="keep",
            )
        except Exception:
            # 无法无损优化时保留原始 JPEG 字节，避免默认 quality 导致有损重编码
            return raw, "jpg"
        return out.getvalue(), "jpg"

    if suf == "gif":
        try:
            im.save(out, format="GIF", save_all=True, optimize=True)
        except Exception:
            out.seek(0)
            out.truncate(0)
            im.save(out, format="GIF", optimize=True)
        return out.getvalue(), "gif"

    if suf == "bmp":
        im.convert("RGB").save(out, format="PNG", optimize=True, compress_level=9)
        return out.getvalue(), "png"

    if suf == "png":
        im.save(out, format="PNG", optimize=True, compress_level=9)
        return out.getvalue(), "png"

    if suf == "webp":
        im.save(out, format="WEBP", lossless=True, method=6)
        return out.getvalue(), "webp"

    # 其余格式统一转 PNG 无损
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA") if im.mode in ("P", "LA") else im.convert("RGB")
    im.save(out, format="PNG", optimize=True, compress_level=9)
    return out.getvalue(), "png"


def _build_public_url(domain: str, key: str) -> str:
    return f"{domain.rstrip('/')}/{key.lstrip('/')}"


def _html_block(src: str, caption: str, width_pct: int) -> str:
    cap = html.escape(caption or "")
    return (
        '<div align="center" style="display: flex; flex-direction: column; '
        "justify-content: center; align-items: center; gap: 10px;\">\n"
        f'  <img src="{html.escape(src)}" width="{width_pct}%" />\n'
        f'  <div style="color: #666; font-size: 14px;">{cap}</div>\n'
        "</div>"
    )


def _rewrite_single_img_tag_src(
    tag: str,
    base_dir: Path,
    prefix: str,
    domain: str,
    bucket: str,
    upload_cache: Dict[str, str],
    width_pct: int,
) -> str:
    """将单个 <img ...> 上传七牛并替换为与 Markdown 插图一致的 `_html_block`（MinerU 同款样式）。"""
    m = _HTML_IMG_SRC_ATTR.search(tag)
    if not m:
        return tag
    rel = m.group(2).strip()
    if rel.startswith(("http://", "https://", "data:")):
        return tag
    low = rel.split("?")[0].lower()
    if not any(low.endswith(x) for x in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        return tag

    full_path = (base_dir / rel).resolve()
    cache_key = str(full_path)

    if cache_key in upload_cache:
        new_url = upload_cache[cache_key]
    else:
        if not full_path.is_file():
            print(f"   ⚠ HTML <img> 找不到文件，保留原 src: {full_path}")
            return tag
        try:
            raw = full_path.read_bytes()
        except Exception as e:
            print(f"   ✗ 读取图片失败 {full_path}: {e}")
            return tag
        suffix = full_path.suffix or ".jpg"
        try:
            blob, ext = compress_image_lossless(raw, suffix)
        except Exception as e:
            print(f"   ✗ 压缩图片失败 {full_path}: {e}")
            return tag
        now = datetime.now()
        date_part = now.strftime("%Y/%m/%d")
        object_name = f"{uuid.uuid4().hex}.{ext}"
        object_key = f"{str(prefix).strip().strip('/')}/{date_part}/{object_name}"
        ok, _ = _upload_bytes_qiniu(blob, object_key, bucket)
        if not ok:
            return tag
        new_url = _build_public_url(domain, object_key)
        upload_cache[cache_key] = new_url

    return _html_block(new_url, "", width_pct)


def _process_html_img_tags_in_markdown(
    text: str,
    base_dir: Path,
    prefix: str,
    domain: str,
    bucket: str,
    upload_cache: Dict[str, str],
    width_pct: int,
) -> str:
    """从内嵌 HTML（如 <table> 内）收集 <img>，上传并替换为 `_html_block`（脚注为空，不使用 alt）。"""
    matches = list(_HTML_IMG_TAG_PATTERN.finditer(text))
    if not matches:
        return text
    out = []
    last = 0
    for m in matches:
        out.append(text[last : m.start()])
        out.append(
            _rewrite_single_img_tag_src(
                m.group(0),
                base_dir,
                prefix,
                domain,
                bucket,
                upload_cache,
                width_pct,
            )
        )
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _upload_bytes_qiniu(
    data: bytes,
    key: str,
    bucket: str,
) -> Tuple[bool, str]:
    if not _ensure_qiniu_imports():
        return False, ""

    ak = os.environ.get("QINIU_ACCESS_KEY", "").strip()
    sk = os.environ.get("QINIU_SECRET_KEY", "").strip()
    if not ak or not sk:
        return False, ""

    auth = _qiniu_auth_class(ak, sk)
    token = auth.upload_token(bucket)
    ret, info = _qiniu_put_data(token, key, data)
    code = getattr(info, "status_code", None)
    if code == 200 and ret is not None:
        if isinstance(ret, dict) and ret.get("error"):
            print(f"   ✗ 七牛上传失败: key={key} ret={ret}")
            return False, ""
        return True, key
    print(f"   ✗ 七牛上传失败: key={key} status={code} info={info}")
    return False, ""


def process_extract_directory(extract_dir: Path, qiniu_cfg: Optional[dict]) -> None:
    """
    处理单个解压目录：重写所有 .md 中的图片引用并上传七牛；本地图片文件保留不动。
    """
    _ensure_project_dotenv_for_qiniu()
    cfg = _normalize_qiniu_config(qiniu_cfg)
    if not cfg.get("enabled"):
        return
    if not qiniu_credentials_available():
        print("   ⚠ 已启用 qiniu.enabled，但未设置 QINIU_ACCESS_KEY / QINIU_SECRET_KEY，跳过 CDN 上传")
        return
    bucket = (cfg.get("bucket") or "").strip()
    if not bucket:
        print("   ⚠ 七牛 bucket 未配置（环境变量 QINIU_BUCKET 或 config.qiniu.bucket），跳过")
        return

    if not _ensure_qiniu_imports():
        return

    md_files = sorted(extract_dir.rglob("*.md"))
    if not md_files:
        return

    prefix = str(cfg.get("key_prefix") or "images/tp").strip().strip("/")
    domain = str(cfg.get("domain") or "https://cdn.maiziedu.cn").rstrip("/")
    width_pct = int(cfg.get("img_width_pct", 30))
    require_figure_caption = bool(cfg.get("require_figure_caption", True))
    pair_table_captions = bool(cfg.get("pair_table_captions", True))

    upload_cache: Dict[str, str] = {}
    decorative_removed = 0

    for md_path in md_files:
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"   ✗ 读取 Markdown 失败 {md_path}: {e}")
            continue

        base_dir = md_path.parent
        original_text = text

        matches = list(_MD_IMG_PATTERN.finditer(text))
        for m in reversed(matches):
            rel = m.group(2).strip()
            if rel.startswith(("http://", "https://", "data:")):
                continue
            low = rel.split("?")[0].lower()
            if not any(low.endswith(x) for x in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
                continue

            before = text[: m.start()]
            after = text[m.end() :]
            cap_res = _resolve_md_image_caption(before, after)
            if cap_res is None:
                if require_figure_caption:
                    # 无正式题注行 → 装饰/版式图：去掉引用，不上传
                    text = text[: m.start()] + text[m.end() :]
                    decorative_removed += 1
                    continue
                cap, skip_before, skip_after = "", 0, 0
            else:
                cap, skip_before, skip_after = cap_res

            full_path = (base_dir / rel).resolve()
            cache_key = str(full_path)
            if cache_key in upload_cache:
                url = upload_cache[cache_key]
                html_frag = _html_block(url, cap, width_pct)
                text = text[: m.start() - skip_before] + html_frag + text[m.end() + skip_after :]
                continue

            if not full_path.is_file():
                print(f"   ⚠ 找不到图片文件，保留原引用: {full_path}")
                continue

            try:
                raw = full_path.read_bytes()
            except Exception as e:
                print(f"   ✗ 读取图片失败 {full_path}: {e}")
                continue

            suffix = full_path.suffix or ".jpg"
            try:
                blob, ext = compress_image_lossless(raw, suffix)
            except Exception as e:
                print(f"   ✗ 压缩图片失败 {full_path}: {e}")
                continue

            now = datetime.now()
            date_part = now.strftime("%Y/%m/%d")
            object_name = f"{uuid.uuid4().hex}.{ext}"
            key = f"{prefix}/{date_part}/{object_name}"

            ok, _ = _upload_bytes_qiniu(blob, key, bucket)
            if not ok:
                continue

            url = _build_public_url(domain, key)
            upload_cache[cache_key] = url
            html_frag = _html_block(url, cap, width_pct)
            text = text[: m.start() - skip_before] + html_frag + text[m.end() + skip_after :]

        # HTML 表格等内嵌的 <img src="images/...">：上传后与 Markdown 插图同为 `_html_block`，脚注为空
        text2 = _process_html_img_tags_in_markdown(
            text,
            base_dir,
            prefix,
            domain,
            bucket,
            upload_cache,
            width_pct,
        )
        text = text2
        text = _process_html_table_captions(text, pair_table_captions)

        if text != original_text:
            try:
                md_path.write_text(text, encoding="utf-8")
            except Exception as e:
                print(f"   ✗ 写入 Markdown 失败 {md_path}: {e}")
                continue

    if upload_cache:
        print(f"   ✓ 七牛 CDN：已上传 {len(upload_cache)} 张图片并更新 Markdown")
    if decorative_removed:
        print(
            f"   ✓ 已移除 {decorative_removed} 处装饰性图片引用"
            "（紧跟处无「图/表/Figure」题注，未上传七牛）"
        )
