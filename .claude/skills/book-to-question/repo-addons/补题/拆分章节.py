#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
脚本一：章节拆分
把 补题/详细版/{数字id}{书名}.md 按「## 」二级标题拆成多份 markdown，
写入 补题/拆分后/{数字id}{书名}/，文件名 = 「第N章-描述」（镜像标签格式）。

用法：
    .venv/bin/python 补题/拆分章节.py            # 交互：列出可用 id，输入数字
    .venv/bin/python 补题/拆分章节.py 2291      # 直接按 id 拆分（缺的补写，不改已有）
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent                      # 补题/
DIR_DETAIL = ROOT / "详细版"                                # 知识梳理原始 md
DIR_SPLIT = ROOT / "拆分后"                                 # 拆分结果

HEAD_TITLE = "00_文件头"                                    # 首个标题前的无标题内容归此名
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_NAME_LEN = 100

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 章号：`第X章`、`单元X`/`第X单元`、`项目X` 都认，统一转成「第N章」（X 中文/阿拉伯数字均可）
_CHAPTER_RES = (
    re.compile(r"^第([零〇一二三四五六七八九十百\d]+)章"),      # 第五章 / 第5章
    re.compile(r"^单元([零〇一二三四五六七八九十百\d]+)"),        # 单元1 / 单元一
    re.compile(r"^第([零〇一二三四五六七八九十百\d]+)单元"),      # 第1单元
    re.compile(r"^项目([零〇一二三四五六七八九十百\d]+)"),        # 项目一 / 项目一 电气安全基础知识
)


def _chapter_number(title):
    """返回 (章号整数, 标题中章号片段的结束位置)；认不出返回 None。"""
    for rx in _CHAPTER_RES:
        m = rx.match(title)
        if not m:
            continue
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else _cn_to_int(raw)
        if n is not None:
            return n, m.end()
    return None

# 横线类字符（含 ASCII 连字符）。刻意不含 ー(U+30FC 假名长音) 与 一(U+4E00 汉字)
_DASH_CHARS = "-‐‑‒–—―−﹘﹣－"
_DASH_RUN = re.compile(f"[{_DASH_CHARS}]+")


def normalize_label(s, spaces=True):
    """把横线类字符（以及可选的空白）统一成单个 '-'。

    主配原料——粮食类 / 主配原料—水产品类 / 主配原料 蔬菜类
      → 主配原料-粮食类 / 主配原料-水产品类 / 主配原料-蔬菜类

    spaces=False 时只动横线类字符（用于书名、source 这类不该改空格的地方）。
    """
    s = _DASH_RUN.sub("-", s)
    if spaces:
        s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def read_text_any(path):
    """读文本：优先 UTF-8（含 BOM），失败回退 GBK。

    Windows 上常见这两种编码：带 BOM 的 UTF-8 会让首行变成 "\\ufeff## …"，
    而切分靠 startswith("## ") 判断标题，不处理就会吃掉第一章。
    两种都解不出则报错，不做静默替换——提示词被悄悄改字比报错更糟。
    """
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk")
    except UnicodeDecodeError as e:
        raise ValueError(f"无法识别文件编码（既不是 UTF-8 也不是 GBK）：{path}") from e


def _cn_to_int(s):
    """中文数字转整数（支持 1..999，如 十一/二十三/一百零五）；解析不了返回 None。"""
    total = section = 0
    for ch in s:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            total += (section or 1) * 10
            section = 0
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return None
    return total + section


def chapter_stem(title):
    """章节标题 → 镜像标签格式的文件名。

    第一章 营养学基础       → 第1章-营养学基础
    第5章 主配原料—水产品类 → 第5章-主配原料-水产品类
    单元3 刀工和勺工技术     → 第3章-刀工和勺工技术
    项目一 电气安全基础知识  → 第1章-电气安全基础知识
    中文/阿拉伯数字都认，对已归一化的名字再跑一遍是幂等的；
    非「第X章/单元X/项目X」开头（如 00_文件头）原样返回。只用于文件名与标签，不改正文。
    """
    hit = _chapter_number(title)
    if hit is None:
        return title
    n, end = hit
    desc = normalize_label(title[end:])
    return f"第{n}章-{desc}" if desc else f"第{n}章"


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------
def list_ids():
    """返回 详细版 下所有『数字开头的文件名』的 id 列表（升序）。"""
    if not DIR_DETAIL.exists():
        return []
    out = []
    for p in DIR_DETAIL.iterdir():
        if p.is_file() and p.suffix.lower() == ".md":
            m = re.match(r"^(\d+)", p.stem)
            if m:
                out.append(m.group(1))
    return sorted(set(out))


def find_source_md(id_):
    """按数字 id 前缀定位源文件（`2252.md` 与 `2252烹饪原料知识（第三版）.md` 都认）。

    命中多个时报错——让用户先消除歧义，别猜。不存在返回 None。
    """
    if not DIR_DETAIL.exists():
        return None
    hits = sorted(p for p in DIR_DETAIL.glob(f"{id_}*.md")
                  if re.match(rf"^{re.escape(str(id_))}(?:\D|$)", p.stem))
    if not hits:
        return None
    if len(hits) > 1:
        names = "、".join(p.name for p in hits)
        raise ValueError(f"id {id_} 匹配到多个文件，请改名区分：{names}")
    return hits[0]


