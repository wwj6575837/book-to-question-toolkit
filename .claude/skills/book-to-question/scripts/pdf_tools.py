#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆书上题 skill：PDF 检查、目录页渲染、按 titles.txt 拆分、范围校验。

只依赖 PyMuPDF（fitz）。不修改原 PDF，输出到 --outdir。

用法：
    python pdf_tools.py inspect --pdf 原书.pdf --out-json book.json
    python pdf_tools.py render  --pdf 原书.pdf --pages 1-20 --outdir tmp
    python pdf_tools.py verify  --pdf 原书.pdf --titles-txt titles.txt
    python pdf_tools.py split   --pdf 原书.pdf --titles-txt titles.txt --outdir 书目录 --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    print("缺少 PyMuPDF：请用 .venv 里的 python 运行，或 pip install pymupdf", file=sys.stderr)
    raise SystemExit(2)

MAX_PAGES_DEFAULT = 40
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RANGE_SPLIT = re.compile(r"[,，]\s*")
_RANGE = re.compile(r"^(\d+)\s*[-‐‑‒–—―−~～]\s*(\d+)$")
_PAGE_NUM = [
    re.compile(r"^[-‐‑‒–—―−\s]*(\d{1,4})[-‐‑‒–—―−\s]*$"),
    re.compile(r"^第\s*(\d{1,4})\s*页$"),
    re.compile(r"^[Pp]\.?\s*(\d{1,4})$"),
]


def sanitize_filename(name: str) -> str:
    """Windows 安全文件名：去非法字符、压缩空白、去尾部点和空格。"""
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name or "未命名"


def parse_titles(path: Path) -> tuple[list[str], list[tuple[int, int]]]:
    """titles.txt：前面每行一个标题，最后一个非空行是英文逗号分隔的页码范围。"""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    non_empty_idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if not non_empty_idx:
        raise ValueError("titles.txt 为空")
    last_idx = non_empty_idx[-1]
    page_line = lines[last_idx].strip()
    titles = [ln.strip() for ln in lines[:last_idx] if ln.strip()]
    ranges: list[tuple[int, int]] = []
    for part in _RANGE_SPLIT.split(page_line.strip()):
        if not part:
            continue
        m = _RANGE.match(part)
        if not m:
            raise ValueError(f"无法解析页码范围：{part!r}")
        ranges.append((int(m.group(1)), int(m.group(2))))
    if len(titles) != len(ranges):
        raise ValueError(f"标题数({len(titles)})与页码范围数({len(ranges)})不一致")
    return titles, ranges


def validate_plan(
    ranges: list[tuple[int, int]],
    total_pages: int,
    max_pages: int,
    *,
    allow_oversize: bool = False,
) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。重叠/越界是 error，内部空隙和超页是 warning/error。"""
    errors: list[str] = []
    warnings: list[str] = []
    for i, (start, end) in enumerate(ranges, 1):
        if start < 1 or end < 1:
            errors.append(f"第 {i} 段 {start}-{end}：页码必须从 1 开始")
        if start > end:
            errors.append(f"第 {i} 段 {start}-{end}：起始页大于结束页")
        if end > total_pages:
            errors.append(f"第 {i} 段 {start}-{end}：超出 PDF 总页数 {total_pages}")
        size = end - start + 1
        if size > max_pages and not allow_oversize:
            errors.append(f"第 {i} 段 {start}-{end}：{size} 页，超过 {max_pages} 页限制")

    ordered = sorted((s, e, i) for i, (s, e) in enumerate(ranges, 1))
    for prev, cur in zip(ordered, ordered[1:]):
        ps, pe, pi = prev
        cs, ce, ci = cur
        if cs <= pe:
            errors.append(
                f"第 {pi} 段 {ps}-{pe} 与第 {ci} 段 {cs}-{ce} 重叠"
            )
        elif cs > pe + 1:
            warnings.append(
                f"第 {pi} 段 {ps}-{pe} 与第 {ci} 段 {cs}-{ce} 之间有 {cs - pe - 1} 页空隙"
            )

    if ordered:
        first_start = ordered[0][0]
        last_end = max(e for _, e, _ in ordered)
        if first_start > 1:
            warnings.append(f"第 1 页到第 {first_start - 1} 页未纳入任何章节（封面/目录？）")
        if last_end < total_pages:
            warnings.append(f"第 {last_end + 1} 页到第 {total_pages} 页未纳入任何章节（附录/封底？）")
    return errors, warnings


def _page_number_candidates(text: str, total_pages: int) -> list[int]:
    """从页面首尾几行里找可能的书上标码。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    probe = lines[-3:] + lines[:2]
    out: list[int] = []
    for line in probe:
        for rx in _PAGE_NUM:
            m = rx.match(line)
            if not m:
                continue
            n = int(m.group(1))
            # 排除明显的年份、图号、超大数字
            if 1 <= n <= max(total_pages * 2, 999) and not (1900 <= n <= 2100):
                out.append(n)
            break
    return out


