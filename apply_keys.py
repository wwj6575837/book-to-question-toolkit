#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从私密密钥 Markdown 写入 .env 和 config.json。

设计目标：让 Claude Code 只运行本脚本，不把密钥读进对话或打印出来。
脚本只输出键名和状态，不输出值。

用法：
    python apply_keys.py --keys-md /path/to/book-to-question-keys.local.md
    python apply_keys.py --keys-md ... --repo-root /path/to/repo
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

DOTENV_BLOCK = re.compile(r"```dotenv\s*\n(.*?)```", re.S)
MINERU_BLOCK = re.compile(r"##\s*MinerU token.*?```text\s*\n(.*?)```", re.S)
ENV_ASSIGN = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<eq>\s*=\s*)(?P<value>.*)$"
)


def parse_keys(md_path: Path) -> tuple[dict[str, str], str]:
    text = md_path.read_text(encoding="utf-8-sig")
    m = DOTENV_BLOCK.search(text)
    if not m:
        raise ValueError("文档里找不到 ```dotenv 代码块")
    env: dict[str, str] = {}
    for line in m.group(1).splitlines():
        s = line.strip()
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = _split_env_value(v.strip())[2]

    m2 = MINERU_BLOCK.search(text)
    if not m2:
        raise ValueError("文档里找不到 MinerU token 代码块")
    token = next((x.strip() for x in m2.group(1).splitlines() if x.strip()), "")
    if not token:
        raise ValueError("MinerU token 为空")
    return env, token


def _split_env_value(raw: str) -> tuple[str, str, str, str]:
    """把 = 后面的原文拆成 (前导空白, 引号, 值, 尾部空格+注释)。"""
    indent = raw[: len(raw) - len(raw.lstrip())]
    rest = raw.lstrip()
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end != -1:
            return indent, quote, rest[1:end], rest[end + 1 :]
    m = re.search(r"\s+#", rest)
    if m:
        return indent, "", rest[: m.start()].rstrip(), rest[m.start() :]
    return indent, "", rest.rstrip(), ""


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _file_mode(path: Path, default: int = 0o600) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return default


def _make_backup_dir(repo: Path) -> Path:
    root = repo / ".book-to-question-backup"
    base = time.strftime("%Y%m%d-%H%M%S-%f")
    candidate = root / base
    n = 1
    while candidate.exists():
        candidate = root / f"{base}-{n}"
        n += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def merge_dotenv(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    mode = _file_mode(path)
    seen = set()
    out = []
    for line in lines:
        m = ENV_ASSIGN.match(line)
        if m and m.group("key") in values:
            key = m.group("key")
            indent, quote, _old, tail = _split_env_value(m.group("value"))
            out.append(
                f"{m.group('prefix')}{key}{m.group('eq')}"
                f"{indent}{quote}{values[key]}{quote}{tail}"
            )
            seen.add(key)
            continue
        out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    _atomic_write(path, "\n".join(out).rstrip() + "\n", mode=mode)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="从私密 Markdown 写入 .env/config.json")
    p.add_argument("--keys-md", required=True, help="包含密钥的 Markdown 文件")
    p.add_argument("--repo-root", default=".", help="目标仓库根（默认当前目录）")
    p.add_argument("--dry-run", action="store_true", help="只报告将写入的键，不落盘")
    args = p.parse_args(argv)

    md = Path(args.keys_md).expanduser().resolve()
    repo = Path(args.repo_root).expanduser().resolve()
    if not md.is_file():
        print(f"密钥文档不存在：{md}", file=sys.stderr)
        return 2
    if not repo.is_dir():
        print(f"目标仓库不存在：{repo}", file=sys.stderr)
        return 2

    try:
        values, token = parse_keys(md)
    except (OSError, ValueError) as e:
        print(f"读取密钥文档失败：{e}", file=sys.stderr)
        return 2
    if not values:
        print("文档里没有可写入的 .env 键", file=sys.stderr)
        return 2

    print(f"目标：{repo}")
    print("将写入 .env：" + "、".join(values))
    print("将更新 config.json 的 api_token")
    if args.dry_run:
        print("（dry-run，没有写文件）")
        return 0

    env_path = repo / ".env"
    cfg_path = repo / "config.json"
    example = repo / "config.example.json"

    backup = None
    try:
        if env_path.exists() or cfg_path.exists():
            backup = _make_backup_dir(repo)
            if env_path.exists():
                shutil.copy2(env_path, backup / ".env")
            if cfg_path.exists():
                shutil.copy2(cfg_path, backup / "config.json")
        merge_dotenv(env_path, values)

        cfg_mode = _file_mode(cfg_path)
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        else:
            if not example.exists():
                raise FileNotFoundError("config.json 不存在，且没有 config.example.json")
            data = json.loads(example.read_text(encoding="utf-8-sig"))
        data["api_token"] = token
        _atomic_write(
            cfg_path,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            mode=cfg_mode,
        )
    except Exception as e:
        print(f"写入失败：{e}", file=sys.stderr)
        return 1

    if backup is not None:
        print(f"旧文件备份：{backup}")
    print("完成：.env 和 config.json 已更新（没有输出密钥值）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
