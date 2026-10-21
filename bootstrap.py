#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""book-to-question-toolkit 自举脚本。

用法：
    python bootstrap.py                         # 在本仓库直接使用
    python bootstrap.py --install-into <项目>   # 安装到现有项目
    python bootstrap.py --install-into <项目> --from-github   # 只从 GitHub 拉缺失/变更
    python bootstrap.py --check                 # 只检查，不写文件

覆盖规则：
- manifest.json 里的受管文件：缺失补、内容不同备份后覆盖、相同跳过；
- .env / config.json / book.json / exports/ 等用户文件永远不覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"
SKILL_BOOTSTRAP = Path(".claude/skills/book-to-question/scripts/bootstrap.py")
DEFAULT_REPO = "wwj6575837/book-to-question-toolkit"


def load_manifest() -> list[str]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = data.get("files", [])
    if not isinstance(files, list):
        raise SystemExit("manifest.json 格式错误：files 不是数组")
    return [str(x) for x in files]


def _fetch_once(url: str) -> bytes:
    try:
        import requests  # 已装依赖时优先用 requests，证书处理更稳
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.content
    except ImportError:
        import ssl
        try:
            import certifi  # requests 的依赖；未装 requests 时可能也没有
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "book-to-question-toolkit"})
        with urlopen(req, timeout=30, context=ctx) as resp:
            if getattr(resp, "status", 200) != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return resp.read()


def fetch_github(repo: str, ref: str, rel: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{quote(rel, safe='/')}"
    last: Exception | None = None
    for attempt in range(3):
        try:
            data = _fetch_once(url)
            if data:
                return data
            last = RuntimeError("下载为空")
        except Exception as e:  # 瞬时 SSL/EOF/5xx 都重试
            last = e
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"下载失败 {rel}: {last}") from last


def atomic_write(target: Path, data: bytes, mode: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".b2q-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
        try:
            os.chmod(target, mode if mode is not None else 0o644)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


GITIGNORE_LINES = [
    ".env", "config.json", ".venv/", "exports/", "拆书/",
    "补题/详细版/", "补题/拆分后/", "补题/题目/", "补题/配置/",
    ".book-to-question-backup/",
]


def merge_gitignore(target: Path, update: bool) -> list[str]:
    path = target / ".gitignore"
    existing = set()
    if path.exists():
        existing = {
            x.strip() for x in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        }
    missing = [x for x in GITIGNORE_LINES if x not in existing]
    if missing and update:
        prefix = "" if not path.exists() or not path.read_text(
            encoding="utf-8", errors="ignore"
        ).endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + "# >>> book-to-question >>>\n")
            fh.write("\n".join(missing) + "\n")
            fh.write("# <<< book-to-question <<<\n")
    return missing


def backup_file(target: Path, rel: str, backup_root: Path) -> None:
    dst = backup_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dst)


