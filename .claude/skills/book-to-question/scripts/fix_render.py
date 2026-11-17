#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动修正可确定修复的公式渲染问题（只改数学环境，不动正文语义）。

用法：
    python fix_render.py --file "exports/.../2552-1 ....md"          # dry-run
    python fix_render.py --file "..." --apply
    python fix_render.py --dir "exports/.../书籍库" --apply
    python fix_render.py --questions-dir "补题/题目/2552xxx" --apply

修正范围：
- Unicode 根号 √/∛/∜ → \\sqrt{...}
- 数学环境里的中文/全角标点 → \\text{...}
- `${...}$` 的外层花括号 → `$\\{...\\}$`
- 数学环境里的 △、℃、≤、×、π 等 Unicode 符号 → LaTeX 命令
- `İ` → `\\dot{I}`，`%` → `\\%`，删除 `\\tag{...}`
- 数学与代码之外成对的单个 * → \\*

解析层面的错误（缺参数、未定义命令）不猜，留在复检报告里。
默认 dry-run；--apply 写盘，原文件备份为 <文件名>.bak-<时间戳>。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from mdscan import read_text_any

REPO_ROOT = Path(__file__).resolve().parents[4]
FENCE = re.compile(r"(```|~~~).*?\1", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+")
TEXT_OPEN = re.compile(r"\\text\{")
TAG = re.compile(r"\\tag\*?\{[^}]*\}")
STAR_RUN = re.compile(r"\*+")

SUP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
       "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁺": "+", "⁻": "-", "ⁿ": "n"}
SUB = {"₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5",
       "₆": "6", "₇": "7", "₈": "8", "₉": "9", "₊": "+", "₋": "-", "ₑ": "e"}
SYMBOLS = {
    "△": "\\triangle ", "▲": "\\triangle ", "□": "\\square ",
    "⊥": "\\perp ", "∥": "\\parallel ", "∠": "\\angle ",
    "∞": "\\infty ", "≤": "\\le ", "≥": "\\ge ", "≠": "\\ne ",
    "≈": "\\approx ", "×": "\\times ", "÷": "\\div ", "±": "\\pm ",
    "∓": "\\mp ", "·": "\\cdot ", "→": "\\to ", "⇒": "\\Rightarrow ",
    "⇔": "\\Leftrightarrow ", "∈": "\\in ", "∉": "\\notin ",
    "⊆": "\\subseteq ", "⊂": "\\subset ", "⊃": "\\supset ",
    "⊇": "\\supseteq ", "∅": "\\varnothing ", "∀": "\\forall ",
    "∃": "\\exists ", "∧": "\\wedge ", "∨": "\\vee ", "¬": "\\neg ",
    "∴": "\\therefore ", "∵": "\\because ",
    "⊙": "\\odot ", "∖": "\\setminus ", "≌": "\\cong ",
    "∪": "\\cup ", "∩": "\\cap ", "∑": "\\sum ", "∏": "\\prod ",
    "∫": "\\int ",
    "–": "-", "—": "-",
    "℃": "^\\circ\\mathrm{C}", "℉": "^\\circ\\mathrm{F}",
    "π": "\\pi ", "α": "\\alpha ", "β": "\\beta ", "γ": "\\gamma ",
    "Δ": "\\Delta ", "θ": "\\theta ", "λ": "\\lambda ", "μ": "\\mu ",
    "σ": "\\sigma ", "φ": "\\varphi ", "ε": "\\varepsilon ",
    "ω": "\\omega ", "Ω": "\\Omega ",
}
BRACE_COMMANDS = re.compile(
    r"\\(?:sqrt|text|mathrm|mathbf|mathit|frac|dfrac|tfrac|overline|underline|hat|bar|vec|begin|operatorname)\s*$"
)
SET_SEP = re.compile(r"[|,，;；]|\\mid|∈")
GAP_BAD = set("*_[]`#<>~")
GAP_MAX = 40
MERGE_SPANS_MAX = 3


