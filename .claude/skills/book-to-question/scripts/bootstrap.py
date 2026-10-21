#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次使用 book-to-question skill 的初始化/自检。

做三件事：
1) 建补题目录；
2) 缺失时从 skill 的 assets/ 复制上题必需文件（仓库已有则保留，不覆盖）；
3) 缺 config.json / .env 时从 example 复制，并检查 Python、依赖、密钥。

不会写入任何密钥，也不会覆盖已有文件。阶段 2/3 仍需要 MinerU 仓库本体。

用法：
    python bootstrap.py            # 初始化缺失项 + 自检
    python bootstrap.py --check    # 只检查，不写文件
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
ASSETS = SKILL_DIR / "assets"

DIRS = ["补题/详细版", "补题/拆分后", "补题/题目", "补题/配置"]

# (仓库目标路径, assets 文件名, 说明)
FILES = [
    ("补题/命题官批量生成.py", "命题官批量生成.py", "上题批量生成脚本"),
    ("补题/拆分章节.py", "拆分章节.py", "按 ## 拆分详细版"),
    ("md/命题官提示词系统 V3.2.md", "命题官提示词系统 V3.2.md", "命题官提示词"),
]

# 阶段 2/3 依赖，缺了只提示、不复制
PIPELINE_FILES = [
    ("run_converter.py", "MinerU 转换入口"),
    ("mineru_batch_converter.py", "MinerU 批量转换"),
    ("qiniu_markdown_images.py", "七牛图片上传"),
    ("advanced_markdown_reorganizer.py", "Markdown 组织"),
    ("project_dotenv.py", ".env 加载"),
    ("multi_agent/utils.py", "流水线工具"),
    ("multi_agent/agent_runner.py", "LLM 调用"),
    ("multi_agent/ensemble_merge.py", "融合"),
    ("multi_agent/prompts", "Agent 提示词"),
    ("multi_agent/chapter_pipeline.py", "详细版流水线"),
    ("md/知识点总结提示词V5.1.md", "详细版提示词"),
    ("multi_agent/chapter_pipeline1.py", "精简版流水线"),
    ("md/精简版提示词1.md", "精简版提示词"),
    ("md/标题层级排序.md", "精简版标题层级提示词"),
]

PACKAGES = [
    ("fitz", "PyMuPDF（拆 PDF）", True),
    ("openai", "OpenAI 兼容客户端", True),
    ("requests", "语雀/MinerU 请求", True),
    ("qiniu", "七牛图片上传", False),
]

ENV_KEYS = [
    ("DEEPSEEK_API_KEY", "阶段 3/4 出题"),
    ("ARK_API_KEY", "阶段 3 豆包"),
    ("QINIU_ACCESS_KEY", "图片上传七牛"),
    ("QINIU_SECRET_KEY", "图片上传七牛"),
    ("YUQUE_TOKEN", "语雀上传"),
    ("YUQUE_REPO", "语雀知识库 namespace"),
]


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def env_value(key: str, files: list[Path]) -> str:
    v = os.getenv(key, "").strip()
    if v:
        return v
    for p in files:
        v = read_env_file(p).get(key, "").strip()
        if v:
            return v
    return ""


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def status(ok: bool, text: str) -> None:
    print(f"  {'✓' if ok else '✗'} {text}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="book-to-question 初始化/自检")
    p.add_argument("--check", action="store_true", help="只检查，不创建/复制文件")
    args = p.parse_args(argv)

    errors = 0
    warnings = 0
    print(f"仓库根：{REPO_ROOT}")
    print(f"skill：{SKILL_DIR}\n")

    # 1) 目录
    print("目录")
    for rel in DIRS:
        d = REPO_ROOT / rel
        if not d.exists() and not args.check:
            d.mkdir(parents=True, exist_ok=True)
        status(d.exists(), rel)

    # 2) 上题必需文件
    print("\n上题必需文件")
    for rel, asset_name, desc in FILES:
        target = REPO_ROOT / rel
        if target.exists():
            status(True, f"{rel}（已有，保留）")
            continue
        asset = ASSETS / asset_name
        if not asset.exists():
            status(False, f"{rel}（缺失，assets 里也没有：{asset_name}）")
            errors += 1
            continue
        if args.check:
            status(False, f"{rel}（缺失，可自动复制）")
            errors += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, target)
        status(True, f"{rel}（已从 assets 复制）")

    # 3) 配置/环境模板
    print("\n配置与环境")
    for example, target, hint in [
        ("config.example.json", "config.json", "填 MinerU api_token"),
        (".env.example", ".env", "填 DeepSeek/豆包/七牛/语雀密钥"),
    ]:
        ex = REPO_ROOT / example
        tg = REPO_ROOT / target
        if tg.exists():
            status(True, f"{target}（已有，保留）")
        elif not ex.exists():
            status(False, f"{target}（缺失，且没有 {example}）")
            errors += 1
        elif args.check:
            status(False, f"{target}（缺失，可自动复制；{hint}）")
            errors += 1
        else:
            shutil.copy2(ex, tg)
            status(True, f"{target}（已从 {example} 复制，请{hint}）")

    # 4) Python / 依赖
    print("\nPython 与依赖")
    status(sys.version_info >= (3, 12), f"Python {sys.version.split()[0]}（需要 ≥3.12）")
    if sys.version_info < (3, 12):
        errors += 1
    for mod, desc, required in PACKAGES:
        ok = has_module(mod)
        status(ok, f"{mod} - {desc}" + ("（必需）" if required else "（可选）"))
        if required and not ok:
            errors += 1

    # 5) 密钥
    print("\n密钥/配置项")
    env_files = [REPO_ROOT / ".env"]
    for key, desc in ENV_KEYS:
        val = env_value(key, env_files)
        status(bool(val), f"{key} - {desc}" + ("" if val else "（未设置）"))
        if not val:
            if key in {"DEEPSEEK_API_KEY", "ARK_API_KEY"}:
                errors += 1
            else:
                warnings += 1
    cfg_path = REPO_ROOT / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            token = str(cfg.get("api_token", "")).strip()
            ok = bool(token) and token != "请替换为您的MinerU API Token"
            status(ok, "config.json api_token" + ("" if ok else "（未填）"))
            if not ok:
                errors += 1
        except (OSError, json.JSONDecodeError):
            status(False, "config.json（JSON 格式错误）")
            errors += 1
    else:
        status(False, "config.json（不存在）")
        errors += 1

    # 6) 阶段 2/3 依赖
    print("\n阶段 2/3 依赖（缺失时只能跑阶段 1/4）")
    for rel, desc in PIPELINE_FILES:
        ok = (REPO_ROOT / rel).exists()
        status(ok, f"{rel} - {desc}")
        if not ok:
            errors += 1

    # 7) 分享前检查
    print("\n分享前检查")
    print("  ⚠ .env / config.json / .book-to-question-backup/ 不要随仓库或压缩包发给别人")

    if errors == 0 and warnings == 0:
        print("\n结果：全部就绪")
    else:
        print(f"\n结果：还有 {errors} 项必需、{warnings} 项可选未就绪")
    if args.check:
        print("（--check 模式，没有写入任何文件）")
    else:
        print("下一步：填好 config.json 和 .env，然后按 SKILL.md 的阶段 1→4 执行。")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
