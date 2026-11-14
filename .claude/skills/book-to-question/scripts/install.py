#!/usr/bin/env python3
"""把 repo-addons/ 里的仓库侧脚本按差异安装进目标仓库。

规则：
  目标缺失 -> 新增
  内容不同 -> 先备份到 .book-to-question-backup/install-<时间戳>/ 再替换
  内容相同 -> 跳过

默认示例（skill 装在 <仓库>/.claude/skills/book-to-question 时，自动识别仓库根）：
    python .claude/skills/book-to-question/scripts/install.py --dry-run
    python .claude/skills/book-to-question/scripts/install.py

也可显式指定：--repo /path/to/仓库
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ADDONS_DIR = SKILL_DIR / "repo-addons"
BACKUP_ROOT = ".book-to-question-backup"
SKIP_DIR_NAMES = {"__pycache__", ".git", ".DS_Store", ".idea", ".vscode"}
SKIP_FILE_SUFFIX = (".pyc", ".pyo", ".bak", ".orig", ".rej")


def make_writable(path: Path) -> None:
    """去掉 macOS 的 uchg 等不可变标记，避免覆盖时 EPERM。"""
    try:
        os.chflags(path, 0)
    except (AttributeError, OSError):
        pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_repo() -> Path:
    # 优先当前目录，其次按 skill 位置向上找仓库根
    cwd = Path.cwd()
    if (cwd / "multi_agent").is_dir() or (cwd / ".git").exists():
        return cwd
    for parent in SKILL_DIR.parents:
        if (parent / ".git").exists() or (parent / "multi_agent").is_dir():
            return parent
    return SKILL_DIR.parents[2]


def iter_addons(root: Path):
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIR_NAMES)
        for name in sorted(files):
            if name in SKIP_DIR_NAMES or name.endswith(SKIP_FILE_SUFFIX):
                continue
            src = Path(cur) / name
            yield src.relative_to(root), src


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把 skill 附带的仓库脚本按差异安装/更新到仓库")
    parser.add_argument("--repo", type=Path, default=None, help="目标仓库根目录（默认自动识别）")
    parser.add_argument("--addons", type=Path, default=ADDONS_DIR, help="附带脚本目录（默认 skill 内 repo-addons/）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不改动任何文件")
    parser.add_argument("--with-env", action="store_true", help="目标缺 .env/config.json 时从包内 deps/（带密钥版）或 .env.example 生成")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = parser.parse_args(argv)

    repo = (args.repo or default_repo()).expanduser().resolve()
    addons = args.addons.expanduser().resolve()

    if not addons.is_dir():
        print(f"[错误] 找不到附带脚本目录：{addons}")
        return 2
    if not repo.is_dir():
        print(f"[错误] 仓库目录不存在：{repo}")
        return 2
    if not (repo / ".venv").exists() and not (repo / "multi_agent").is_dir():
        print(f"[警告] {repo} 看起来不像本仓库根（没有 .venv 和 multi_agent/），仍按给定路径继续")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = repo / BACKUP_ROOT / f"install-{stamp}"
    added, replaced, same, errors = [], [], [], []

    for rel, src in iter_addons(addons):
        dst = repo / rel
        try:
            if not dst.exists():
                if not args.dry_run:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                added.append(rel)
                line = f"ADD     {rel}"
            elif dst.is_file() and sha256(dst) == sha256(src):
                same.append(rel)
                line = f"SAME    {rel}"
            else:
                backup = backup_dir / rel
                if not args.dry_run:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if dst.is_file():
                        make_writable(dst)
                        shutil.copy2(dst, backup)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                replaced.append(rel)
                line = f"REPLACE {rel}  (备份: {backup.relative_to(repo)})"
            if not args.quiet:
                print(("[dry] " if args.dry_run else "") + line)
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
            print(f"ERROR   {rel}: {exc}")

    if args.with_env:
        secrets_dir = SKILL_DIR / "deps"
        for name in (".env", "config.json"):
            dst = repo / name
            if dst.exists():
                print(f"SKIP    {name}（已存在，不覆盖）")
                continue
            src = secrets_dir / name
            if src.is_file():
                if not args.dry_run:
                    shutil.copy2(src, dst)
                    os.chmod(dst, 0o600)
                added.append(Path(name))
                print(("ADD     " if not args.dry_run else "[dry] ADD ") + f"{name}（来自包内 deps/，含密钥，权限 600）")
            elif name == ".env" and (repo / ".env.example").is_file():
                if not args.dry_run:
                    shutil.copy2(repo / ".env.example", dst)
                added.append(Path(".env"))
                print(("ADD     " if not args.dry_run else "[dry] ADD ") + ".env（从 .env.example 生成，记得填 key）")

    print("")
    print(f"仓库: {repo}")
    print(f"附带: {addons}")
    print(f"新增 {len(added)} / 替换 {len(replaced)} / 跳过 {len(same)} / 失败 {len(errors)}")
    if replaced and not args.dry_run:
        print(f"被替换文件的原件已备份到: {backup_dir.relative_to(repo)}")
    if errors:
        return 1
    if args.dry_run:
        print("（dry-run：未写入任何文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
