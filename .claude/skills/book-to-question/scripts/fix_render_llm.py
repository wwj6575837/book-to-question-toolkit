#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KaTeX 解析错误的大模型兜底：逐条修公式，修完必须重新通过 KaTeX。

用法：
    python fix_render_llm.py --file "exports/.../<id>-1 <书名>.md"          # dry-run
    python fix_render_llm.py --file "..." --apply
    python fix_render_llm.py --questions-dir "补题/题目/<id><书名>" --apply

只处理 KaTeX 解析失败（status=error），警告交给 fix_render.py。每条公式单独调用，
按 (公式, 报错) 去重；默认最多 20 条。写盘前备份为 <文件名>.bak-<时间戳>。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import check_katex
from mdscan import read_text_any

REPO_ROOT = Path(__file__).resolve().parents[4]
OPEN = {"inline": "$", "block": "$$", "paren": "\\(", "bracket": "\\["}
CLOSE = {"inline": "$", "block": "$$", "paren": "\\)", "bracket": "\\]"}

SYSTEM = (
    "你是 LaTeX 公式修错助手。只修给定的一条公式，不解释、不扩写，保持数学含义、"
    "变量名和原有风格。优先最小改动：补全缺失的花括号/参数、修正明显的转义错误。"
    "不要为了绕过报错引入新的环境（如 aligned/cases），除非原式本来就有对齐结构；"
    "孤立的 & 用 \\& 或直接删除。未定义命令只有在能确定是常见命令拼写错误时才替换，"
    "否则输出 __UNFIXABLE__。只输出修正后的公式本体，不要 $、$$、\\(...\\)、"
    "\\[...\\] 定界符，不要 Markdown 代码块。如果公式明显是更长公式的片段"
    "（如以裸 { 开头、或无端以 } 结尾），输出 __UNFIXABLE__，不要补全成独立公式；"
    "但 \\sqrt{、\\text{、\\left( 等命令缺闭合括号属于普通括号错误，正常补全。"
    "如果必须猜测缺失的数值/条件才能修好，输出 __UNFIXABLE__，不要编造内容。"
)


