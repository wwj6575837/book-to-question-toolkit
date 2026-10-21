#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查详细版 Markdown 的公式渲染问题。只读，不修改文件。

用法：
    python check_formulas.py --file "exports/.../2552-1 ....md"
    python check_formulas.py --dir "exports/multi_agent/final/书籍库"
    python check_formulas.py --dir ... --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

from mdscan import read_text_any

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
ESCAPED_DOLLAR = re.compile(r"\\\$")
BLOCK = re.compile(r"\$\$(.*?)\$\$", re.S)
PAREN = re.compile(r"\\\((.*?)\\\)", re.S)
BRACKET = re.compile(r"\\\[(.*?)\\\]", re.S)
TAG = re.compile(r"\\tag\*?\{[^}]*\}")
HTML_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#\d+);")
LEFT = re.compile(r"\\left(?![A-Za-z])")
RIGHT = re.compile(r"\\right(?![A-Za-z])")


@dataclass
class Issue:
    file: str
    line: int
    level: str
    code: str
    message: str
    snippet: str = ""


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _snippet(text: str, pos: int, limit: int = 80) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end][:limit].strip()


def _check_body(body: str, file: str, line: int, issues: list[Issue], kind: str) -> None:
    if not body.strip():
        issues.append(Issue(file, line, "WARN", "empty-math", f"{kind} 公式为空"))

    depth = 0
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth < 0:
                issues.append(Issue(file, line, "ERROR", "brace", f"{kind} 公式花括号多余右括号"))
                depth = 0
        i += 1
    if depth:
        issues.append(Issue(file, line, "ERROR", "brace", f"{kind} 公式花括号未闭合（缺 {depth} 个）"))

    left, right = len(LEFT.findall(body)), len(RIGHT.findall(body))
    if left != right:
        issues.append(
            Issue(file, line, "ERROR", "left-right", f"{kind} 公式 \\left/\\right 不配对（{left}/{right}）")
        )

    stack: list[str] = []
    for m in re.finditer(r"\\(begin|end)\{([^}]+)\}", body):
        action, env = m.group(1), m.group(2)
        if action == "begin":
            stack.append(env)
            continue
        if not stack or stack[-1] != env:
            issues.append(
                Issue(file, line, "ERROR", "environment", f"{kind} 公式 \\end{{{env}}} 与 \\begin 不匹配")
            )
        else:
            stack.pop()
    for env in stack:
        issues.append(Issue(file, line, "ERROR", "environment", f"{kind} 公式 \\begin{{{env}}} 未闭合"))

    if TAG.search(body):
        issues.append(Issue(file, line, "WARN", "tag", f"{kind} 公式残留 \\tag"))
    if HTML_ENTITY.search(body):
        issues.append(Issue(file, line, "WARN", "html-entity", f"{kind} 公式含 HTML 实体"))


def _strip_fences(text: str, file: str, issues: list[Issue]) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_fence = False
    fence_line = 0
    for i, line in enumerate(lines, 1):
        if FENCE.match(line):
            if not in_fence:
                in_fence, fence_line = True, i
            else:
                in_fence = False
            out.append("")
            continue
        out.append("" if in_fence else INLINE_CODE.sub("", line))
    if in_fence:
        issues.append(
            Issue(file, fence_line, "ERROR", "fence", "代码围栏未闭合，后续公式检查不可靠")
        )
    return "\n".join(out)