def cmd_inspect(args: argparse.Namespace) -> int:
    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"PDF 不存在：{pdf}", file=sys.stderr)
        return 2
    doc = fitz.open(pdf)
    total = doc.page_count
    print(f"PDF：{pdf}")
    print(f"总页数：{total}")

    mapping: list[tuple[int, int]] = []  # (pdf_page, printed)
    no_text_pages = 0
    for idx in range(total):
        text = doc[idx].get_text("text")
        if not text.strip():
            no_text_pages += 1
        cands = _page_number_candidates(text, total)
        if cands:
            # 首尾都可能出现数字；同一页取出现次数最多的候选，平票取最后出现的
            c = Counter(cands)
            mapping.append((idx + 1, c.most_common(1)[0][0]))

    doc.close()

    offsets = Counter(pdf_page - printed for pdf_page, printed in mapping)
    print(f"可识别标码的页数：{len(mapping)}/{total}")
    if no_text_pages:
        print(f"无文本层页数：{no_text_pages}（可能是扫描件，用 render 子命令导出 PNG 人工确认）")
    print(f"候选偏移量分布：{dict(offsets.most_common(5))}")

    report: dict = {
        "pdf": str(pdf),
        "total_pages": total,
        "pages_with_printed_number": len(mapping),
        "pages_without_text": no_text_pages,
        "offset_candidates": [{"offset": k, "count": v} for k, v in offsets.most_common(5)],
    }

    if mapping:
        printed = [p for _, p in mapping]
        dup = sorted(n for n, c in Counter(printed).items() if c > 1)
        expected = list(range(min(printed), max(printed) + 1))
        missing = sorted(set(expected) - set(printed))
        disordered = [
            (a, b) for a, b in zip(mapping, mapping[1:]) if b[1] < a[1]
        ]
        report.update({
            "printed_min": min(printed),
            "printed_max": max(printed),
            "duplicate_printed_numbers": dup,
            "missing_printed_numbers": missing[:200],
            "missing_count": len(missing),
            "out_of_order": disordered[:50],
        })
        if dup:
            print(f"⚠ 重号：{dup[:20]}")
        if missing:
            print(f"⚠ 缺号 {len(missing)} 个：{missing[:30]}")
        if disordered:
            print(f"⚠ 乱序 {len(disordered)} 处：{disordered[:10]}")
        if not (dup or missing or disordered):
            print("标码连续性：正常")

        top_offset, top_count = offsets.most_common(1)[0]
        confidence = top_count / len(mapping)
        report["offset_suggestion"] = top_offset if confidence >= 0.6 and len(mapping) >= 5 else None
        report["offset_confidence"] = round(confidence, 3)
        if report["offset_suggestion"] is None:
            print("偏移量：样本不足或一致率低，未给出建议值，请人工确认。")
        else:
            print(
                f"偏移量建议：{top_offset}（一致率 {confidence:.0%}）"
                f"；书上第 1 页 ≈ PDF 第 {top_offset + 1} 页"
            )
    else:
        print("没有识别到书上标码；建议 render 前 20 页后人工核对偏移量。")

    if args.out_json:
        out = Path(args.out_json)
        data = {}
        if out.exists():
            try:
                data = json.loads(out.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                data = {}
        data.update({k: v for k, v in report.items() if k != "pdf"})
        data["pdf"] = str(pdf)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入：{out}")
    return 0


def _parse_pages(spec: str, total: int) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = _RANGE.match(part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            pages.extend(range(a, b + 1))
        elif part.isdigit():
            pages.append(int(part))
        else:
            raise ValueError(f"无法解析页范围：{part!r}")
    return [p for p in pages if 1 <= p <= total]


def cmd_render(args: argparse.Namespace) -> int:
    pdf = Path(args.pdf)
    doc = fitz.open(pdf)
    pages = _parse_pages(args.pages, doc.page_count)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    scale = args.dpi / 72.0
    for p in pages:
        pix = doc[p - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        out = outdir / f"page-{p:04d}.png"
        pix.save(out)
        print(out)
    doc.close()
    return 0


def _load_plan(args: argparse.Namespace):
    pdf = Path(args.pdf)
    if not pdf.is_file():
        raise ValueError(f"PDF 不存在：{pdf}")
    titles, ranges = parse_titles(Path(args.titles_txt))
    doc = fitz.open(pdf)
    total = doc.page_count
    doc.close()
    errors, warnings = validate_plan(
        ranges, total, args.max_pages, allow_oversize=args.allow_oversize
    )
    return pdf, total, titles, ranges, errors, warnings


def _print_plan(titles, ranges, total, errors, warnings):
    print(f"共 {len(titles)} 段，PDF 总页数 {total}")
    for i, (title, (s, e)) in enumerate(zip(titles, ranges), 1):
        print(f"  {i:>2}. {s:>4}-{e:<4} ({e - s + 1:>2}页)  {title}")
    if warnings:
        print("\n警告：")
        for w in warnings:
            print(f"  ⚠ {w}")
    if errors:
        print("\n错误：")
        for e in errors:
            print(f"  ✗ {e}")


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        _pdf, total, titles, ranges, errors, warnings = _load_plan(args)
    except (ValueError, OSError) as e:
        print(f"校验失败：{e}", file=sys.stderr)
        return 2
    _print_plan(titles, ranges, total, errors, warnings)
    return 1 if errors else 0


def cmd_split(args: argparse.Namespace) -> int:
    try:
        pdf, total, titles, ranges, errors, warnings = _load_plan(args)
    except (ValueError, OSError) as e:
        print(f"校验失败：{e}", file=sys.stderr)
        return 2
    _print_plan(titles, ranges, total, errors, warnings)
    if errors:
        print("\n有错误，未拆分。", file=sys.stderr)
        return 1
    if not args.apply:
        print("\n当前是 dry-run；确认无误后加 --apply 才会落盘。")
        return 0

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    src = fitz.open(pdf)
    made, skipped = [], []
    for title, (s, e) in zip(titles, ranges):
        out = outdir / (sanitize_filename(title) + ".pdf")
        if out.resolve() == pdf.resolve():
            print(f"输出名与原 PDF 相同，拒绝覆盖：{out}", file=sys.stderr)
            src.close()
            return 1
        if out.exists() and not args.force:
            skipped.append(out.name)
            continue
        dest = fitz.open()
        dest.insert_pdf(src, from_page=s - 1, to_page=e - 1)
        dest.save(out)
        dest.close()
        made.append((out.name, e - s + 1))
    src.close()

    print(f"\n已生成 {len(made)} 个 PDF -> {outdir}")
    for name, n in made:
        print(f"  {name} ({n}页)")
    if skipped:
        print(f"跳过已存在 {len(skipped)} 个（加 --force 覆盖）")

    book_path = outdir / "book.json"
    if book_path.exists():
        try:
            data = json.loads(book_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data["chapters"] = [
            {"title": t, "start": s, "end": e}
            for t, (s, e) in zip(titles, ranges)
        ]
        data.setdefault("stages", {})["split"] = "done"
        book_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已更新：{book_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="拆书上题：PDF 检查/渲染/拆分/校验")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="页数、标码连续性、候选偏移量")
    pi.add_argument("--pdf", required=True)
    pi.add_argument("--out-json", help="更新 book.json")
    pi.set_defaults(func=cmd_inspect)

    pr = sub.add_parser("render", help="把指定页导出 PNG，供扫描件人工识别")
    pr.add_argument("--pdf", required=True)
    pr.add_argument("--pages", required=True, help="如 1-20 或 1-5,8,10-12")
    pr.add_argument("--outdir", required=True)
    pr.add_argument("--dpi", type=int, default=150)
    pr.set_defaults(func=cmd_render)

    pv = sub.add_parser("verify", help="校验 titles.txt 与 PDF 页码")
    _add_plan_args(pv)
    pv.set_defaults(func=cmd_verify)

    ps = sub.add_parser("split", help="按 titles.txt 拆分 PDF")
    _add_plan_args(ps)
    ps.add_argument("--outdir", required=True)
    ps.add_argument("--apply", action="store_true", help="默认 dry-run；加此参数才落盘")
    ps.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    ps.add_argument("--force", action="store_true", help="覆盖同名输出 PDF")
    ps.set_defaults(func=cmd_split)
    return p


def _add_plan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pdf", required=True)
    p.add_argument("--titles-txt", required=True)
    p.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT,
                   help=f"单份最大页数（默认 {MAX_PAGES_DEFAULT}）")
    p.add_argument("--allow-oversize", action="store_true",
                   help="允许超过 --max-pages（需用户明确确认）")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
