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
import unicodedata
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
DIFF_VALUES = frozenset({0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9})
TYPE_TO_CN = {
    "choice": "单选题",
    "multiple": "多选题",
    "judge": "判断题",
    "group": "组合题",
    "fill": "填空题",
    "short": "简答题",
}
MULTI_MIN_ANSWERS = 2
MULTI_MAX_ANSWERS = 5
MULTI_TWO_RATIO = 0.25
MULTI_SAMPLE_MIN = 8
GROUP_ROOT_MIN = 150
OPT_LEAK_RATIO = 1.45
OPT_LEAK_SHARE_ERROR = 0.10
JUDGE_CHAPTER_MIN = 8
DUP_NEAR = 0.60
DUP_NEAR_HARD = 0.90

_FENCE = re.compile(r"(```|~~~).*?\1", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_BAD_DOLLAR_BRACE = re.compile(r"(?<!\\)\$\{")
_UNICODE_RADICAL = re.compile(r"[√∛∜](?=\s*[\dA-Za-z(（\[\{])")  # 后面跟数字/字母/括号才算根号，`故√。` 是对勾
_MATH_BLOCK = re.compile(r"\$\$(.*?)\$\$", re.S)
_MATH_INLINE = re.compile(r"(?<!\\)\$([^$\n]+?)(?<!\\)\$")
_MATH_PAREN = re.compile(r"\\\((.*?)\\\)", re.S)
_MATH_BRACKET = re.compile(r"\\\[(.*?)\\\]", re.S)
_LATEX_LEFTOVER = re.compile(r"\\\\[A-Za-z]{2,}")  # \\x 是合法的换行，命令名至少 2 字符
_LATEX_TAG = re.compile(r"\\tag\*?\{")
_LATEX_LEFT = re.compile(r"\\left(?![A-Za-z])")
_LATEX_RIGHT = re.compile(r"\\right(?![A-Za-z])")
_LATEX_ENV = re.compile(r"\\(begin|end)\{([^}]*)\}")
_MATH_CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+")
_TEXT_OPEN = re.compile(r"\\text\{")
_STAR_RUN = re.compile(r"\*+")


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


def check_fill_blank(title: str, where: str, errs: list[str], warns: list[str]) -> None:
    """填空题作答位统一为 16 个半角下划线。"""
    half_runs = re.findall(r"_{2,}", title)
    full_runs = re.findall(r"＿{2,}", title)
    if not half_runs and not full_runs:
        errs.append(f"{where} fill 题干缺少连续下划线作答位")
        return
    if full_runs and not half_runs:
        errs.append(f"{where} fill 作答位须用 16 个半角下划线 `________________`")
        return
    if any(len(r) != 16 for r in half_runs):
        warns.append(
            f"{where} fill 作答位长度不统一（"
            + "/".join(str(len(r)) for r in half_runs)
            + "），应为 16 个"
        )


def _latex_snippet(text: str, pos: int, limit: int = 36) -> str:
    start = max(0, pos - 8)
    return text[start : pos + limit].replace("\n", " ").strip()


def _check_latex_body(body: str, where: str, errs: list[str], warns: list[str]) -> None:
    if not body.strip():
        warns.append(f"{where} 空公式")
        return
    bare = body.replace("\\{", "").replace("\\}", "")
    if bare.count("{") != bare.count("}"):
        errs.append(
            f"{where} 公式花括号不配对（{bare.count('{')} 开 / {bare.count('}')} 闭）："
            f"{body[:40]}"
        )
    left = len(_LATEX_LEFT.findall(body))
    right = len(_LATEX_RIGHT.findall(body))
    if left != right:
        errs.append(f"{where} 公式 \\left/\\right 不配对（{left}/{right}）：{body[:40]}")
    stack: list[str] = []
    bad_env = False
    for kind, env in _LATEX_ENV.findall(body):
        if kind == "begin":
            stack.append(env)
        elif not stack or stack.pop() != env:
            errs.append(f"{where} 公式 \\end{{{env}}} 与 \\begin 不匹配")
            bad_env = True
            break
    if stack and not bad_env:
        errs.append(f"{where} 公式 \\begin{{{stack[-1]}}} 未闭合")
    if _LATEX_TAG.search(body):
        warns.append(f"{where} 公式含 \\tag，KaTeX 不兼容，应删除编号")


def _math_ranges(text: str) -> list[tuple[int, int, str]]:
    """返回 (起, 止, 类型)；类型为 block/inline/paren/bracket。"""
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


def _math_bodies(text: str):
    for start, end, kind in _math_ranges(text):
        pad = 1 if kind == "inline" else 2
        if end - start >= pad * 2:
            yield text[start + pad : end - pad]


def _text_ranges(body: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for m in _TEXT_OPEN.finditer(body):
        depth = 0
        for j in range(m.end() - 1, len(body)):
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append((m.start(), j + 1))
                    break
    return out


def check_math_chinese(text: str, where: str, errs: list[str]) -> None:
    """数学环境里的中文/全角标点必须包在 \\text{} 内。"""
    for body in _math_bodies(text):
        protected = _text_ranges(body)
        for m in _MATH_CJK.finditer(body):
            if any(a <= m.start() < b for a, b in protected):
                continue
            errs.append(
                f"{where} 数学环境里的中文/全角标点未包 \\text{{}}：{_latex_snippet(body, m.start())}"
            )
            break


def _is_punct(ch: str) -> bool:
    return bool(ch) and unicodedata.category(ch)[0] in "PS"


def check_plain_star_emphasis(text: str, where: str, warns: list[str]) -> None:
    """数学与代码之外的单个 * 成对时，Markdown 会渲染成斜体。"""
    chars = list(text)
    for start, end, _kind in _math_ranges(text):
        for i in range(start, end):
            chars[i] = " "
    plain = "".join(chars)
    openers: list[int] = []
    for m in _STAR_RUN.finditer(plain):
        if len(m.group(0)) != 1:
            continue
        i = m.start()
        if i and plain[i - 1] == "\\":
            continue
        prev = plain[i - 1] if i else " "
        nxt = plain[i + 1] if i + 1 < len(plain) else " "
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
            if not (outer_alnum or formula_like):
                continue  # 更像有意写的 *斜体*
            warns.append(
                f"{where} 单独的 * 成对出现，可能被 Markdown 渲染成斜体，应写成 \\*："
                f"{_latex_snippet(plain, i)}"
            )
            return
        if left:
            openers.append(i)


def check_latex_text(text: str, where: str, errs: list[str], warns: list[str]) -> None:
    """题目字段的 LaTeX 可渲染性检查。只读，报错不自动修。"""
    clean = _INLINE_CODE.sub("", _FENCE.sub("", str(text or "")))
    if not clean.strip():
        return

    hit = _BAD_DOLLAR_BRACE.search(clean)
    if hit:
        errs.append(
            f"{where} 公式花括号紧贴 $（`${{`），花括号不会渲染：{_latex_snippet(clean, hit.start())}"
            "；需显示花括号时写成 `$\\{...\\}$`（花括号前加反斜杠），否则去掉多余花括号"
        )

    radical = _UNICODE_RADICAL.search(clean)
    if radical:
        errs.append(
            f"{where} 使用 Unicode 根号 `{radical.group(0)}`，无法覆盖根号内全部内容："
            f"{_latex_snippet(clean, radical.start())}；改用 `$\\sqrt{{...}}$`（立方根 `$\\sqrt[3]{{...}}$`）"
        )

    leftover = _LATEX_LEFTOVER.search(clean)
    if leftover:
        errs.append(
            f"{where} LaTeX 反斜杠重复（JSON 转义残留，解析后仍是 "
            f"`{leftover.group(0)}`）：{_latex_snippet(clean, leftover.start())}"
        )

    if clean.count("$$") % 2:
        errs.append(f"{where} `$$` 数量为奇数（{clean.count('$$')}），公式块未闭合")

    for m in _MATH_BLOCK.finditer(clean):
        _check_latex_body(m.group(1), where, errs, warns)
        start = clean.rfind("\n", 0, m.start()) + 1
        end = clean.find("\n", m.end())
        end = len(clean) if end == -1 else end
        before = clean[start : m.start()].strip()
        after = clean[m.end() : end].strip()
        if before or after:
            warns.append(f"{where} `$$` 块与正文同一行，应独占一行：{_latex_snippet(clean, m.start())}")

    rest = _MATH_BLOCK.sub(lambda m: "\n" * m.group(0).count("\n"), clean)
    for i, line in enumerate(rest.splitlines(), 1):
        positions = [m.start() for m in re.finditer(r"(?<!\\)\$", line)]
        if len(positions) % 2:
            warns.append(
                f"{where} 第 {i} 行 `$` 不成对（{len(positions)} 个），疑似公式截断："
                f"{_latex_snippet(line, positions[-1] if positions else 0)}"
            )
            continue
        for a, b in zip(positions[::2], positions[1::2]):
            _check_latex_body(line[a + 1 : b], where, errs, warns)

    for is_paren, regex in ((True, _MATH_PAREN), (False, _MATH_BRACKET)):
        for m in regex.finditer(rest):
            _check_latex_body(m.group(1), where, errs, warns)
        opened = len(re.findall(r"\\\(", rest)) if is_paren else len(re.findall(r"\\\[", rest))
        closed = len(re.findall(r"\\\)", rest)) if is_paren else len(re.findall(r"\\\]", rest))
        label = "\\( \\)" if is_paren else "\\[ \\]"
        if opened != closed:
            errs.append(f"{where} {label} 数量不等（{opened}/{closed}）")

    if "$" in rest and ("\\(" in rest or "\\[" in rest):
        warns.append(f"{where} 混用 $ 与 \\( / \\[ 分隔符，题目统一用 $...$")

    check_math_chinese(clean, where, errs)
    check_plain_star_emphasis(clean, where, warns)


def check_latex_question(q: dict, idx: int, errs: list[str], warns: list[str]) -> None:
    """检查顶层题与组合子题的 title/options/parse。"""
    def fields(item: dict, prefix: str) -> None:
        title = str(item.get("title") or "")
        if title:
            check_latex_text(title, f"{prefix} title", errs, warns)
        parse = str(item.get("parse") or "")
        if parse:
            check_latex_text(parse, f"{prefix} parse", errs, warns)
        for key, text in _option_texts(item.get("options")).items():
            if text:
                check_latex_text(text, f"{prefix} 选项{key}", errs, warns)

    fields(q, f"#{idx}")
    subs = q.get("subs")
    if isinstance(subs, list):
        for si, sub in enumerate(subs, 1):
            if isinstance(sub, dict):
                fields(sub, f"#{idx} 子题{si}")


def check_one(
    q,
    idx: int,
    book_id: int | None,
    book: str,
    tag: str,
    errs: list[str],
    warns: list[str],
    lenient: bool = False,
):
    where = f"#{idx}"
    if not isinstance(q, dict):
        errs.append(f"{where} 不是对象")
        return None
    if book_id is not None and q.get("know_id") != book_id:
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
    elif float(d) not in DIFF_VALUES:
        msg = (
            f"{where} difficulty={d} 不是允许档位"
            "（仅 0.2/0.3/0.4/0.5/0.6/0.7/0.8/0.9）"
        )
        (warns if lenient else errs).append(msg)

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
        if typ == "multiple" and isinstance(answer, str):
            letters = answer.strip()
            if re.fullmatch(r"[A-H]+", letters):
                n = len(set(letters))
                if not (MULTI_MIN_ANSWERS <= n <= MULTI_MAX_ANSWERS):
                    msg = (
                        f"{where} multiple 正确答案 {n} 项，"
                        f"应为 {MULTI_MIN_ANSWERS}–{MULTI_MAX_ANSWERS} 项：{answer!r}"
                    )
                    (warns if lenient else errs).append(msg)
    elif typ == "judge":
        if answer not in ("是", "否"):
            errs.append(f"{where} judge answer 应为 是/否：{answer!r}")
    elif typ == "fill":
        if answer != "":
            errs.append(f"{where} fill answer 必须为空字符串：{answer!r}")
        check_fill_blank(title, where, errs, warns)
    elif typ == "group":
        subs = q.get("subs")
        if not isinstance(subs, list) or not (2 <= len(subs) <= 5):
            errs.append(f"{where} group subs 应为 2–5 个")
        else:
            if len(title) < GROUP_ROOT_MIN:
                msg = (
                    f"{where} group 根题干不足 {GROUP_ROOT_MIN} 字"
                    f"（{len(title)}）"
                )
                (warns if lenient else errs).append(msg)
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
                    if st == "multiple":
                        sub_answer = str(sub.get("answer") or "").strip()
                        if re.fullmatch(r"[A-H]+", sub_answer):
                            n = len(set(sub_answer))
                            if not (MULTI_MIN_ANSWERS <= n <= MULTI_MAX_ANSWERS):
                                msg = (
                                    f"{where} group 子题 {si} multiple 正确答案 {n} 项，"
                                    f"应为 {MULTI_MIN_ANSWERS}–{MULTI_MAX_ANSWERS} 项：{sub_answer!r}"
                                )
                                (warns if lenient else errs).append(msg)
                elif st == "judge":
                    if sub.get("answer") not in ("是", "否"):
                        errs.append(f"{where} group 子题 {si} judge answer 应为 是/否")
                elif st == "fill":
                    if sub.get("answer") != "":
                        errs.append(
                            f"{where} group 子题 {si} fill answer 必须为空字符串"
                        )
                    check_fill_blank(
                        str(sub.get("title") or ""),
                        f"{where} group 子题 {si}",
                        errs,
                        warns,
                    )
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


def expected_type_counts(config_path: Path | None) -> dict[str, dict[str, int]] | None:
    """按 counts×rounds 算每个题型、每个难度的目标题数。"""
    if not config_path:
        return None
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        q = cfg.get("question", {}) if isinstance(cfg.get("question"), dict) else {}
        counts = cfg.get("counts", q.get("counts", {}))
        rounds = max(1, int(cfg.get("rounds", q.get("rounds", 1))))
    except (OSError, json.JSONDecodeError, AttributeError, ValueError, TypeError):
        return None
    out: dict[str, dict[str, int]] = {}
    for typ, cn in TYPE_TO_CN.items():
        row = counts.get(cn)
        if not isinstance(row, dict):
            continue
        out[typ] = {}
        for name, _, _ in DIFF_BANDS:
            value = row.get(name, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[typ][name] = int(value) * rounds
    return out or None


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


def _band_values(values: list[float]) -> dict[str, list[float]]:
    out = {name: [] for name, _, _ in DIFF_BANDS}
    for d in values:
        for name, lo, hi in DIFF_BANDS:
            if lo <= d <= hi:
                out[name].append(d)
                break
    return out


def check_difficulty_distribution(
    chapter_diffs: dict[str, list[float]],
    all_diffs: list[float],
    target: dict[str, float] | None,
    errs: list[str],
    warns: list[str],
    lenient: bool,
) -> None:
    """默认严格校验 3:4:3；lenient 时只告警。不修改任何 difficulty。"""
    def add(msg: str, hard: bool) -> None:
        (warns if lenient or not hard else errs).append(msg)

    def check_intra_band(label: str, values: list[float], min_n: int) -> None:
        for name, vals in _band_values(values).items():
            if len(vals) < min_n:
                continue
            value, count = Counter(vals).most_common(1)[0]
            if count / len(vals) > 0.70:
                warns.append(
                    f"{label}「{name}」档内 difficulty={value} 占 {count}/{len(vals)}"
                    f"（{count / len(vals):.0%}），档内取值过于单一"
                )

    n = len(all_diffs)
    check_intra_band("全书", all_diffs, 30)
    if not target:
        if n >= 100:
            worst = max(band_counts(all_diffs).values()) / n
            if worst > 0.70:
                warns.append(f"全书难度分布单一：{_band_text(all_diffs)}")
        for chapter, values in chapter_diffs.items():
            if len(values) >= 20:
                worst = max(band_counts(values).values()) / len(values)
                if worst > 0.70:
                    warns.append(f"{chapter} 难度分布单一：{_band_text(values)}")
                check_intra_band(chapter, values, 20)
        return

    if n < 40:
        warns.append(f"全书难度样本不足（{n} < 40），无法严格判定 3:4:3")
    else:
        counts = band_counts(all_diffs)
        limit = 0.03 if n >= 100 else 0.05
        for name, ratio in target.items():
            actual = counts[name] / n
            diff = abs(actual - ratio)
            if diff > limit:
                add(
                    f"全书难度「{name}」占比 {actual:.0%}，目标 {ratio:.0%}，"
                    f"偏差 {diff:.0%}（允许 ≤{limit:.0%}）",
                    hard=True,
                )

    for chapter, values in chapter_diffs.items():
        cn = len(values)
        if cn < 20:
            continue
        check_intra_band(chapter, values, 20)
        counts = band_counts(values)
        limit = 0.05 if cn >= 40 else 0.10
        for name, ratio in target.items():
            actual = counts[name] / cn
            diff = abs(actual - ratio)
            if diff > limit:
                add(
                    f"{chapter} 难度「{name}」占比 {actual:.0%}，目标 {ratio:.0%}，"
                    f"偏差 {diff:.0%}（样本 {cn}，允许 ≤{limit:.0%}）",
                    hard=cn >= 40,
                )


def check_type_difficulty(
    chapter_type_diffs: dict[tuple[str, str], list[float]],
    all_type_diffs: dict[str, list[float]],
    targets: dict[str, dict[str, int]] | None,
    warns: list[str],
) -> None:
    """核对「题型 × 难度」配额；去重损耗较大，只告警。"""
    if not targets:
        return

    def check(label: str, typ: str, values: list[float]) -> None:
        expected = targets.get(typ)
        if not expected or len(values) < 8:
            return
        counts = band_counts(values)
        for name, _, _ in DIFF_BANDS:
            want = expected.get(name, 0)
            if want <= 0:
                continue
            got = counts[name]
            tol = max(2, int(round(want * 0.20)))
            if abs(got - want) > tol:
                warns.append(
                    f"{label}{TYPE_TO_CN.get(typ, typ)}「{name}」{got} 道，"
                    f"配置目标 {want} 道，偏差超过 {tol}"
                )

    for (chapter, typ), values in chapter_type_diffs.items():
        check(f"{chapter} ", typ, values)
    for typ, values in all_type_diffs.items():
        check("全书", typ, values)


def collect_option_answers(q: dict) -> list[tuple[str, str, int]]:
    """收集 choice/multiple 的 (type, answer, 选项数)，group 递归。"""
    out: list[tuple[str, str, int]] = []

    def take(item) -> None:
        if not isinstance(item, dict):
            return
        typ = item.get("type")
        if typ not in ("choice", "multiple"):
            return
        answer = str(item.get("answer") or "").strip()
        if not answer:
            return
        opts = item.get("options")
        count = len(opts) if isinstance(opts, list) else 0
        out.append((typ, answer, count))

    take(q)
    subs = q.get("subs")
    if isinstance(subs, list):
        for sub in subs:
            take(sub)
    return out


def _option_texts(options) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(options, list):
        return out
    for i, opt in enumerate(options):
        if isinstance(opt, dict):
            key = str(
                opt.get("key") or opt.get("letter") or opt.get("label") or ""
            ).strip().upper()
            if not key:
                key = chr(65 + i)
            name = opt.get("name")
            if name is None:
                name = opt.get("text") or opt.get("value") or ""
            out[key] = str(name)
        else:
            out[chr(65 + i)] = str(opt)
    return out


_LATEX_CMD = re.compile(r"\\[A-Za-z]+")
_VISIBLE_CMD = {
    "sqrt": "√", "frac": "/", "dfrac": "/", "tfrac": "/",
    "times": "×", "cdot": "·", "pm": "±", "mp": "∓",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "infty": "∞", "pi": "π", "alpha": "α", "beta": "β",
    "gamma": "γ", "Delta": "Δ", "theta": "θ", "lambda": "λ", "mu": "μ",
    "sigma": "σ", "varphi": "φ", "varepsilon": "ε", "omega": "ω",
    "Omega": "Ω", "circ": "°", "triangle": "△",
}


def visible_len(text: str) -> int:
    """按渲染后的可见字符估算长度，避免 \\sqrt{2} 这类标记把选项撑长。"""
    s = str(text or "")
    s = re.sub(r"\\([A-Za-z]+)", lambda m: _VISIBLE_CMD.get(m.group(1), ""), s)
    s = re.sub(r"[${}\\^_]", "", s)
    s = re.sub(r"\s+", "", s)
    return len(s)


def collect_option_lengths(q: dict) -> list[tuple[int, int, str]]:
    """收集 (正确项最长长度, 干扰项最长长度, 题干)，group 递归。"""
    out: list[tuple[int, int, str]] = []

    def take(item) -> None:
        if not isinstance(item, dict) or item.get("type") not in ("choice", "multiple"):
            return
        answer = str(item.get("answer") or "").strip().upper()
        if not answer:
            return
        texts = _option_texts(item.get("options"))
        if len(texts) < 2:
            return
        # 纯数字/公式短选项（如 22.36V vs √50V）长度差没有泄露意义
        joined = "".join(texts.values())
        if (not re.search(r"[一-鿿]", joined)
                and max(visible_len(v) for v in texts.values()) <= 14):
            return
        correct = [visible_len(v) for k, v in texts.items() if k in answer and v]
        wrong = [visible_len(v) for k, v in texts.items() if k not in answer and v]
        if correct and wrong:
            out.append((max(correct), max(wrong), str(item.get("title") or "")))

    take(q)
    subs = q.get("subs")
    if isinstance(subs, list):
        for sub in subs:
            take(sub)
    return out


def collect_judge_answers(q: dict) -> list[str]:
    """收集顶层判断题答案；组合题子题不参与比例判定。"""
    if q.get("type") == "judge" and q.get("answer") in ("是", "否"):
        return [str(q["answer"])]
    return []


def collect_multiples(q: dict) -> list[tuple[str, int, bool]]:
    """收集 (answer, 选项数, 是否组合题子题)。"""
    out: list[tuple[str, int, bool]] = []

    def take(item, is_sub: bool) -> None:
        if not isinstance(item, dict) or item.get("type") != "multiple":
            return
        answer = str(item.get("answer") or "").strip()
        if not answer:
            return
        opts = item.get("options")
        count = len(opts) if isinstance(opts, list) else 0
        out.append((answer, count, is_sub))

    take(q, False)
    subs = q.get("subs")
    if isinstance(subs, list):
        for sub in subs:
            take(sub, True)
    return out


def check_multi_two_ratio(
    chapter_multiples: dict[str, list[tuple[str, int, bool]]],
    all_multiples: list[tuple[str, int, bool]],
    errs: list[str],
    warns: list[str],
    lenient: bool,
    target_zero: bool = False,
) -> None:
    """顶层多选题至少 25% 答案为两项；组合题子题单独打印。"""
    if target_zero:
        return

    def add(msg: str) -> None:
        (warns if lenient else errs).append(msg)

    def judge(label: str, pairs: list[tuple[str, int, bool]]) -> None:
        top = [a for a, _n, is_sub in pairs if not is_sub]
        n = len(top)
        if n == 0:
            return
        if n < MULTI_SAMPLE_MIN:
            warns.append(f"{label}多选题仅 {n} 道，样本不足，无法判定两项答案 ≥25%")
            return
        two = sum(1 for a in top if len(set(a)) == 2)
        if two * 4 < n:
            need = (n + 3) // 4
            add(
                f"{label}多选两项答案 {two}/{n}（{two / n:.0%}），"
                f"低于 25%（至少需 {need} 道只选 2 项）"
            )

    judge("全书", all_multiples)
    for chapter, pairs in chapter_multiples.items():
        judge(f"{chapter} ", pairs)

    subs = [a for a, _n, is_sub in all_multiples if is_sub]
    if subs:
        two = sum(1 for a in subs if len(set(a)) == 2)
        warns.append(
            f"组合题子题多选 {len(subs)} 道，其中两项答案 {two} 道"
            f"（{two / len(subs):.0%}），仅作参考，不计入 25% 判定"
        )


def answer_distribution(pairs: list[tuple[str, str, int]]) -> tuple[str, str]:
    choices = Counter(answer for typ, answer, _ in pairs if typ == "choice")
    n = sum(choices.values())
    single = ""
    if n:
        single = "，".join(
            f"{k} {choices.get(k, 0)}({choices.get(k, 0) / n:.0%})" for k in "ABCD"
        )
    multiples = Counter(answer for typ, answer, _ in pairs if typ == "multiple")
    total = sum(multiples.values())
    multi = ""
    if total:
        multi = "，".join(
            f"{k} {v}({v / total:.0%})" for k, v in multiples.most_common(5)
        )
    return single, multi


def check_answer_balance(
    chapter_answers: dict[str, list[tuple[str, str, int]]],
    all_answers: list[tuple[str, str, int]],
    errs: list[str],
    warns: list[str],
    lenient: bool,
) -> None:
    """答案分布严重失衡时判错；lenient 时只告警。不修改任何题目。"""

    def add(msg: str) -> None:
        (warns if lenient else errs).append(msg)

    def check(label: str, pairs: list[tuple[str, str, int]], min_n: int) -> None:
        choices = [(answer, count) for typ, answer, count in pairs if typ == "choice"]
        n = len(choices)
        if n >= min_n:
            counts = Counter(answer for answer, _ in choices)
            worst = max(counts.values()) / n
            if worst > 0.60:
                letter, count = counts.most_common(1)[0]
                add(f"{label}单选答案集中：{letter} {count}/{n}（{worst:.0%}）")
            for letter in "ABCD":
                index = "ABCD".index(letter)
                available = sum(1 for _answer, count in choices if count > index)
                if available >= min_n and counts.get(letter, 0) == 0:
                    add(
                        f"{label}单选答案缺失：{letter}（可用 {available} 题，0 题选它）"
                    )
        if n >= 50:
            counts = Counter(answer for answer, _ in choices)
            for letter in "ABCD":
                index = "ABCD".index(letter)
                available = sum(1 for _answer, count in choices if count > index)
                if available < 50:
                    continue
                ratio = counts.get(letter, 0) / n
                if abs(ratio - 0.25) > 0.15:
                    add(
                        f"{label}单选 {letter} 占比 {ratio:.0%}，偏离 25% 超过 15 个百分点"
                    )
        multiples = [answer for typ, answer, _ in pairs if typ == "multiple"]
        if len(multiples) >= 8:
            counts = Counter(multiples)
            combo, count = counts.most_common(1)[0]
            ratio = count / len(multiples)
            if ratio > 0.50:
                add(
                    f"{label}多选答案组合集中：{combo} {count}/{len(multiples)}（{ratio:.0%}）"
                )

    check("全书", all_answers, 50)
    for chapter, pairs in chapter_answers.items():
        check(f"{chapter} ", pairs, 8)


def check_option_lengths(
    chapter_lengths: dict[str, list[tuple[int, int, str]]],
    errs: list[str],
    warns: list[str],
    lenient: bool,
) -> None:
    """正确项 ≥ 最长干扰项 ×1.45 即判泄露：单题即错，lenient 时只告警。"""
    total = 0
    leaked = 0
    for chapter, pairs in chapter_lengths.items():
        n = len(pairs)
        if not n:
            continue
        bad = [(c, w, t) for c, w, t in pairs if w and c >= w * OPT_LEAK_RATIO]
        total += n
        leaked += len(bad)
        if not bad:
            continue
        examples = "；例：" + " / ".join(t[:20] for _c, _w, t in bad[:3])
        msg = (
            f"{chapter} 选项长度泄露 {len(bad)}/{n}（{len(bad) / n:.0%}），"
            f"正确项 ≥ 最长干扰项 ×{OPT_LEAK_RATIO}{examples}"
        )
        (warns if lenient else errs).append(msg)
    if total >= 20 and leaked / total > OPT_LEAK_SHARE_ERROR:
        msg = (
            f"全书选项长度泄露 {leaked}/{total}（{leaked / total:.0%}），"
            f"超过 {OPT_LEAK_SHARE_ERROR:.0%}"
        )
        (warns if lenient else errs).append(msg)


def check_judge_balance(
    chapter_judges: dict[str, list[str]],
    all_judges: list[str],
    errs: list[str],
    warns: list[str],
    lenient: bool,
) -> None:
    """判断题「是/否」应大致均衡；严重偏置判错，lenient 时告警。"""

    def ratio(values: list[str]) -> float:
        return sum(1 for v in values if v == "是") / len(values)

    if len(all_judges) >= 40:
        r = ratio(all_judges)
        if abs(r - 0.5) > 0.10:
            (warns if lenient else errs).append(
                f"全书判断题「是」占 {r:.0%}，偏离 50% 超过 10 个百分点"
            )
        elif abs(r - 0.5) > 0.05:
            warns.append(f"全书判断题「是」占 {r:.0%}，建议压到 45%–55%")
    for chapter, values in chapter_judges.items():
        if len(values) < JUDGE_CHAPTER_MIN:
            continue
        r = ratio(values)
        if abs(r - 0.5) > 0.20:
            (warns if lenient else errs).append(
                f"{chapter} 判断题「是」占 {r:.0%}，偏离 50% 超过 20 个百分点"
            )
        elif abs(r - 0.5) > 0.10:
            warns.append(f"{chapter} 判断题「是」占 {r:.0%}，建议压到 40%–60%")


def cross_chapter_dups(
    records: list[tuple[str, str]],
    errs: list[str],
    warns: list[str],
    lenient: bool,
) -> None:
    """同章/跨章完全相同记错误；≥0.90 记错误，0.60–0.90 记警告。"""
    exact: dict[str, str] = {}
    buckets: dict[str, list[tuple[str, str, str]]] = {}
    reported_hard = 0
    reported_soft = 0
    for chapter, title in records:
        n = norm_title(title)
        if not n:
            continue
        prev = exact.get(n)
        if prev is not None:
            label = "同章重复题" if prev == chapter else "跨章重复题"
            (warns if lenient else errs).append(
                f"{label}：{prev} 与 {chapter} 题干完全相同：{title[:40]}"
            )
        else:
            exact[n] = chapter
        for n2, ch2, t2 in buckets.get(n[:10], []):
            if n2 == n:
                continue
            if abs(len(n) - len(n2)) > 30:
                continue
            ratio = SequenceMatcher(None, n, n2).ratio()
            same = "同章" if ch2 == chapter else "跨章"
            if ratio >= DUP_NEAR_HARD:
                if reported_hard < 20:
                    (warns if lenient else errs).append(
                        f"{same}近似题：{ch2} / {chapter} 相似度≥{DUP_NEAR_HARD:.2f}："
                        f"{t2[:30]} ↔ {title[:30]}"
                    )
                reported_hard += 1
            elif ratio >= DUP_NEAR:
                if reported_soft < 20:
                    warns.append(
                        f"{same}近似题：{ch2} / {chapter} 相似度≥{DUP_NEAR:.2f}："
                        f"{t2[:30]} ↔ {title[:30]}"
                    )
                reported_soft += 1
        buckets.setdefault(n[:10], []).append((n, chapter, title))
    if reported_hard > 20:
        (warns if lenient else errs).append(
            f"… 相似度≥{DUP_NEAR_HARD:.2f} 的近似题共 {reported_hard} 对，其余省略"
        )
    if reported_soft > 20:
        warns.append(f"… 相似度 0.60–0.90 的近似题共 {reported_soft} 对，其余省略")


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
    p.add_argument("--no-id-check", action="store_true",
                   help="不校验 know_id（多 id 题库/教材出题产物）")
    p.add_argument("--questions-dir", help="默认 补题/题目/<id><书名>")
    p.add_argument("--split-dir", help="拆分后的章节 md 目录，用于检查漏章")
    p.add_argument("--book-json", help="通过后写 stages.questions=done")
    p.add_argument("--lenient", action="store_true",
                   help="存量旧批次复验：难度档位、3:4:3、多选两项比例、"
                        "根标题 150 字、答案分布、选项长度、判断题均衡只告警；"
                        "LaTeX 渲染错误仍为 error")
    p.add_argument("--no-katex", action="store_true",
                   help="跳过 KaTeX 实渲染检查（默认自动查找 node + katex）")
    p.add_argument("--katex-path", help="KaTeX 包目录，默认自动查找")
    p.add_argument("--strict-difficulty", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if not re.fullmatch(r"\d+", str(args.id).strip()):
        print(f"id 必须是纯数字：{args.id}", file=sys.stderr)
        return 2
    book_id = None if args.no_id_check else int(args.id)
    book = args.book.strip()
    qdir = (
        Path(args.questions_dir)
        if args.questions_dir
        else REPO_ROOT / "补题" / "题目" / f"{int(args.id)}{book}"
    )
    if not qdir.is_dir():
        print(f"题目目录不存在：{qdir}", file=sys.stderr)
        return 2

    expected = expected_counts(Path(args.config)) if args.config else None
    expected_ratio = expected_diff_ratios(Path(args.config)) if args.config else None
    expected_types = expected_type_counts(Path(args.config)) if args.config else None
    multi_target_zero = bool(
        expected_types
        and "multiple" in expected_types
        and sum(expected_types["multiple"].values()) == 0
    )
    errs: list[str] = []
    warns: list[str] = []
    type_dist: Counter = Counter()
    chapter_counts: dict[str, int] = {}
    chapter_diffs: dict[str, list[float]] = {}
    all_diffs: list[float] = []
    chapter_type_diffs: dict[tuple[str, str], list[float]] = {}
    all_type_diffs: dict[str, list[float]] = {}
    chapter_answers: dict[str, list[tuple[str, str, int]]] = {}
    all_answers: list[tuple[str, str, int]] = []
    chapter_multiples: dict[str, list[tuple[str, int, bool]]] = {}
    all_multiples: list[tuple[str, int, bool]] = []
    chapter_opt_lengths: dict[str, list[tuple[int, int]]] = {}
    chapter_judges: dict[str, list[str]] = {}
    all_judges: list[str] = []
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
            typ = check_one(q, i, book_id, book, tag, errs, warns, args.lenient)
            if typ:
                type_dist[typ] += 1
            if isinstance(q, dict):
                check_latex_question(q, i, errs, warns)
                dedup_records.append((path.stem, str(q.get("title") or "")))
                diffs = collect_difficulties(q)
                chapter_diffs.setdefault(path.stem, []).extend(diffs)
                all_diffs.extend(diffs)
                if typ in TYPES and diffs:
                    chapter_type_diffs.setdefault((path.stem, typ), []).extend(diffs)
                    all_type_diffs.setdefault(typ, []).extend(diffs)
                answers = collect_option_answers(q)
                chapter_answers.setdefault(path.stem, []).extend(answers)
                all_answers.extend(answers)
                multiples = collect_multiples(q)
                chapter_multiples.setdefault(path.stem, []).extend(multiples)
                all_multiples.extend(multiples)
                chapter_opt_lengths.setdefault(path.stem, []).extend(
                    collect_option_lengths(q)
                )
                judges = collect_judge_answers(q)
                chapter_judges.setdefault(path.stem, []).extend(judges)
                all_judges.extend(judges)
        if expected is not None:
            dev = abs(len(obj) - expected) / expected
            if dev > 0.2:
                msg = f"{path.name}: {len(obj)} 题，配置 {expected} 题，偏差 {dev:.0%}"
                (warns if args.lenient else errs).append(msg)

    # 章节齐全性
    split_dir = Path(args.split_dir) if args.split_dir else REPO_ROOT / "补题" / "拆分后" / f"{book_id}{book}"
    if split_dir.is_dir():
        expected_ch = {p.stem for p in split_dir.glob("*.md")} - SKIP_CHAPTERS
        missing = sorted(expected_ch - set(chapter_counts))
        if missing:
            errs.append(f"缺少章节 JSON {len(missing)} 个：{missing[:10]}")

    cross_chapter_dups(dedup_records, errs, warns, args.lenient)
    check_difficulty_distribution(
        chapter_diffs, all_diffs, expected_ratio, errs, warns, args.lenient
    )
    check_type_difficulty(chapter_type_diffs, all_type_diffs, expected_types, warns)
    check_answer_balance(chapter_answers, all_answers, errs, warns, args.lenient)
    check_option_lengths(chapter_opt_lengths, errs, warns, args.lenient)
    check_judge_balance(chapter_judges, all_judges, errs, warns, args.lenient)
    check_multi_two_ratio(
        chapter_multiples,
        all_multiples,
        errs,
        warns,
        args.lenient,
        multi_target_zero,
    )

    katex_note = ""
    if not args.no_katex:
        try:
            import check_katex
        except ImportError as exc:
            warns.append(f"跳过 KaTeX 渲染检查：无法导入 check_katex（{exc}）")
        else:
            try:
                k_errs, k_warns, _note = check_katex.check_questions(
                    qdir, katex_path=args.katex_path
                )
            except check_katex.KatexUnavailable as exc:
                warns.append(f"跳过 KaTeX 渲染检查：{exc}")
            else:
                errs.extend(k_errs)
                warns.extend(k_warns)
                katex_note = f"{_note}：{len(k_errs)} error，{len(k_warns)} warning"

    total = sum(chapter_counts.values())
    print(f"目录：{qdir}")
    print(f"章节：{len(chapter_counts)}  题目：{total}")
    for name, n in chapter_counts.items():
        print(f"  {n:>3}  {name}")
    if type_dist:
        print("题型分布：" + "，".join(f"{k}={v}" for k, v in sorted(type_dist.items())))
    if all_diffs:
        print("难度分布：" + _band_text(all_diffs))
    if katex_note:
        print(katex_note)
    single, multi = answer_distribution(all_answers)
    if single:
        print("单选答案：" + single)
    if multi:
        print("多选组合：" + multi)
    top_multiples = [a for a, _n, is_sub in all_multiples if not is_sub]
    if top_multiples:
        two = sum(1 for a in top_multiples if len(set(a)) == 2)
        print(
            f"多选两项答案：{two}/{len(top_multiples)}"
            f"（{two / len(top_multiples):.0%}）"
        )

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
