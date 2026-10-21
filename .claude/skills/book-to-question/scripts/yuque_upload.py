#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆书上题 skill：用语雀 Token 读取目录、建分组、批量建 Markdown 文档。

Token 和 namespace 从 YUQUE_TOKEN / YUQUE_REPO（环境变量或仓库根 .env）读取，也可用 --repo 覆盖。

用法：
    python yuque_upload.py toc
    python yuque_upload.py toc --repo <user/repo>
    python yuque_upload.py upload --repo <user/repo> --section 原书 \
        --book-id 1957 --book "逻辑思维训练教程" --dir "D:\\...\\1957-..._organized"
    python yuque_upload.py upload --repo <user/repo> --section 详细版 \
        --book-id 1957 --book "逻辑思维训练教程" \
        --file "exports\\multi_agent\\final\\书籍库\\1957-1 逻辑思维训练教程.md" \
        --doc-title "1957-1 逻辑思维训练教程"

默认 dry-run；加 --apply 才写语雀。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    print("缺少 requests：请用 .venv 里的 python 运行", file=sys.stderr)
    raise SystemExit(2)

BASE = "https://www.yuque.com/api/v2"
REPO_ROOT = Path(__file__).resolve().parents[4]
ROOT_ENV_FILE = REPO_ROOT / ".env"
INDEX_NAME = "000-文件索引.md"

_ILLEGAL_SLUG = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")


def load_env_value(key: str) -> str:
    """环境变量优先，其次仓库根 .env。"""
    value = os.getenv(key, "").strip()
    if value:
        return value
    for path in (ROOT_ENV_FILE,):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load_token() -> str:
    return load_env_value("YUQUE_TOKEN")


def resolve_repo(arg: str | None) -> str:
    repo = (arg or load_env_value("YUQUE_REPO")).strip()
    if not repo:
        raise ValueError(
            "没有语雀 namespace。请用 --repo <user/repo> 传入，"
            "或在 .env 里设置 YUQUE_REPO=https://www.yuque.com/<user>/<repo>"
        )
    return parse_repo(repo)


def parse_repo(value: str) -> str:
    """支持 URL、'user/repo'、'user/repo/xxx'。"""
    value = value.strip().rstrip("/")
    if value.startswith("http"):
        path = urlparse(value).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        raise ValueError(f"无法从 URL 解析 namespace：{value}")
    parts = [p for p in value.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"namespace 应为 user/repo：{value}")
    return f"{parts[0]}/{parts[1]}"


class Yuque:
    def __init__(self, token: str, delay: float = 0.5):
        self.delay = max(0.0, delay)
        self.s = requests.Session()
        self.s.headers.update({"X-Auth-Token": token})
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT"],
        )
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self._last = 0.0

    def _pace(self):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def request(self, method: str, path: str, **kwargs):
        self._pace()
        url = f"{BASE}{path}"
        resp = self.s.request(method, url, timeout=30, **kwargs)
        if resp.status_code == 401:
            raise RuntimeError("401：Token 无效或已过期")
        if resp.status_code == 403:
            raise RuntimeError("403：Token 缺少 repo:write / doc:write 权限")
        if resp.status_code >= 400:
            detail = resp.text[:500].replace("\n", " ")
            hint = ""
            if resp.status_code == 422:
                hint = "（正文可能含语雀不支持的 HTML，或字段不合法）"
            raise RuntimeError(f"{resp.status_code}：{detail}{hint}")
        if not resp.text.strip():
            return {}
        body = resp.json()
        return body.get("data", body)

    def get(self, path: str, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path: str, payload: dict):
        return self.request("POST", path, json=payload)

    def put(self, path: str, payload: dict):
        return self.request("PUT", path, json=payload)


def get_toc(api: Yuque, repo: str) -> list[dict]:
    data = api.get(f"/repos/{repo}/toc")
    return data if isinstance(data, list) else []


def build_tree(nodes: list[dict]) -> dict[str | None, list[dict]]:
    by_parent: dict[str | None, list[dict]] = {}
    for n in nodes:
        by_parent.setdefault(n.get("parent_uuid"), []).append(n)
    return by_parent


