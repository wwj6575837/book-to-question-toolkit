#!/usr/bin/env python3
"""安装前后自检：仓库缺哪些脚本/参数、该用哪条命令。

用法（在仓库根，或加 --repo）：
    python .claude/skills/book-to-question/scripts/preflight.py
    python .../preflight.py --emit detail
    python .../preflight.py --emit compact --json

退出码：0 一切就绪 / 2 有阻断项（缺脚本、缺依赖、--help 起不来）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

# 每个模块支持的参数 -> 拼命令时按探测结果过滤
DETAIL_BASE = [
    "--ids", "<id>", "--auto-chunk", "--max-workers", "6", "--chapter-max-workers", "7",
    "--resume-partial", "--skip-merge-unchanged",
    "--limit-deepseek", "15", "--limit-doubao", "15", "--limit-merge", "8",
    "--retries", "2", "--retry-backoff", "10",
]
COMPACT_BASE = DETAIL_BASE + ["--split-by-section"]

PROMPTS_REQUIRED = [
    "md/知识点总结提示词V5.1.md",
    "md/精简版提示词1.md",
    "md/标题层级排序.md",
    "multi_agent/prompts/deepseek_agent.md",
    "multi_agent/prompts/doubao_agent.md",
    "multi_agent/prompts/llm_merge_agent.md",
    "multi_agent/prompts/kimi_agent.md",
]

FAIL, WARN, OK = "FAIL", "WARN", "OK"


def default_repo() -> Path:
    cwd = Path.cwd()
    if (cwd / "multi_agent").is_dir() or (cwd / ".git").exists():
        return cwd
    for parent in SKILL_DIR.parents:
        if (parent / ".git").exists() or (parent / "multi_agent").is_dir():
            return parent
    return cwd


def pick_python(repo: Path, override: str | None) -> str:
    if override:
        return override
    win = repo / ".venv" / "Scripts" / "python.exe"
    posix = repo / ".venv" / "bin" / "python"
    if win.exists():
        return str(win)
    if posix.exists():
        return str(posix)
    return sys.executable


def probe(py: str, repo: Path, module: str) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        proc = subprocess.run(
            [py, "-m", module, "--help"],
            cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"module": module, "ok": False, "error": str(exc), "flags": set()}
    out = (proc.stdout or "") + (proc.stderr or "")
    flags = set(re.findall(r"--[a-z][a-z0-9-]+", out))
    ok = proc.returncode == 0
    err = "" if ok else (proc.stderr or proc.stdout or "").strip().splitlines()[:3]
    return {"module": module, "ok": ok, "error": err, "flags": flags}


def build_cmd(py: str, module: str, tokens: list[str], flags: set[str]) -> str:
    out, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            if tok in flags:
                out.append(tok)
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    out.append(tokens[i + 1])
                    i += 1
            # 不支持的 flag 连同其后值一起丢掉
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                i += 1
        else:
            out.append(tok)
        i += 1
    return f'"{py}" -m {module} ' + " ".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查目标仓库是否具备 skill 所需脚本与参数")
    parser.add_argument("--repo", type=Path, default=None, help="仓库根目录（默认自动识别）")
    parser.add_argument("--python", default=None, help="指定解释器（默认仓库 .venv）")
    parser.add_argument("--emit", choices=["detail", "compact"], default=None,
                        help="只输出该阶段可直接粘贴的命令")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    parser.add_argument("--check", action="store_true", help="显式执行检查（默认行为）")
    args = parser.parse_args(argv)

    repo = (args.repo or default_repo()).expanduser().resolve()
    checks: list[dict] = []

    def add(name: str, status: str, detail: str = "", hint: str = ""):
        checks.append({"name": name, "status": status, "detail": detail, "hint": hint})

    add("仓库根", OK if (repo / "multi_agent").is_dir() else FAIL, str(repo),
        "加 --repo 指向仓库根" if not (repo / "multi_agent").is_dir() else "")
    for rel in ("multi_agent/chapter_pipeline.py", "multi_agent/chapter_pipeline1.py",
                "补题/命题官批量生成.py"):
        p = repo / rel
        add(rel, OK if p.is_file() else FAIL, "", "跑 scripts/install.py 从 zip 补齐")
    for rel in PROMPTS_REQUIRED:
        p = repo / rel
        add(rel, OK if p.is_file() else WARN, "", "缺提示词时流水线读不到；跑 install.py 补齐")

    py = pick_python(repo, args.python)
    add("解释器", OK if Path(py).exists() or py == sys.executable else FAIL, py,
        "仓库缺 .venv：参考 references/install.md 建虚拟环境")

    detail = probe(py, repo, "multi_agent.chapter_pipeline")
    compact = probe(py, repo, "multi_agent.chapter_pipeline1")
    for probe_res in (detail, compact):
        if probe_res["ok"]:
            add(f"{probe_res['module']} --help", OK, "")
        else:
            add(f"{probe_res['module']} --help", FAIL, str(probe_res["error"])[:300],
                "多为缺依赖或 prompts；先跑 install.py，再按报错装包")

    if detail["ok"]:
        add("chapter_pipeline 支持 --limit-merge", OK if "--limit-merge" in detail["flags"] else WARN,
            "", "旧版流水线：跑 scripts/install.py 覆盖 multi_agent/")
    if compact["ok"]:
        if "--split-by-section" not in compact["flags"]:
            add("chapter_pipeline1 支持 --split-by-section", WARN, "",
                "旧版流水线：跑 scripts/install.py 覆盖 multi_agent/")
        if "--limit-merge" not in compact["flags"]:
            add("chapter_pipeline1 支持 --limit-merge", WARN, "",
                "旧版流水线：跑 scripts/install.py 覆盖 multi_agent/")

    failed = [c for c in checks if c["status"] == FAIL]

    if args.emit:
        module = "multi_agent.chapter_pipeline" if args.emit == "detail" else "multi_agent.chapter_pipeline1"
        tokens = DETAIL_BASE if args.emit == "detail" else COMPACT_BASE
        res = detail if args.emit == "detail" else compact
        if not res["ok"]:
            print(f"[错误] {module} --help 起不来，先修环境：{res['error']}")
            return 2
        print(build_cmd(py, module, tokens, res["flags"]))
        return 0

    if args.json:
        print(json.dumps({"repo": str(repo), "python": py, "checks": checks,
                          "ok": not failed}, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            line = f"[{c['status']}] {c['name']}"
            if c["detail"]:
                line += f" -> {c['detail']}"
            print(line)
            if c["hint"] and c["status"] != OK:
                print(f"       {c['hint']}")
        print("")
        print("结论：" + ("就绪" if not failed else f"{len(failed)} 项阻断，按上面的提示处理"))

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