def env_value(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    for path in (REPO_ROOT / ".env", Path.cwd() / ".env",
                 Path(__file__).resolve().parents[1] / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def clean_output(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^```(?:latex|tex)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"^(?:修正后|修正结果|正确公式|公式)\s*[：:]\s*", "", s)
    for op, cl in (("$$", "$$"), ("$", "$"), ("\\(", "\\)"), ("\\[", "\\]")):
        if s.startswith(op) and s.endswith(cl) and len(s) > len(op) + len(cl):
            s = s[len(op): len(s) - len(cl)]
            break
    return s.strip().rstrip("。；;，")


def call_llm(client, model: str, expr: str, message: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"公式：\n{expr}\n\nKaTeX 报错：\n{message}"},
        ],
        temperature=0,
        max_tokens=2048,
    )
    return resp.choices[0].message.content or ""


BRACE_COMMANDS = re.compile(
    r"\\(?:sqrt|text|mathrm|mathbf|mathit|frac|dfrac|tfrac|overline|underline|hat|bar|vec|begin|operatorname)\s*$"
)


def looks_like_fragment(expr: str) -> bool:
    """明显是更长公式的一半（如 A={x、x>2}）时不要交给模型补全。"""
    s = expr.strip()
    if s.startswith("{") and not s.startswith("\\"):
        return True
    opens, closes = s.count("{"), s.count("}")
    if closes > opens:
        return True
    if opens > closes:
        depth = 0
        unmatched = []
        for i, ch in enumerate(s):
            if ch == "{":
                if depth == 0:
                    unmatched.append(i)
                depth += 1
            elif ch == "}":
                depth -= 1
        for pos in unmatched:
            if not BRACE_COMMANDS.search(s[:pos].rstrip()):
                return True
    return False


def repair(client, model: str, expr: str, message: str, display: bool,
           node: str | None, katex_path: str | None) -> tuple[str | None, str]:
    """返回 (修正后的公式, 最后一次报错)；失败时公式为 None。"""
    if looks_like_fragment(expr):
        return None, "公式是更长公式的片段，跳过自动补全"
    current_error = message
    for _ in range(2):
        try:
            out = clean_output(call_llm(client, model, expr, current_error))
        except Exception as exc:  # 网络/限流
            return None, f"{type(exc).__name__}: {exc}"
        if "UNFIXABLE" in out.upper():
            return None, "模型判定无法在不猜测的情况下修复"
        if not out or out == expr:
            current_error = message + "\n上一次没有给出实际修改，请真正修正。"
            continue
        status, msg = check_katex.verify_expr(out, display, node, katex_path)
        if status != "error":
            return out, msg if status == "warn" else ""
        current_error = f"{message}\n上一次输出仍然报错：{msg}"
    return None, current_error


def collect_targets(files: list[Path], qdir: Path | None,
                    node: str | None, katex_path: str | None):
    items = (check_katex.collect_questions(qdir, include_all=True)
             if qdir else check_katex.collect_markdown(files))
    results, note = check_katex.render_details(items, node, katex_path)
    errors = [(items[res["index"]], str(res["message"]))
              for res in results if res["status"] == "error"]
    return errors, note


def _field_container(q: dict, field: str):
    if field in ("title", "parse"):
        return q, field
    m = re.fullmatch(r"子题(\d+) (title|parse)", field)
    if m:
        subs = q.get("subs") or []
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(subs) and isinstance(subs[idx], dict):
            return subs[idx], m.group(2)
        return None, None
    m = re.fullmatch(r"选项(\w+)", field)
    if m:
        for opt in q.get("options") or []:
            if isinstance(opt, dict) and str(opt.get("key")) == m.group(1):
                return opt, "name"
        return None, None
    m = re.fullmatch(r"子题(\d+) 选项(\w+)", field)
    if m:
        subs = q.get("subs") or []
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(subs) and isinstance(subs[idx], dict):
            for opt in subs[idx].get("options") or []:
                if isinstance(opt, dict) and str(opt.get("key")) == m.group(2):
                    return opt, "name"
    return None, None


def apply_markdown(fixes: list[tuple[check_katex.Snippet, str]], stamp: str) -> int:
    by_path: dict[Path, list[tuple[check_katex.Snippet, str]]] = {}
    for item, expr in fixes:
        if item.path:
            by_path.setdefault(item.path, []).append((item, expr))
    done = 0
    for path, rows in by_path.items():
        text = read_text_any(path)
        changed = False
        for item, expr in sorted(rows, key=lambda x: x[0].offset, reverse=True):
            op, cl = OPEN[item.kind], CLOSE[item.kind]
            old = op + item.expr + cl
            if text[item.offset: item.offset + len(old)] != old:
                print(f"  跳过 {item.source}：源文本已变化")
                continue
            text = text[: item.offset] + op + expr + cl + text[item.offset + len(old):]
            changed = True
            done += 1
        if changed:
            shutil.copy2(path, path.with_name(path.name + f".bak-{stamp}"))
            path.write_text(text, encoding="utf-8")
            print(f"  写入 {path.name}")
    return done


def apply_questions(fixes: list[tuple[check_katex.Snippet, str]], stamp: str) -> int:
    by_path: dict[Path, list[tuple[check_katex.Snippet, str]]] = {}
    for item, expr in fixes:
        if item.path:
            by_path.setdefault(item.path, []).append((item, expr))
    done = 0
    for path, rows in by_path.items():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            print(f"  跳过 {path.name}：JSON 读取失败")
            continue
        if not isinstance(data, list):
            continue
        changed = False
        for item, expr in sorted(rows, key=lambda x: x[0].offset, reverse=True):
            if not (1 <= item.qindex <= len(data)) or not isinstance(data[item.qindex - 1], dict):
                print(f"  跳过 {item.source}：题号越界")
                continue
            container, key = _field_container(data[item.qindex - 1], item.field)
            if container is None:
                print(f"  跳过 {item.source}：找不到字段")
                continue
            text = str(container.get(key) or "")
            op, cl = OPEN[item.kind], CLOSE[item.kind]
            old = op + item.expr + cl
            if text[item.offset: item.offset + len(old)] != old:
                print(f"  跳过 {item.source}：字段文本已变化")
                continue
            container[key] = text[: item.offset] + op + expr + cl + text[item.offset + len(old):]
            changed = True
            done += 1
        if changed:
            shutil.copy2(path, path.with_name(path.name + f".bak-{stamp}"))
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            print(f"  写入 {path.name}")
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="KaTeX 解析错误的 LLM 兜底（默认 dry-run）")
    p.add_argument("--file", action="append", default=[], help="Markdown 文件，可重复")
    p.add_argument("--dir", help="Markdown 目录")
    p.add_argument("--questions-dir", help="章节 JSON 目录")
    p.add_argument("--apply", action="store_true", help="写盘（默认只试算）")
    p.add_argument("--max-items", type=int, default=20, help="最多处理多少个不同错误")
    p.add_argument("--model", default=None, help="默认读 DEEPSEEK_MODEL，否则 deepseek-v4-flash")
    p.add_argument("--node", help="node 路径")
    p.add_argument("--katex-path", help="KaTeX 包目录")
    args = p.parse_args(argv)

    files = [Path(f) for f in args.file]
    if args.dir:
        files += sorted(Path(args.dir).glob("*.md"))
    if not files and not args.questions_dir:
        p.error("需要 --file / --dir / --questions-dir 之一")
    qdir = Path(args.questions_dir) if args.questions_dir else None

    try:
        errors, note = collect_targets(files, qdir, args.node, args.katex_path)
    except check_katex.KatexUnavailable as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    if not errors:
        print(f"{note}：没有解析错误")
        return 0

    groups: dict[tuple[str, bool, str], list[check_katex.Snippet]] = {}
    for item, msg in errors:
        groups.setdefault((item.expr, item.display, msg), []).append(item)
    keys = list(groups)[: args.max_items]
    print(f"{note}：{len(errors)} 条解析错误，{len(groups)} 个不同公式，本批处理 {len(keys)} 个")

    api_key = env_value("DEEPSEEK_API_KEY")
    if not api_key:
        print("✗ 未配置 DEEPSEEK_API_KEY（环境变量或仓库根 .env）", file=sys.stderr)
        return 2
    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=env_value("DEEPSEEK_API_BASE") or "https://api.deepseek.com",
    )
    model = args.model or env_value("DEEPSEEK_MODEL") or "deepseek-v4-flash"

    fixes: list[tuple[check_katex.Snippet, str]] = []
    failed = 0
    for expr, display, msg in keys:
        new_expr, last = repair(client, model, expr, msg, display, args.node, args.katex_path)
        if new_expr is None:
            failed += 1
            print(f"✗ 仍失败：{expr[:60]}（{last.splitlines()[0][:100]}）")
            continue
        for item in groups[(expr, display, msg)]:
            fixes.append((item, new_expr))
        print(f"✓ {expr[:60]}\n  → {new_expr[:90]}")

    if not fixes:
        print("没有可写入的修正。")
        return 1
    if not args.apply:
        print(f"\ndry-run，未写盘：可修 {len(fixes)} 处，失败 {failed} 个。加 --apply 写入。")
        return 1 if failed else 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    done = apply_questions(fixes, stamp) if qdir else apply_markdown(fixes, stamp)
    print(f"\n已写盘 {done} 处，备份后缀 .bak-{stamp}")

    try:
        left, note2 = collect_targets(files, qdir, args.node, args.katex_path)
    except check_katex.KatexUnavailable as exc:
        print(f"（跳过复检：{exc}）")
        return 1 if failed else 0
    print(f"复检：{note2}，剩余 {len(left)} 条解析错误")
    for item, msg in left[:10]:
        print(f"  ⚠ {item.source}：{msg.splitlines()[0][:100]}")
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(main())