def print_tree(
    nodes: list[dict],
    as_json: bool = False,
    max_depth: int = 1,
    filter_text: str | None = None,
    full: bool = False,
) -> None:
    if as_json:
        print(json.dumps(nodes, ensure_ascii=False, indent=2))
        return

    by_parent = build_tree(nodes)
    by_uuid = {n.get("uuid"): n for n in nodes}

    def child_count(uuid: str) -> int:
        return len(by_parent.get(uuid, []))

    def label(node: dict) -> str:
        extra = f" doc_id={node.get('doc_id')}" if node.get("doc_id") else ""
        return f"[{node.get('type', '?')}] {node.get('title', '')}  uuid={node.get('uuid', '')}{extra}"

    if filter_text:
        shown: set[tuple] = set()
        needle = filter_text.lower()
        for n in nodes:
            if needle not in str(n.get("title", "")).lower():
                continue
            path, cur = [], n
            while cur is not None:
                path.append(cur)
                parent_uuid = cur.get("parent_uuid")
                cur = by_uuid.get(parent_uuid) if parent_uuid else None
            path.reverse()
            key = tuple(x.get("uuid") for x in path)
            if key in shown:
                continue
            shown.add(key)
            print(" > ".join(label(x) for x in path) + f"  子节点={child_count(n.get('uuid', ''))}")
        if not shown:
            print(f"没有标题包含「{filter_text}」的节点")
        return

    roots = by_parent.get(None, []) + by_parent.get("", [])
    seen: set[str] = set()
    limit = None if full else max(0, int(max_depth))

    def walk(node: dict, depth: int):
        uuid = node.get("uuid", "")
        if uuid in seen:
            return
        seen.add(uuid)
        print(f"{'  ' * depth}{label(node)}  子节点={child_count(uuid)}")
        if limit is not None and depth + 1 >= limit:
            return
        for child in by_parent.get(uuid, []):
            walk(child, depth + 1)

    for r in roots:
        walk(r, 0)
    if limit is None and len(seen) < len(nodes):
        for n in nodes:
            if n.get("uuid") not in seen:
                walk(n, 0)


def find_nodes(nodes: list[dict], title: str, node_type: str | None = None) -> list[dict]:
    return [
        n for n in nodes
        if (not node_type or n.get("type") == node_type)
        and n.get("title", "").strip() == title.strip()
    ]


def find_node(nodes: list[dict], title: str, node_type: str | None = None) -> dict | None:
    hits = find_nodes(nodes, title, node_type)
    return hits[0] if hits else None


def find_child(
    nodes: list[dict], parent_uuid: str, title: str, node_type: str | None = None
) -> dict | None:
    hits = [
        n for n in nodes
        if (not node_type or n.get("type") == node_type)
        and n.get("parent_uuid") == parent_uuid
        and n.get("title", "").strip() == title.strip()
    ]
    if len(hits) > 1:
        raise ValueError(
            f"父节点下存在多个同名分组「{title}」："
            + "、".join(str(n.get("uuid")) for n in hits)
        )
    return hits[0] if hits else None


def toc_write(api: Yuque, repo: str, body: dict):
    """部分语雀版本接受单个 action 对象，部分要求数组；先对象后数组。"""
    try:
        return api.put(f"/repos/{repo}/toc", body)
    except RuntimeError as e:
        text = str(e)
        if "400" in text or "422" in text:
            return api.put(f"/repos/{repo}/toc", [body])
        raise


def create_title_node(api: Yuque, repo: str, title: str, parent_uuid: str | None) -> None:
    body: dict = {"action": "appendNode", "type": "TITLE", "title": title}
    if parent_uuid:
        body.update({"target_uuid": parent_uuid, "action_mode": "child"})
    toc_write(api, repo, body)


def create_doc(api: Yuque, repo: str, title: str, slug: str, body: str) -> dict:
    return api.post(f"/repos/{repo}/docs", {
        "title": title,
        "slug": slug,
        "format": "markdown",
        "body": body,
        "public": 0,
    })


def update_doc(api: Yuque, repo: str, doc_id: int, title: str, body: str) -> dict:
    return api.put(f"/repos/{repo}/docs/{doc_id}", {
        "title": title,
        "format": "markdown",
        "body": body,
    })


def attach_doc(api: Yuque, repo: str, doc_id: int, parent_uuid: str) -> None:
    toc_write(api, repo, {
        "action": "appendNode",
        "type": "DOC",
        "doc_ids": [int(doc_id)],
        "target_uuid": parent_uuid,
        "action_mode": "child",
    })