def venv_python(target: Path) -> Path:
    return target / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def run_check_skill(target: Path, py: Path, *, check: bool = True) -> int:
    script = target / SKILL_BOOTSTRAP
    if not script.exists():
        print(f"⚠ 找不到 skill 自检脚本：{script}")
        return 1
    cmd = [str(py), str(script)] + (["--check"] if check else [])
    print(f"\n$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(target))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="book-to-question-toolkit 自举/安装")
    p.add_argument("--install-into", help="目标项目根目录（默认当前仓库）")
    p.add_argument("--from-github", action="store_true", help="只从 GitHub 拉取，不读本地副本")
    p.add_argument("--source-repo", default=DEFAULT_REPO, help="GitHub repo（owner/name）")
    p.add_argument("--ref", default="main", help="分支/标签（默认 main）")
    p.add_argument("--check", action="store_true", help="只检查，不写文件/不建 venv")
    p.add_argument("--no-overwrite", action="store_true", help="只补缺失，不覆盖内容不同的受管文件")
    p.add_argument("--no-install", action="store_true", help="不执行 pip install")
    p.add_argument("--no-venv", action="store_true", help="不创建 .venv")
    p.add_argument("--create", action="store_true", help="目标目录不存在时允许新建")
    p.add_argument("--install-deps", action="store_true",
                   help="安装到现有项目时也执行 pip install（默认不做）")
    p.add_argument("--venv", help="指定独立的 venv 路径（默认 <目标>/.venv）")
    p.add_argument("--update-gitignore", action="store_true",
                   help="把密钥/产物忽略规则追加到目标 .gitignore")
    p.add_argument("--skip-skill-files", action="store_true",
                   help="不处理 .claude/skills/ 下的文件（主仓库模式）")
    args = p.parse_args(argv)

    if sys.version_info < (3, 12):
        print(f"需要 Python ≥3.12，当前 {sys.version.split()[0]}", file=sys.stderr)
        return 2

    target = Path(args.install_into).expanduser().resolve() if args.install_into else ROOT
    is_self = target == ROOT
    if not target.exists():
        if args.check:
            print(f"目标目录不存在：{target}", file=sys.stderr)
            return 2
        if args.install_into and not args.create:
            print(f"目标目录不存在：{target}\n确认要新建请加 --create。", file=sys.stderr)
            return 2
        target.mkdir(parents=True, exist_ok=True)

    try:
        files = load_manifest()
    except (OSError, json.JSONDecodeError) as e:
        print(f"读取 manifest.json 失败：{e}", file=sys.stderr)
        return 2

    same: list[str] = []
    missing: list[str] = []
    different: list[str] = []
    overwritten: list[str] = []
    unavailable: list[str] = []
    errors: list[str] = []
    stamp = time.strftime("%Y%m%d-%H%M%S-%f")
    backup_root = target / ".book-to-question-backup" / stamp

    for rel in files:
        dst = target / rel
        if args.skip_skill_files and rel.startswith(".claude/skills/"):
            if dst.exists():
                same.append(rel)
            else:
                unavailable.append(rel)
            continue
        try:
            if args.from_github:
                data = fetch_github(args.source_repo, args.ref, rel)
                mode = 0o755 if rel.endswith(".py") else 0o644
            else:
                src = ROOT / rel
                data = src.read_bytes()
                mode = src.stat().st_mode & 0o777
        except Exception as e:
            errors.append(f"{rel}: {e}")
            continue

        if not dst.exists():
            missing.append(rel)
            if not args.check:
                atomic_write(dst, data, mode)
            continue

        if dst.read_bytes() == data:
            same.append(rel)
            continue

        different.append(rel)
        if args.check or args.no_overwrite:
            continue
        backup_file(dst, rel, backup_root)
        atomic_write(dst, data, mode)
        overwritten.append(rel)

    venv_dir = Path(args.venv).expanduser().resolve() if args.venv else target / ".venv"
    py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    want_deps = is_self or args.install_deps
    if not args.check and not args.no_venv and want_deps:
        if not py.exists():
            print(f"\n创建 venv：{venv_dir}")
            rc = subprocess.call([sys.executable, "-m", "venv", str(venv_dir)])
            if rc != 0:
                errors.append("创建 .venv 失败")
        req = ROOT / "requirements.txt"
        if args.no_install:
            print(f"\n（--no-install）安装命令：{py} -m pip install -r {req}")
        elif errors:
            print("（有错误，跳过 pip install）")
        else:
            print(f"\n安装依赖：{py} -m pip install -r {req}")
            rc = subprocess.call([str(py), "-m", "pip", "install", "-r", str(req)])
            if rc != 0:
                errors.append("pip install 失败")
    elif args.install_into and not is_self:
        print("\n（安装到现有项目）默认不创建 venv、不装依赖；需要时加 --install-deps 或 --venv。")

    # 配置模板：用户文件只在缺失时生成
    if not args.check:
        for example, name in [("config.example.json", "config.json"), (".env.example", ".env")]:
            src, dst = ROOT / example, target / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                print(f"已生成模板：{name}（请填入真实值）")

    print("\n===== 同步结果 =====")
    print(f"目标：{target}")
    for label, items in [
        ("相同/跳过", same),
        ("缺失已补", missing),
        ("内容不同", different),
        ("已备份并覆盖", overwritten),
        ("主仓库无此文件", unavailable),
    ]:
        print(f"{label}: {len(items)}")
        for x in items[:8]:
            print(f"  - {x}")
        if len(items) > 8:
            print(f"  … 其余 {len(items) - 8} 个")
    if overwritten:
        print(f"备份目录：{backup_root}")
    if errors:
        print(f"\n错误 {len(errors)} 条：")
        for e in errors[:12]:
            print(f"  ✗ {e}")

    if args.check:
        py = venv_python(target) if (target / ".venv").exists() else Path(sys.executable)
        rc = run_check_skill(target, py)
        bad = len(missing) + len(different) + len(unavailable) + len(errors)
        return 1 if (bad or rc != 0) else 0

    py = venv_python(target) if not args.no_venv else Path(sys.executable)
    if py.exists():
        run_check_skill(target, py, check=False)
    else:
        print(f"\n未找到 venv python：{py}，跳过 skill 自检")

    print("\n下一步：")
    print(f"  1. 在 {target} 填好 .env 和 config.json")
    print("  2. 在仓库根打开 Claude Code，说「拆书」或查看 .claude/skills/book-to-question/SKILL.md")
    return 1 if (errors or unavailable) else 0


if __name__ == "__main__":
    raise SystemExit(main())