def math_ranges(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\\":
            matched = False
            for op, cl, kind in (("\\(", "\\)", "paren"), ("\\[", "\\]", "bracket")):
                if text.startswith(op, i):
                    j = text.find(cl, i + 2)
                    if j < 0:
                        out.append((i, n, kind))
                        i = n
                    else:
                        out.append((i, j + 2, kind))
                        i = j + 2
                    matched = True
                    break
            if matched:
                continue
            i += 2
            continue
        if text.startswith("$$", i):
            j = text.find("$$", i + 2)
            if j < 0:
                out.append((i, n, "block"))
                break
            out.append((i, j + 2, "block"))
            i = j + 2
            continue
        if text[i] == "$":
            j = text.find("$", i + 1)
            nl = text.find("\n", i + 1)
            if j >= 0 and (nl < 0 or j < nl):
                out.append((i, j + 1, "inline"))
                i = j + 1
                continue
        i += 1
    return out


def text_ranges(expr: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for m in TEXT_OPEN.finditer(expr):
        depth = 0
        for j in range(m.end() - 1, len(expr)):
            if expr[j] == "{":
                depth += 1
            elif expr[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append((m.start(), j + 1))
                    break
    return out


def radicand(text: str, i: int):
    n = len(text)
    j = i + 1
    while j < n and text[j] in " \t":
        j += 1
    if j >= n:
        return None
    c = text[j]
    pairs = {"(": ")", "（": "）", "[": "]", "{": "}"}
    if c in pairs:
        close, depth = pairs[c], 0
        for k in range(j, n):
            if text[k] == c:
                depth += 1
            elif text[k] == close:
                depth -= 1
                if depth == 0:
                    return j + 1, k, k + 1
        return None
    if c.isdigit():
        m = re.match(r"\d+(?:\.\d+)?", text[j:])
        return j, j + m.end(), j + m.end()
    if c.isascii() and c.isalpha():
        end = j + 1
        m = re.match(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ₀₁₂₃₄₅₆₇₈₉₊₋ₑ]*", text[end:])
        return j, end + m.end(), end + m.end()
    return None


def normalize_inner(inner: str) -> str:
    s = re.sub(r"[₀₁₂₃₄₅₆₇₈₉₊₋ₑ]+",
               lambda m: "_{" + "".join(SUB[c] for c in m.group(0)) + "}", inner)
    s = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ]+",
               lambda m: "^{" + "".join(SUP[c] for c in m.group(0)) + "}", s)
    s = re.sub(r"[一-鿿]+", lambda m: "\\text{" + m.group(0) + "}", s)
    return s


def _escape_outer_braces(core: str) -> str:
    if len(core) < 2 or core[0] != "{" or core[-1] != "}":
        return core
    depth = 0
    for i, ch in enumerate(core):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                if i != len(core) - 1:
                    return core
                inner = core[1:-1]
                # 以命令开头的外层花括号只是分组，直接去掉；不要变成可见花括号
                if inner.lstrip().startswith("\\"):
                    return inner
                # 明确像集合/数列时才转义，其余不动，避免误改分组
                if re.search(r"[|,，;；∈]|^[A-Za-z]_[A-Za-z0-9]+$", inner):
                    return "\\{" + inner + "\\}"
                return core
    return core


def brace_delta(expr: str) -> int:
    s = expr.replace("\\{", "").replace("\\}", "")
    return s.count("{") - s.count("}")


def escape_set_braces(expr: str) -> str:
    """数学里像集合的裸 {..} 转成 \\{..\\}；命令参数、上下标、\\text 不动。"""
    protected = text_ranges(expr)
    out, last, i = [], 0, 0
    while i < len(expr):
        if expr[i] == "{" and not (i and expr[i - 1] == "\\"):
            depth, close = 0, None
            for j in range(i, len(expr)):
                if expr[j] == "{":
                    depth += 1
                elif expr[j] == "}":
                    depth -= 1
                    if depth == 0:
                        close = j
                        break
            if close is not None:
                inner = expr[i + 1 : close]
                before = expr[:i].rstrip()
                if (not BRACE_COMMANDS.search(before)
                        and not before.endswith(("^", "_", "}", "]"))
                        and SET_SEP.search(inner)
                        and not any(a <= i < b for a, b in protected)):
                    out.append(expr[last:i])
                    out.append("\\{" + escape_set_braces(inner) + "\\}")
                    last = close + 1
                    i = close + 1
                    continue
        i += 1
    out.append(expr[last:])
    return "".join(out)


def _replace_radicals(expr: str) -> str:
    protected = text_ranges(expr)
    changes = []
    for m in re.finditer(r"[√∛∜]", expr):
        if any(a <= m.start() < b for a, b in protected):
            continue
        r = radicand(expr, m.start())
        ch = m.group(0)
        if not r:
            if ch == "√":
                changes.append((m.start(), m.start() + 1, "\\surd "))
            continue
        a, b, end = r
        sym = {"√": "\\sqrt", "∛": "\\sqrt[3]", "∜": "\\sqrt[4]"}[ch]
        changes.append((m.start(), end, f"{sym}{{{normalize_inner(expr[a:b])}}}"))
    for start, end, rep in reversed(changes):
        expr = expr[:start] + rep + expr[end:]
    return expr


def _fix_segment(seg: str) -> str:
    seg = seg.replace("\\AA", "\\text{Å}").replace("\\aa", "\\text{å}")
    seg = seg.replace("\\circC", "\\circ\\mathrm{C}").replace("\\circF", "\\circ\\mathrm{F}")
    for key, val in SYMBOLS.items():
        if key in seg:
            seg = seg.replace(key, val)
    seg = seg.replace("İ", "\\dot{I}")
    seg = re.sub(r"(?<!\\)%", r"\\%", seg)
    out, last = [], 0
    for m in CJK.finditer(seg):
        out.append(seg[last:m.start()])
        out.append("\\text{" + m.group(0) + "}")
        last = m.end()
    out.append(seg[last:])
    return "".join(out)


def split_text_temps(expr: str) -> str:
    """\\text{25℃时} → \\text{25}^\\circ\\mathrm{C}\\text{时}，KaTeX 不认文本里的 ℃。"""
    out, last = [], 0
    for a, b in text_ranges(expr):
        inner = expr[a + len("\\text{"):b - 1]
        if "℃" not in inner and "℉" not in inner:
            continue
        parts = re.split(r"([℃℉])", inner)
        repl = []
        for part in parts:
            if part == "℃":
                repl.append("^\\circ\\mathrm{C}")
            elif part == "℉":
                repl.append("^\\circ\\mathrm{F}")
            elif part:
                repl.append("\\text{" + part + "}")
        out.append(expr[last:a])
        out.append("".join(repl))
        last = b
    out.append(expr[last:])
    return "".join(out)


def fix_math(expr: str) -> str:
    expr = expr.replace("\\boldsymbol{√}", "\\checkmark")
    expr = TAG.sub("", expr)
    expr = _replace_radicals(expr)
    lead = expr[: len(expr) - len(expr.lstrip())]
    trail = expr[len(expr.rstrip()):]
    core = _escape_outer_braces(expr.strip())
    expr = escape_set_braces(lead + core + trail)
    protected = text_ranges(expr)
    out, last = [], 0
    for a, b in protected:
        out.append(_fix_segment(expr[last:a]))
        out.append(expr[a:b])
        last = b
    out.append(_fix_segment(expr[last:]))
    return split_text_temps("".join(out))


def _mask_code(text: str) -> str:
    chars = list(text)
    for pattern in (FENCE, INLINE_CODE):
        for m in pattern.finditer(text):
            for i in range(m.start(), m.end()):
                chars[i] = " "
    return "".join(chars)


def merge_split_math(text: str) -> str:
    """把被错误 $ 拆开的集合式合并回一个公式，如 $A={x$ | $x^2>0}$。"""
    ranges = math_ranges(_mask_code(text))
    out, last, i = [], 0, 0
    while i < len(ranges):
        start, end, kind = ranges[i]
        pad = 1 if kind == "inline" else 2
        body = text[start + pad:end - pad]
        depth = brace_delta(body)
        if depth <= 0:
            out.append(text[last:end])
            last = end
            i += 1
            continue
        parts = [body]
        gaps = []
        spans_merged = 1
        pos, close_pos, j = end, None, i + 1
        while depth > 0:
            nxt = ranges[j] if j < len(ranges) else None
            same_line = bool(
                nxt and nxt[0] >= pos and nxt[2] == "inline"
                and "\n" not in text[pos:nxt[0]]
            )
            if same_line:
                limit = nxt[0]
            else:
                nl = text.find("\n", pos)
                limit = len(text) if nl < 0 else nl
            d, k, found = depth, pos, False
            while k < limit:
                ch = text[k]
                if ch == "{" and not (k and text[k - 1] == "\\"):
                    d += 1
                elif ch == "}" and not (k and text[k - 1] == "\\"):
                    d -= 1
                    if d == 0:
                        parts.append(text[pos:k + 1])
                        gaps.append(text[pos:k + 1])
                        close_pos = k + 1
                        found = True
                        break
                k += 1
            if found or not same_line:
                break
            nstart, nend = nxt[0], nxt[1]
            parts.append(text[pos:nstart])
            gaps.append(text[pos:nstart])
            nbody = text[nstart + 1:nend - 1]
            parts.append(nbody)
            depth += brace_delta(nbody)
            spans_merged += 1
            pos = nend
            j += 1
            if depth <= 0:
                close_pos = pos
                break
        if close_pos is not None:
            merged = "".join(parts)
            gap_text = "".join(gaps)
            if (spans_merged > MERGE_SPANS_MAX
                    or len(gap_text) > GAP_MAX
                    or any(ch in GAP_BAD for ch in gap_text)
                    or not SET_SEP.search(merged)):
                close_pos = None
        if close_pos is None:
            out.append(text[last:end])
            last = end
            i += 1
            continue
        out.append(text[last:start])
        out.append(text[start:start + pad] + "".join(parts) + text[end - pad:end])
        last = close_pos
        while i < len(ranges) and ranges[i][1] <= close_pos:
            i += 1
    out.append(text[last:])
    return "".join(out)


def normalize_delimiters(text: str) -> tuple[str, int]:
    """\\(...\\) 统一成 $...$，\\[...\\] 统一成独立 $$ 块。"""
    masked = _mask_code(text)
    out, last, i, n = [], 0, 0, 0
    while i < len(masked):
        if masked.startswith("\\[", i):
            j = masked.find("\\]", i + 2)
            if j >= 0:
                body = text[i + 2:j].strip()
                out.append(text[last:i])
                out.append("\n$$\n" + body + "\n$$\n")
                last, i, n = j + 2, j + 2, n + 1
                continue
        if masked.startswith("\\(", i):
            j = masked.find("\\)", i + 2)
            if j >= 0:
                body = text[i + 2:j].strip()
                out.append(text[last:i])
                out.append("$" + body + "$")
                last, i, n = j + 2, j + 2, n + 1
                continue
        i += 1
    out.append(text[last:])
    return "".join(out), n


def fix_markdown(text: str, merge: bool = True) -> tuple[str, int]:
    text, delim_changes = normalize_delimiters(text)
    if merge:
        text = merge_split_math(text)
    ranges = math_ranges(_mask_code(text))
    out, last, changes = [], 0, delim_changes
    for start, end, kind in ranges:
        pad = 1 if kind == "inline" else 2
        if end - start < pad * 2:
            continue
        out.append(text[last:start + pad])
        inner = text[start + pad:end - pad]
        fixed = fix_math(inner)
        if fixed != inner:
            changes += 1
        out.append(fixed)
        out.append(text[end - pad:end])
        last = end
    out.append(text[last:])
    text = "".join(out)
    stripped = fix_stars(text)
    if stripped != text:
        changes += 1
    return stripped, changes


def _is_punct(ch: str) -> bool:
    import unicodedata
    return bool(ch) and unicodedata.category(ch)[0] in "PS"


def _fence_block_ranges(text: str) -> list[tuple[int, int]]:
    """按独立成行的 $$ 围栏切块；比全局配对更稳，避免前面多一个 $$ 导致错位。"""
    out, open_at, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        if line.strip() == "$$":
            if open_at is None:
                open_at = pos
            else:
                out.append((open_at, pos + len(line)))
                open_at = None
        pos += len(line)
    return out


def fix_stars(text: str) -> str:
    """数学与代码之外、会触发 Markdown 斜体的单个 * 改成 \\*。"""
    chars = list(text)
    for start, end, _kind in math_ranges(text):
        for i in range(start, end):
            chars[i] = " "
    for start, end in _fence_block_ranges(text):
        for i in range(start, end):
            chars[i] = " "
    for m in FENCE.finditer(text):
        for i in range(m.start(), m.end()):
            chars[i] = " "
    for m in INLINE_CODE.finditer(text):
        for i in range(m.start(), m.end()):
            chars[i] = " "
    plain = "".join(chars)
    openers: list[int] = []
    escape: set[int] = set()
    for m in STAR_RUN.finditer(plain):
        if len(m.group(0)) != 1:
            continue
        i = m.start()
        prev = plain[i - 1] if i else " "
        nxt = plain[i + 1] if i + 1 < len(plain) else " "
        if prev == "\\":
            continue
        if i > 0 and plain[i - 1] == "\n" and (nxt.isspace() or not nxt):
            continue
        line_start = plain.rfind("\n", 0, i) + 1
        if not plain[line_start:i].strip() and nxt.isspace():
            continue
        left = not nxt.isspace() and (not _is_punct(nxt) or prev.isspace() or _is_punct(prev))
        right = not prev.isspace() and (not _is_punct(prev) or nxt.isspace() or _is_punct(nxt))
        if right and openers:
            opener = openers.pop()
            prev_outer = plain[opener - 1] if opener else " "
            next_outer = plain[i + 1] if i + 1 < len(plain) else " "
            content = plain[opener + 1:i]
            outer_alnum = (
                (prev_outer.isascii() and prev_outer.isalnum())
                or (next_outer.isascii() and next_outer.isalnum())
            )
            formula_like = (
                bool(re.search(r"[=+\-*/^_\\]", content))
                and bool(re.search(r"[A-Za-z0-9]", content))
                and not re.match(r"\s*[一-鿿]", content)
            )
            # *斜体* 多为中文句子、两侧留白/标点；像乘号的才转义
            if outer_alnum or formula_like:
                escape.add(opener)
                escape.add(i)
        elif left:
            openers.append(i)
    for i in sorted(escape, reverse=True):
        text = text[:i] + "\\" + text[i:]
    return text


def _option_texts(options) -> list[tuple[dict | None, int, str]]:
    out: list[tuple[dict | None, int, str]] = []
    if isinstance(options, list):
        for i, opt in enumerate(options):
            if isinstance(opt, dict):
                out.append((opt, i, "name"))
            else:
                out.append((None, i, ""))
    return out


def _fix_list(item: dict, key: str, counter: list[int], merge: bool = True) -> None:
    text = str(item.get(key) or "")
    fixed, n = fix_markdown(text, merge)
    if n:
        item[key] = fixed
        counter[0] += 1


def fix_questions_dir(qdir: Path, apply: bool, merge: bool = True) -> tuple[int, int]:
    files = 0
    fields = 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path in sorted(qdir.glob("*.json")):
        if path.name in {"config.json", "progress.json"} or path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        changed = [0]
        for q in data:
            if not isinstance(q, dict):
                continue
            for key in ("title", "parse"):
                _fix_list(q, key, changed, merge)
            for opt in q.get("options") or []:
                if isinstance(opt, dict):
                    _fix_list(opt, "name", changed, merge)
            for sub in q.get("subs") or []:
                if not isinstance(sub, dict):
                    continue
                for key in ("title", "parse"):
                    _fix_list(sub, key, changed, merge)
                for opt in sub.get("options") or []:
                    if isinstance(opt, dict):
                        _fix_list(opt, "name", changed, merge)
        if changed[0]:
            files += 1
            fields += changed[0]
            print(f"{path.name}: {changed[0]} 个字段")
            if apply:
                shutil.copy2(path, path.with_name(path.name + f".bak-{stamp}"))
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    return files, fields


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="自动修正公式渲染问题（默认 dry-run）")
    p.add_argument("--file", action="append", default=[], help="Markdown 文件，可重复")
    p.add_argument("--dir", help="Markdown 目录")
    p.add_argument("--questions-dir", help="章节 JSON 目录")
    p.add_argument("--apply", action="store_true", help="写盘（默认只报告）")
    p.add_argument("--no-katex", action="store_true", help="写盘后不跑 KaTeX 复检")
    p.add_argument("--no-merge", action="store_true",
                   help="不自动合并被 $ 拆开的集合式（保守模式）")
    args = p.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    total_files = total_fields = 0
    if args.questions_dir:
        total_files, total_fields = fix_questions_dir(
            Path(args.questions_dir), args.apply, not args.no_merge
        )
        target = Path(args.questions_dir)
    else:
        files = [Path(f) for f in args.file]
        if args.dir:
            files += sorted(Path(args.dir).glob("*.md"))
        if not files:
            p.error("需要 --file / --dir / --questions-dir 之一")
        for path in files:
            text = read_text_any(path)
            fixed, n = fix_markdown(text, not args.no_merge)
            if n:
                total_files += 1
                total_fields += n
                print(f"{path.name}: {n} 处")
                if args.apply:
                    shutil.copy2(path, path.with_name(path.name + f".bak-{stamp}"))
                    path.write_text(fixed, encoding="utf-8")
        target = Path(args.dir) if args.dir else (files[0].parent if files else Path("."))

    mode = "已写盘" if args.apply else "dry-run，未写盘"
    print(f"\n{mode}：{total_files} 个文件，{total_fields} 处修正")
    if not args.apply or total_fields == 0 or args.no_katex:
        return 0

    try:
        import check_katex
    except ImportError:
        print("（未找到 check_katex.py，跳过复检）")
        return 0
    try:
        if args.questions_dir:
            errors, warns, note = check_katex.check_questions(Path(args.questions_dir))
        else:
            errors, warns, note = check_katex.check_markdown(
                sorted(Path(args.dir).glob("*.md")) if args.dir else [Path(f) for f in args.file]
            )
    except check_katex.KatexUnavailable as exc:
        print(f"（跳过 KaTeX 复检：{exc}）")
        return 0
    print(f"复检：{note}，{len(errors)} error，{len(warns)} warning")
    for row in (errors + warns)[:20]:
        print(f"  ⚠ {row}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