def make_slug(title: str) -> str:
    import hashlib
    base = _ILLEGAL_SLUG.sub("-", title).strip("-").lower()[:80] or "doc"
    suffix = hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


def collect_files(args: argparse.Namespace) -> list[tuple[str, Path]]:
    if args.file:
        p = Path(args.file)
        if not p.is_file():
            raise ValueError(f"文件不存在：{p}")
        title = args.doc_title or p.stem
        return [(title, p)]
    directory = Path(args.dir)
    if not directory.is_dir():
        raise ValueError(f"目录不存在：{directory}")
    out = []
    for p in sorted(directory.glob("*.md")):
        if p.name == INDEX_NAME:
            continue
        title = p.stem
        if args.strip_suffix and title.endswith(args.strip_suffix):
            title = title[: -len(args.strip_suffix)]
        out.append((title, p))
    return out


def cmd_toc(args: argparse.Namespace) -> int:
    token = load_token()
    if not token:
        print("找不到 YUQUE_TOKEN（环境变量或仓库根 .env）", file=sys.stderr)
        return 2
    try:
        repo = resolve_repo(args.repo)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    api = Yuque(token, args.delay)
    try:
        nodes = get_toc(api, repo)
    except Exception as e:
        print(f"读取 TOC 失败：{e}", file=sys.stderr)
        return 1
    print(f"namespace: {repo}  节点数: {len(nodes)}\n")
    print_tree(
        nodes,
        as_json=args.json,
        max_depth=args.max_depth,
        filter_text=args.filter,
        full=args.full,
    )
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    token = load_token()
    if not token:
        print("找不到 YUQUE_TOKEN（环境变量或仓库根 .env）", file=sys.stderr)
        return 2
    try:
        repo = resolve_repo(args.repo)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    api = Yuque(token, args.delay)

    try:
        nodes = get_toc(api, repo)
    except Exception as e:
        print(f"读取 TOC 失败：{e}", file=sys.stderr)
        return 1

    parent = None
    if args.parent_uuid:
        parent = next((n for n in nodes if n.get("uuid") == args.parent_uuid), None)
        if not parent:
            print(f"找不到 parent uuid：{args.parent_uuid}", file=sys.stderr)
            return 2
    elif args.section:
        hits = find_nodes(nodes, args.section, "TITLE") or find_nodes(nodes, args.section)
        if len(hits) > 1:
            print(f"「{args.section}」有多个同名节点，请用 --parent-uuid 指定：", file=sys.stderr)
            for n in hits:
                print(f"  [{n.get('type')}] {n.get('title')}  uuid={n.get('uuid')}")
            return 2
        parent = hits[0] if hits else None
        if not parent:
            print(
                f"在 TOC 顶层找不到「{args.section}」。先运行 toc 查看结构，"
                "或用 --parent-uuid 指定父节点。",
                file=sys.stderr,
            )
            print("\n当前顶层节点：")
            for n in nodes:
                if not n.get("parent_uuid"):
                    print(f"  [{n.get('type')}] {n.get('title')}  uuid={n.get('uuid')}")
            return 2
    else:
        print("必须给 --section 或 --parent-uuid", file=sys.stderr)
        return 2

    try:
        files = collect_files(args)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if not files:
        print("没有可上传的 Markdown", file=sys.stderr)
        return 2

    if args.flat:
        # 详细版/精简版在语雀里是平铺的：文档直接挂在 section 节点下
        book_group_title = parent.get("title", "")
        group = parent
    else:
        book_group_title = args.group_title or f"{args.book_id}-{args.book}"
        try:
            group = find_child(nodes, parent.get("uuid"), book_group_title, "TITLE")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2

    existing: dict[str, dict] = {}
    if group:
        by_parent = build_tree(nodes)
        for n in by_parent.get(group.get("uuid"), []):
            if n.get("type") == "DOC":
                existing[n.get("title", "")] = n

    print(f"namespace: {repo}")
    print(f"父节点：{parent.get('title')}  uuid={parent.get('uuid')}")
    if args.flat:
        print(f"上传方式：直接挂在「{book_group_title}」下（平铺）")
    else:
        print(f"书籍分组：{book_group_title}  " + ("已存在" if group else "将创建"))
    print(f"待处理文档：{len(files)}")
    for title, path in files:
        state = "更新" if title in existing and args.update else ("跳过（已存在）" if title in existing else "新建")
        print(f"  [{state}] {title}  <- {path.name}")

    if not args.apply:
        print("\n当前是 dry-run；确认无误后加 --apply 才写入语雀。")
        return 0

    try:
        if not args.flat and not group:
            create_title_node(api, repo, book_group_title, parent.get("uuid"))
            nodes = get_toc(api, repo)
            group = find_child(nodes, parent.get("uuid"), book_group_title, "TITLE")
            if not group:
                print("创建分组后仍找不到该节点，中止。", file=sys.stderr)
                return 1
            existing = {}

        made = updated = skipped = 0
        for title, path in files:
            body = path.read_text(encoding="utf-8-sig")
            cur = existing.get(title)
            if cur and not cur.get("doc_id"):
                cur = None
            if cur and not args.update:
                skipped += 1
                continue
            if cur:
                update_doc(api, repo, int(cur["doc_id"]), title, body)
                updated += 1
            else:
                doc = create_doc(api, repo, title, make_slug(f"{book_group_title}/{title}"), body)
                doc_id = doc.get("id") or doc.get("doc_id")
                if not doc_id:
                    print(f"⚠ 创建文档未返回 id：{title}", file=sys.stderr)
                    continue
                attach_doc(api, repo, int(doc_id), group["uuid"])
                made += 1
    except Exception as e:
        print(f"\n上传失败：{e}", file=sys.stderr)
        return 1

    # 复核
    try:
        final_nodes = get_toc(api, repo)
        if args.flat:
            final_group = next(
                (n for n in final_nodes if n.get("uuid") == parent.get("uuid")), None
            )
        else:
            final_group = find_child(final_nodes, parent.get("uuid"), book_group_title, "TITLE")
        count = 0
        if final_group:
            count = sum(
                1 for n in final_nodes
                if n.get("parent_uuid") == final_group.get("uuid") and n.get("type") == "DOC"
            )
        print(f"\n完成：新建 {made}，更新 {updated}，跳过 {skipped}；分组下现有 {count} 篇。")
        if count < len(files):
            print(f"⚠ 分组下文档数少于本地文件数（{count}/{len(files)}），请人工核对。")
    except Exception as e:
        print(f"⚠ 复核 TOC 失败：{e}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="拆书上题：语雀 TOC / 上传")
    p.add_argument("--delay", type=float, default=0.5, help="请求间隔秒数（默认 0.5）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("toc", help="只读列出目录树")
    pt.add_argument("--repo", help="user/repo 或知识库 URL；省略则读 YUQUE_REPO")
    pt.add_argument("--json", action="store_true")
    pt.add_argument("--max-depth", type=int, default=1,
                    help="打印深度（默认 1=只打印顶层+子节点数）")
    pt.add_argument("--filter", help="只显示标题包含该关键词的节点路径")
    pt.add_argument("--full", action="store_true", help="打印完整目录树")
    pt.set_defaults(func=cmd_toc)

    pu = sub.add_parser("upload", help="建分组并批量建/更新文档")
    pu.add_argument("--repo", help="user/repo 或知识库 URL；省略则读 YUQUE_REPO")
    pu.add_argument("--section", help="父节点标题，如 原书 / 详细版")
    pu.add_argument("--parent-uuid", help="直接指定父节点 uuid（优先于 --section）")
    pu.add_argument("--book-id", required=True)
    pu.add_argument("--book", required=True)
    pu.add_argument("--group-title", help="默认 <book-id>-<book>")
    pu.add_argument("--flat", action="store_true",
                    help="文档直接挂在 section 下，不建 <id>-<书名> 子分组（详细版/精简版用）")
    src = pu.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="原书：目录下所有 *.md（跳过 000-文件索引.md）")
    src.add_argument("--file", help="详细版：单个 md")
    pu.add_argument("--doc-title", help="配合 --file 的文档标题")
    pu.add_argument("--strip-suffix", default="_full", help="去掉文件名的后缀（默认 _full）")
    pu.add_argument("--update", action="store_true", help="已存在的同标题文档改为更新")
    pu.add_argument("--apply", action="store_true", help="默认 dry-run；加此参数才写")
    pu.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    pu.set_defaults(func=cmd_upload)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