def check_text(text: str, file: str = "<text>") -> list[Issue]:
    issues: list[Issue] = []
    clean = _strip_fences(text, file, issues)
    clean = ESCAPED_DOLLAR.sub("", clean)

    block_count = clean.count("$$")
    if block_count % 2:
        issues.append(Issue(file, 1, "ERROR", "block-pair", f"$$ 数量为奇数（{block_count}），存在未闭合块"))

    for m in BLOCK.finditer(clean):
        line = _line_of(clean, m.start())
        _check_body(m.group(1), file, line, issues, "$$")
        start = clean.rfind("\n", 0, m.start()) + 1
        end = clean.find("\n", m.end())
        if end == -1:
            end = len(clean)
        row = clean[start:end]
        before = row[: m.start() - start].strip(" \t$")
        after = row[m.end() - start :].strip(" \t$")
        if before or after:
            issues.append(
                Issue(file, line, "WARN", "block-inline", "$$ 块与正文文字在同一行", _snippet(clean, m.start()))
            )

    paren_open = len(re.findall(r"\\\(", clean))
    paren_close = len(re.findall(r"\\\)", clean))
    if paren_open != paren_close:
        issues.append(
            Issue(file, 1, "ERROR", "paren-pair", f"\\( 与 \\) 数量不等（{paren_open}/{paren_close}）")
        )
    bracket_open = len(re.findall(r"\\\[", clean))
    bracket_close = len(re.findall(r"\\\]", clean))
    if bracket_open != bracket_close:
        issues.append(
            Issue(file, 1, "ERROR", "bracket-pair", f"\\[ 与 \\] 数量不等（{bracket_open}/{bracket_close}）")
        )

    for m in PAREN.finditer(clean):
        line = _line_of(clean, m.start())
        _check_body(m.group(1), file, line, issues, "\\( \\)")
        if clean.count("\n", m.start(), m.end()) > 3:
            issues.append(Issue(file, line, "WARN", "long-inline", "行内公式跨超过 3 行"))
    for m in BRACKET.finditer(clean):
        line = _line_of(clean, m.start())
        _check_body(m.group(1), file, line, issues, "\\[ \\]")

    uses_dollar = "$" in clean
    uses_paren = "\\(" in clean or "\\[" in clean
    if uses_dollar and uses_paren:
        issues.append(Issue(file, 1, "WARN", "mixed-delimiter", "同一文件混用 $ 与 \\(/\\[ 分隔符"))

    # 行内 $：同一行内必须成对；奇数视为截断
    inline_clean = BLOCK.sub(lambda m: "\n" * m.group(0).count("\n"), clean)
    for i, line in enumerate(inline_clean.splitlines(), 1):
        positions = [m.start() for m in re.finditer(r"\$", line)]
        if not positions:
            continue
        if len(positions) % 2:
            issues.append(
                Issue(file, i, "WARN", "inline-pair", "行内 $ 数量为奇数，疑似截断", _snippet(line, positions[-1]))
            )
            continue
        for a, b in zip(positions[::2], positions[1::2]):
            _check_body(line[a + 1 : b], file, i, issues, "$")

    # 去重（同一文件同一行同一代码只报一次）
    seen = set()
    unique = []
    for issue in issues:
        key = (issue.line, issue.level, issue.code, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def collect_files(args: argparse.Namespace) -> list[Path]:
    if args.file:
        return [Path(args.file).expanduser()]
    root = Path(args.dir).expanduser()
    return sorted(root.rglob("*.md"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="检查 Markdown 公式渲染问题（只读）")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--dir")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--stats", action="store_true", help="只输出汇总")
    p.add_argument("--max-print", type=int, default=50, help="最多打印多少条问题")
    args = p.parse_args(argv)

    files = collect_files(args)
    if not files:
        print("没有找到 Markdown 文件", file=sys.stderr)
        return 2

    all_issues: list[Issue] = []
    failed = 0
    for path in files:
        if not path.is_file():
            print(f"文件不存在：{path}", file=sys.stderr)
            failed += 1
            continue
        try:
            text = read_text_any(path)
        except (OSError, ValueError) as exc:
            print(f"无法读取 {path}：{exc}", file=sys.stderr)
            failed += 1
            continue
        all_issues.extend(check_text(text, str(path)))

    errors = sum(1 for x in all_issues if x.level == "ERROR")
    warnings = len(all_issues) - errors

    if args.json:
        print(json.dumps(
            {
                "files": len(files) - failed,
                "errors": errors,
                "warnings": warnings,
                "issues": [asdict(x) for x in all_issues],
            },
            ensure_ascii=False,
            indent=2,
        ))
    elif args.stats:
        print(f"文件 {len(files) - failed}  错误 {errors}  警告 {warnings}")
    else:
        for issue in all_issues[: args.max_print]:
            loc = f"{issue.file}:{issue.line}"
            extra = f" | {issue.snippet}" if issue.snippet else ""
            print(f"{loc} [{issue.level}] {issue.code} {issue.message}{extra}")
        if len(all_issues) > args.max_print:
            print(f"… 其余 {len(all_issues) - args.max_print} 条省略")
        print(f"\n文件 {len(files) - failed}  错误 {errors}  警告 {warnings}")

    return 1 if (errors or failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
