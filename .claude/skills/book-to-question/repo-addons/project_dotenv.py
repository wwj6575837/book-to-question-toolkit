#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从项目根目录 `.env` 加载环境变量。

优先使用 python-dotenv（与 override=False 一致：已有环境变量不被覆盖）。
若未安装该包，则用手写解析器读取 `.env`，避免因静默跳过导致七牛等密钥从未注入。
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv_manual(env_path: Path, *, override: bool) -> None:
    """最小 .env 解析：注释行、export KEY=、KEY=VALUE，不含 dotenv 的高级换行/嵌套规则。"""
    try:
        raw = env_path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            continue
        key, _, rest = s.partition("=")
        key = key.strip()
        if not key:
            continue
        value = rest.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override:
            os.environ[key] = value
        elif key not in os.environ:
            os.environ[key] = value


def load_project_dotenv() -> None:
    root = Path(__file__).resolve().parent
    env_path = root / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    _load_dotenv_manual(env_path, override=False)
