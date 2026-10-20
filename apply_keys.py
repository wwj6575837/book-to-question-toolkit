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
import sys
import time
from pathlib import Path

DOTENV_BLOCK = re.compile(r"```dotenv\s*\n(.*?)```", re.S)
MINERU_BLOCK = re.compile(r"##\s*MinerU token.*?```text\s*\n(.*?)```", re.S)


def parse_keys(md_path: Path) -> tuple[dict[str, str], str]:
    text = md_path.read_text(encoding="utf-8-sig")
    m = DOTENV_BLOCK.search(text)
    if not m:
        raise ValueError("文档里找不到 ```dotenv 代码块")
    env: dict[str, str] = {}
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

    m2 = MINERU_BLOCK.search(text)
    if not m2:
        raise ValueError("文档里找不到 MinerU token 代码块")
    token = next((x.strip() for x in m2.group(1).splitlines() if x.strip()), "")
    if not token:
        raise ValueError("MinerU token 为空")
    return env, token


def merge_dotenv(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in values:
                out.append(f"{k}={values[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = repo / ".book-to-question-backup" / stamp
    env_path = repo / ".env"
    cfg_path = repo / "config.json"
    example = repo / "config.example.json"

    try:
        if env_path.exists():
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(env_path, backup / ".env")
        merge_dotenv(env_path, values)

        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfg_path, backup / "config.json")
        else:
            if not example.exists():
                raise FileNotFoundError("config.json 不存在，且没有 config.example.json")
            data = json.loads(example.read_text(encoding="utf-8-sig"))
        data["api_token"] = token
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
    except Exception as e:
        print(f"写入失败：{e}", file=sys.stderr)
        return 1

    if backup.exists():
        print(f"旧文件备份：{backup}")
    print("完成：.env 和 config.json 已更新（没有输出密钥值）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
