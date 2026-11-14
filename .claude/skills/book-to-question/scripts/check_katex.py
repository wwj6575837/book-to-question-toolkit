#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实 KaTeX 试渲染公式，检查开头的详细版和上题产物。

用法：
    python check_katex.py --file "exports/.../2552-1 ....md"
    python check_katex.py --dir "exports/.../书籍库"
    python check_katex.py --questions-dir "补题/题目/2552xxx"

依赖 node 与 KaTeX：优先读 --katex-path / KATEX_PATH，再找 node_modules、
Yarn/npm 缓存；找不到时打印提示并以 2 退出（可用 --require 改成失败）。
只读，不修改任何文件。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mdscan import read_text_any

REPO_ROOT = Path(__file__).resolve().parents[4]
JS = Path(__file__).with_name("katex_render.js")
FENCE = re.compile(r"(```|~~~).*?\1", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
SKIP_JSON = {"all.json", "config.json", "progress.json"}


class KatexUnavailable(RuntimeError):
    """本机没有 node 或 KaTeX。"""


@dataclass
class Snippet:
    source: str
    expr: str
    display: bool
    offset: int = 0
    path: Path | None = None
    field: str = ""
    kind: str = ""  # inline / block / paren / bracket
    qindex: int = 0  # 题目在章节 JSON 里的序号（1 起）


def find_node(explicit: str | None = None) -> str | None:
    for cand in (explicit, os.environ.get("NODE_BIN"), shutil.which("node")):
        if cand and Path(cand).exists():
            return cand
    for pattern in ("~/.nvm/versions/node/*/bin/node", "/opt/homebrew/bin/node",
                    "/usr/local/bin/node", "/usr/bin/node"):
        hits = sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
        if hits:
            return hits[0]
    return None


def _is_katex_dir(path) -> bool:
    root = Path(path)
    return (root / "package.json").is_file() and (root / "dist" / "katex.js").is_file()


def find_katex(node: str | None, explicit: str | None = None) -> Path | None:
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("KATEX_PATH"):
        cands.append(os.environ["KATEX_PATH"])
    for root in (Path.cwd(), REPO_ROOT, Path(__file__).resolve().parents[1]):
        cands.append(str(root / "node_modules" / "katex"))
        cands.append(str(root / "补题" / "练习册生成" / "node_modules" / "katex"))
    for pattern in ("~/Library/Caches/Yarn/v6/npm-katex-*/node_modules/katex",
                    "~/.cache/yarn/v6/npm-katex-*/node_modules/katex",
                    "~/.npm/_npx/*/node_modules/katex"):
        cands.extend(sorted(glob.glob(os.path.expanduser(pattern)), reverse=True))
    for cand in cands:
        if cand and _is_katex_dir(cand):
            return Path(cand)
    if node:
        try:
            proc = subprocess.run(
                [node, "-e", "console.log(require.resolve('katex'))"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc and proc.returncode == 0:
            resolved = Path(proc.stdout.strip())
            for parent in resolved.parents:
                if _is_katex_dir(parent):
                    return parent
    return None


def _env(node: str | None, katex_path: str | None) -> tuple[str, Path]:
    node_bin = find_node(node)
    if not node_bin:
        raise KatexUnavailable("未找到 node（可设 NODE_BIN 或 --node）")
    katex = find_katex(node_bin, katex_path)
    if not katex:
        raise KatexUnavailable(
            "未找到 KaTeX（可设 KATEX_PATH/--katex-path，或在仓库根执行 npm i katex）"
        )
    return node_bin, katex


def extract_math(text: str) -> list[tuple[str, bool, int, str]]:
    """返回 (公式体, 是否块级, 起始偏移, 定界符类型)。"""
    text = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = INLINE_CODE.sub("", text)
    out: list[tuple[str, bool, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\\":
            matched = False
            for op, cl, display, kind in (("\\(", "\\)", False, "paren"),
                                          ("\\[", "\\]", True, "bracket")):
                if text.startswith(op, i):
                    j = text.find(cl, i + 2)
                    if j < 0:
                        j = n - 2
                    out.append((text[i + 2 : j], display, i, kind))
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
                break
            out.append((text[i + 2 : j], True, i, "block"))
            i = j + 2
            continue
        if text[i] == "$":
            j = text.find("$", i + 1)
            nl = text.find("\n", i + 1)
            if j >= 0 and (nl < 0 or j < nl):
                out.append((text[i + 1 : j], False, i, "inline"))
                i = j + 1
                continue
        i += 1
    return out


def _option_texts(options) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(options, list):
        return out
    for i, opt in enumerate(options):
        if isinstance(opt, dict):
            key = str(opt.get("key") or opt.get("letter") or opt.get("label") or chr(65 + i))
            name = opt.get("name")
            if name is None:
                name = opt.get("text") or opt.get("value") or ""
            out[key] = str(name)
        else:
            out[chr(65 + i)] = str(opt)
    return out


def collect_markdown(paths: list[Path]) -> list[Snippet]:
    items: list[Snippet] = []
    for path in paths:
        text = read_text_any(path)
        for expr, display, offset, kind in extract_math(text):
            line = text.count("\n", 0, offset) + 1
            items.append(Snippet(f"{path.name}:{line}", expr, display, offset,
                                 path=path, kind=kind))
    return items


def collect_questions(qdir: Path, include_all: bool = False) -> list[Snippet]:
    items: list[Snippet] = []
    skip = SKIP_JSON - {"all.json"} if include_all else SKIP_JSON
    files = sorted(
        p for p in qdir.glob("*.json")
        if p.name not in skip and not p.name.startswith("_")
    )
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        for idx, q in enumerate(data, 1):
            if not isinstance(q, dict):
                continue
            fields: list[tuple[str, str]] = [
                ("title", str(q.get("title") or "")),
                ("parse", str(q.get("parse") or "")),
            ]
            for key, text in _option_texts(q.get("options")).items():
                fields.append((f"选项{key}", text))
            subs = q.get("subs")
            if isinstance(subs, list):
                for si, sub in enumerate(subs, 1):
                    if not isinstance(sub, dict):
                        continue
                    fields.append((f"子题{si} title", str(sub.get("title") or "")))
                    fields.append((f"子题{si} parse", str(sub.get("parse") or "")))
                    for key, text in _option_texts(sub.get("options")).items():
                        fields.append((f"子题{si} 选项{key}", text))
            for name, text in fields:
                for expr, display, offset, kind in extract_math(text):
                    items.append(Snippet(f"{path.stem} #{idx} {name}", expr, display,
                                         offset, path=path, field=name, kind=kind,
                                         qindex=idx))
    return items


def render_details(items: list[Snippet], node: str | None = None,
                   katex_path: str | None = None) -> tuple[list[dict], str]:
    """返回 ([{index, status, message}], 说明)。"""
    if not items:
        return [], "没有找到公式"
    node_bin, katex = _env(node, katex_path)
    payload = json.dumps(
        [{"index": i, "expr": it.expr, "display": it.display} for i, it in enumerate(items)],
        ensure_ascii=False,
    )
    try:
        proc = subprocess.run(
            [node_bin, str(JS), str(katex)],
            input=payload, capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KatexUnavailable(f"KaTeX 渲染进程启动失败：{exc}") from exc
    if proc.returncode != 0:
        raise KatexUnavailable(f"KaTeX 渲染失败：{proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout), f"KaTeX 渲染 {len(items)} 条公式"


def render(items: list[Snippet], node: str | None = None,
           katex_path: str | None = None) -> tuple[list[str], list[str], str]:
    results, note = render_details(items, node, katex_path)
    errors: list[str] = []
    warns: list[str] = []
    for res in results:
        item = items[res["index"]]
        target = errors if res["status"] == "error" else warns
        kind = "解析失败" if res["status"] == "error" else "警告"
        target.append(f"{item.source} KaTeX {kind}：{item.expr[:70]}（{res['message']}）")
    return errors, warns, note


def verify_expr(expr: str, display: bool = False, node: str | None = None,
                katex_path: str | None = None) -> tuple[str, str]:
    """渲染单条公式，返回 (status, message)。"""
    results, _ = render_details([Snippet("<verify>", expr, display)], node, katex_path)
    if not results:
        return "ok", ""
    return str(results[0]["status"]), str(results[0]["message"])


def check_markdown(paths: list[Path], node: str | None = None,
                   katex_path: str | None = None) -> tuple[list[str], list[str], str]:
    return render(collect_markdown(paths), node, katex_path)


def check_questions(qdir: Path, node: str | None = None,
                    katex_path: str | None = None) -> tuple[list[str], list[str], str]:
    return render(collect_questions(qdir), node, katex_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="用 KaTeX 试渲染公式（只读）")
    p.add_argument("--file", action="append", default=[], help="Markdown 文件，可重复")
    p.add_argument("--dir", help="Markdown 目录")
    p.add_argument("--questions-dir", help="章节 JSON 目录")
    p.add_argument("--node", help="node 可执行文件路径")
    p.add_argument("--katex-path", help="KaTeX 包目录")
    p.add_argument("--strict", action="store_true", help="警告也判失败")
    p.add_argument("--require", action="store_true", help="找不到 node/KaTeX 时判失败")
    p.add_argument("--max-report", type=int, default=50, help="最多打印多少条问题")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args(argv)

    files = [Path(f) for f in args.file]
    if args.dir:
        files += sorted(Path(args.dir).glob("*.md"))
    if not files and not args.questions_dir:
        p.error("需要 --file / --dir / --questions-dir 之一")

    try:
        if args.questions_dir:
            errors, warns, note = check_questions(
                Path(args.questions_dir), args.node, args.katex_path
            )
        else:
            errors, warns, note = check_markdown(files, args.node, args.katex_path)
    except KatexUnavailable as exc:
        if args.require:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        print(f"⚠ 跳过 KaTeX 渲染检查：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(
            {"note": note, "errors": errors, "warnings": warns},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(note)
        for title, rows in (("错误", errors), ("警告", warns)):
            if not rows:
                continue
            print(f"\n{title} {len(rows)} 条：")
            for row in rows[: args.max_report]:
                print(f"  {'✗' if title == '错误' else '⚠'} {row}")
            if len(rows) > args.max_report:
                print(f"  … 其余 {len(rows) - args.max_report} 条省略")
        if not errors and not warns:
            print("通过。")
    if errors:
        return 1
    if warns and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