def source_stem(id_):
    """源文件去扩展名的完整文件名（= id+书名），用作输出目录名。找不到返回 None。"""
    p = find_source_md(id_)
    return p.stem if p else None


def book_of(id_):
    """从源文件名解析书名：2291烹饪营养与安全（第三版） → 烹饪营养与安全（第三版）。

    文件名只有数字时返回 ""（调用方据此决定是否回退到手输书名）。
    """
    stem = source_stem(id_)
    if not stem:
        return ""
    return re.sub(r"^\d+", "", stem).strip()


def _sanitize(name):
    """清洗为可用的文件基本名：去非法字符、去首尾空白，空则兜底。"""
    name = _ILLEGAL.sub("", name).strip().rstrip(".")
    if not name:
        return "未命名"
    return name[:_MAX_NAME_LEN]


def parse_chapters(text):
    """按「行首 ## 」切成章节，原文保真。

    返回 [(title, body)]：
      * title = ## 后的标题文字（原始、未清洗，做标签用）
      * body  = 从该 ## 行到下一个二级标题前（含标题行，去尾部空行）
    若第一个标题前存在非空内容，归为 (HEAD_TITLE, 那段内容) 并排最前。
    """
    lines = text.splitlines(keepends=True)

    # 定位每个二级标题行（须严格 "## "+ 非#开头），跳过 ``` 代码块
    heads = []
    in_code_block = False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if not in_code_block and ln.startswith("## "):
            heads.append(i)

    chapters = []
    if not heads:
        if "".join(lines).strip():
            chapters.append((HEAD_TITLE, "".join(lines).rstrip("\n")))
        return chapters

    # 标题前的无标题前言（原文整段保真）
    pre_block = "".join(lines[: heads[0]]).strip()
    if pre_block:
        chapters.append((HEAD_TITLE, pre_block))

    for k, i in enumerate(heads):
        j = heads[k + 1] if k + 1 < len(heads) else len(lines)
        title = lines[i][3:].strip()
        body = "".join(lines[i:j]).rstrip("\n")
        chapters.append((title, body))
    return chapters


def ensure_split(id_, overwrite=False):
    """确保 拆分后/{id}/ 下的章节 md 齐备，缺的补写；返回 (chapters, files)。

    overwrite=True  先清空目录内 *.md 再全量重写。
    默认：只补写缺失文件，已存在的保留（幂等）。
    """
    src = find_source_md(id_)
    if src is None:
        return [], []
    chapters = parse_chapters(read_text_any(src))
    if not chapters:
        return [], []

    outdir = DIR_SPLIT / (source_stem(id_) or str(id_))
    outdir.mkdir(parents=True, exist_ok=True)

    if overwrite:
        for old in outdir.glob("*.md"):
            old.unlink()

    # 只与「本轮其他章节」去重；磁盘上已存在的同名文件视为同一章节，直接复用（幂等）
    claimed = set()

    files = []
    for title, body in chapters:
        stem = _sanitize(chapter_stem(title))
        base, n = stem, 1
        while base in claimed:
            n += 1
            base = f"{stem} ({n})"
        claimed.add(base)
        p = outdir / f"{base}.md"
        if not p.exists():
            p.write_text(body + "\n", encoding="utf-8")
        files.append(p)
    return chapters, files


# --------------------------------------------------------------------------
# 交互入口
# --------------------------------------------------------------------------
def _main():
    ids = list_ids()
    if not ids:
        print("【拆分章节】 详细版/ 下没有数字命名的 .md 文件。")
        return

    id_ = None
    if len(sys.argv) > 1:
        id_ = sys.argv[1].strip()
        if id_ not in ids:
            print(f"id「{id_}」不在可用列表内：{ids}")
            return
    else:
        print("可用知识点（输入数字 id）：")
        for i in ids:
            src = find_source_md(i)
            size_kb = src.stat().st_size // 1024 if src else 0
            book = book_of(i) or "（未带书名）"
            print(f"  {i}  {book}  ({size_kb}KB)")
        while True:
            raw = input("请输入数字 id（直接回车退出）：").strip()
            if not raw:
                return
            if raw in ids:
                id_ = raw
                break
            print(f"没有 {raw}，可选：{ids}")

    try:
        src = find_source_md(id_)
    except ValueError as e:
        print(e)
        return
    if src is None:
        print(f"找不到 详细版/{id_}*.md")
        return

    outdir = DIR_SPLIT / (source_stem(id_) or id_)
    existing = list(outdir.glob("*.md")) if outdir.exists() else []

    overwrite = False
    if existing and len(sys.argv) <= 1:
        print(f"\n拆分后/{outdir.name}/ 已有 {len(existing)} 个文件。")
        ans = input("覆盖重拆 [o] / 复用已有（补缺）[r] / 退出 [q]，默认 r：").strip().lower()
        if ans == "q":
            return
        overwrite = ans == "o"

    chapters, files = ensure_split(id_, overwrite=overwrite)
    print(f"\n拆分后/{outdir.name}/ 共 {len(chapters)} 个章节：")
    for p in files:
        chars = len(read_text_any(p))
        print(f"  {p.name}  ({chars} 字符)")
    print("完成。")


if __name__ == "__main__":
    _main()
