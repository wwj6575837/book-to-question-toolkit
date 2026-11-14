#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复已上传七牛的图片：下载 → 去除二维码 → 用相同 key 覆盖上传。

用法：
    python repair_qrcode_cdn.py --md-dir "exports/Organized_Books/<id>-..._organized"
    python repair_qrcode_cdn.py --md-dir "..." --apply

默认 dry-run，只下载并报告命中；--apply 才覆盖上传。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

import qrcode_removal as qr  # noqa: E402
import qiniu_markdown_images as qm  # noqa: E402

URL_RE = re.compile(r"https://[^\s\"'<>)]*?\.(?:jpg|jpeg|png|webp|gif|bmp)", re.I)


def collect_urls(md_dir: Path) -> list[str]:
    urls: list[str] = []
    seen = set()
    for path in sorted(md_dir.rglob("*.md")):
        if path.name == "000-文件索引.md" or ".bak-" in path.name:
            continue
        for m in URL_RE.finditer(path.read_text(encoding="utf-8-sig", errors="ignore")):
            url = m.group(0)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def fetch(url: str) -> bytes:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def process(url: str, apply: bool, bucket: str) -> dict:
    try:
        raw = fetch(url)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as e:
        return {"url": url, "status": "download_failed", "error": str(e)}

    out, boxes = qr.remove_qrcodes(im)
    if not boxes:
        return {"url": url, "status": "clean", "hits": 0}

    if not apply:
        return {"url": url, "status": "would_fix", "hits": len(boxes), "boxes": boxes}

    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=95, optimize=True)
    key = urlparse(url).path.lstrip("/")
    ok, _ = qm._upload_bytes_qiniu(buf.getvalue(), key, bucket, overwrite=True)
    return {
        "url": url,
        "status": "fixed" if ok else "upload_failed",
        "hits": len(boxes),
        "boxes": boxes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="修复七牛图片中的二维码")
    ap.add_argument("--md-dir", required=True, help="organized 目录，扫描其中的 CDN 图片 URL")
    ap.add_argument("--apply", action="store_true", help="默认 dry-run；加此参数才覆盖上传")
    ap.add_argument("--refresh", action="store_true", help="上传后刷新七牛 CDN 缓存（否则边缘节点可能仍旧图）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张（调试用）")
    args = ap.parse_args()

    md_dir = Path(args.md_dir)
    if not md_dir.is_dir():
        print(f"目录不存在：{md_dir}", file=sys.stderr)
        return 2

    qr.configure({})
    qm._ensure_project_dotenv_for_qiniu()
    cfg = qm._normalize_qiniu_config(None)
    bucket = str(cfg.get("bucket") or "")
    if not bucket:
        print("未配置 qiniu.bucket", file=sys.stderr)
        return 2

    urls = collect_urls(md_dir)
    if args.limit:
        urls = urls[: args.limit]
    print(f"图片 URL：{len(urls)}  模式：{'APPLY' if args.apply else 'dry-run'}")
    if not urls:
        return 0

    stats: dict[str, int] = {}
    fixes: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(process, u, args.apply, bucket) for u in urls]
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            stats[r["status"]] = stats.get(r["status"], 0) + 1
            if r["status"] in ("would_fix", "fixed", "upload_failed"):
                fixes.append(r)
                print(
                    f"  [{r['status']}] hits={r.get('hits', 0)} "
                    f"{r['url']} {r.get('error', '')}"
                )
            if i % 100 == 0:
                print(f"  进度 {i}/{len(urls)}")

    print("\n汇总：" + "，".join(f"{k}={v}" for k, v in sorted(stats.items())))

    if args.apply and args.refresh:
        fixed_urls = [r["url"] for r in fixes if r["status"] == "fixed"]
        if fixed_urls:
            from qiniu import Auth, CdnManager

            ak = os.environ.get("QINIU_ACCESS_KEY", "").strip()
            sk = os.environ.get("QINIU_SECRET_KEY", "").strip()
            if ak and sk:
                ret, info = CdnManager(Auth(ak, sk)).refresh_urls(fixed_urls)
                print(f"CDN 刷新：status={getattr(info, 'status_code', None)} "
                      f"提交 {len(fixed_urls)} 条")
            else:
                print("缺少 QINIU_ACCESS_KEY / QINIU_SECRET_KEY，跳过刷新")

    if not args.apply and fixes:
        print("确认清单后加 --apply 覆盖上传。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
