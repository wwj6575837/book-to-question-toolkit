from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .utils import safe_read_text, safe_write_text, SimpleFileLock
from .agent_runner import run_agent, _PRINT_LOCK

# ============================================================
# 并发信号量
# ============================================================

def _safe_int_env(key: str, default: int) -> int:
    try:
        val = int(os.getenv(key, str(default)))
        return max(1, val)
    except (ValueError, TypeError):
        return default

_SEMA_DEEPSEEK = threading.Semaphore(_safe_int_env("DEEPSEEK_MAX_CONCURRENCY", 3))
_SEMA_DOUBAO = threading.Semaphore(_safe_int_env("DOUBAO_MAX_CONCURRENCY", 3))
_SEMA_DEEPSEEK_MERGE = threading.Semaphore(_safe_int_env("DEEPSEEK_MERGE_MAX_CONCURRENCY", 2))

def set_provider_limits(deepseek: Optional[int] = None,
                        doubao: Optional[int] = None) -> None:
    global _SEMA_DEEPSEEK, _SEMA_DOUBAO
    try:
        if deepseek is not None:
            _SEMA_DEEPSEEK = threading.Semaphore(max(1, int(deepseek)))
        if doubao is not None:
            _SEMA_DOUBAO = threading.Semaphore(max(1, int(doubao)))
    except Exception:
        pass

# ============================================================
# 工具
# ============================================================

def now_str() -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def format_duration_cn(seconds: float) -> str:
    try:
        total = int(round(seconds))
    except Exception:
        total = int(seconds) if isinstance(seconds, (int,)) else 0
    if total < 60:
        return f"{total}秒"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}分{s:02d}秒"
    h, m = divmod(m, 60)
    return f"{h}小时{m}分{s:02d}秒"

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def compute_hash(text: str) -> str:
    h = hashlib.sha1()
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()

# ============================================================
# Markdown 按 ## 拆分
# ============================================================

RE_CHAPTER_NUM = re.compile(r"第\s*([一二三四五六七八九十百千万\d]+)\s*(章|单元|讲|编|课|模块|项目|部分|篇)")

CHN_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

def cn_to_int(s: str) -> int:
    s = s.strip()
    if not s:
        return 0
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return 0
    total = 0
    if "十" in s:
        parts = s.split("十")
        left = parts[0]
        right = parts[1] if len(parts) > 1 else ""
        a = CHN_MAP.get(left, 1 if left == "" else 0)
        b = CHN_MAP.get(right, 0) if right != "" else 0
        total = a * 10 + b
    else:
        total = CHN_MAP.get(s, 0)
    return int(total) if isinstance(total, int) else 0

def extract_chapter_num(h2_title: str) -> int:
    m = RE_CHAPTER_NUM.search(h2_title)
    if not m:
        return -1
    raw = m.group(1)
    return cn_to_int(raw) if not raw.isdigit() else int(raw)

def slugify_chapter(h2_title: str, chapter_index: int) -> str:
    clean = re.sub(r"[^\w\u4e00-\u9fff\s\-]", "", h2_title)
    clean = re.sub(r"\s+", "-", clean.strip())
    # 格式为: 01-标题前30字
    idx_str = str(chapter_index).zfill(2)
    return f"{idx_str}-{clean[:30]}" if clean else f"{idx_str}-未命名章节"

@dataclass
class ChapterSlice:
    know_id: int
    book_name: str
    chapter_no: int
    title: str
    content: str
    prefix: str
    chapter_dir: str
    chapter_index: int = 0

# 修复后的 RE_H2，支持 0-3 个空格前导
RE_H2 = re.compile(r"^ {0,3}##\s+(.*?)\s*$")
# 节级切分点（仅在 split_by_section 时启用）
# 支持“### 第一节 …”与“### 2.1 …”两种节标题；三级编号（2.21.1）不算节
RE_H3_SECTION = re.compile(
    r"^ {0,3}###\s+(第\s*[一二三四五六七八九十百千万\d]+\s*节.*"
    r"|\d+\.\d+(?![.\d])(?:\s.*)?)$")

def split_one_book(md_text: str, know_id: int, book_name: str, chapter_dir: str,
                   split_by_section: bool = False) -> List[ChapterSlice]:
    lines = md_text.splitlines()
    h2_indices: List[Tuple[int, str]] = []

    in_code_block = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            in_code_block = not in_code_block
            continue

        if not in_code_block:
            m = RE_H2.match(ln)
            if m:
                h2_indices.append((i, m.group(1).strip()))
            elif split_by_section:
                ms = RE_H3_SECTION.match(ln)
                if ms:
                    h2_indices.append((i, ms.group(1).strip()))

    if not h2_indices:
        return [ChapterSlice(know_id=know_id, book_name=book_name, chapter_no=1,
                             title="全书", content=md_text, prefix="全书",
                             chapter_dir=chapter_dir, chapter_index=0)]
                             
    chapters: List[ChapterSlice] = []
    
    # 保留第一个 ## 前面的内容（如书名、前言）
    first_h2_idx = h2_indices[0][0]
    if first_h2_idx > 0:
        preface_lines = lines[0:first_h2_idx]
        preface_text = "\n".join(preface_lines).strip()
        if preface_text:  
            chapters.append(ChapterSlice(
                know_id=know_id, book_name=book_name, chapter_no=0,
                title="前言/书籍概述", content=preface_text,
                prefix=slugify_chapter("前言", 1), # 前言是 01
                chapter_dir=chapter_dir, chapter_index=0
            ))

    offset = len(chapters) # 如果有前言，offset 就是 1，后面的标题从 02 开始
    
    # 状态机：记录上一个成功分配的"主章节号"
    last_assigned_no = 0 
    
    # 检查列表中是否包含真实编号的第一章，如果有，绪论等前置章节从 0 开始
    has_real_chapter_one = any(extract_chapter_num(t) == 1 for _, t in h2_indices)
    if not has_real_chapter_one:
        last_assigned_no = 0

    for idx, (start_line, title) in enumerate(h2_indices):
        end_line = h2_indices[idx + 1][0] if idx + 1 < len(h2_indices) else len(lines)
        chapter_lines = lines[start_line:end_line]
        chapter_text = "\n".join(chapter_lines).strip()
        
        # 提取真实章节号
        ch_no = extract_chapter_num(title)
        
        if ch_no > 0:
            # 解析成功，它是一个新的主章节，更新记录
            last_assigned_no = ch_no
        else:
            # 没有解析出编号（如“绪论”或“本章小结”）
            if "绪论" in title or "前言" in title or "序" in title:
                ch_no = 0
            else:
                # 遇到普通的二级标题（例如"本章小结"或未标号的补充标题）
                # 不要自增！把它作为当前主章节的一部分（同属一个 chapter_no）
                ch_no = last_assigned_no
            
        idx_with_offset = idx + offset
        prefix = slugify_chapter(title, idx_with_offset + 1) # 给 idx_with_offset + 1，从 01 开始
        chapters.append(ChapterSlice(
            know_id=know_id, book_name=book_name, chapter_no=ch_no, 
            title=title, content=chapter_text,
            prefix=prefix, chapter_dir=chapter_dir, chapter_index=idx_with_offset
        ))
        
    return chapters

# ============================================================
# 文件扫描 & 全量拆分
# ============================================================

def parse_book_filename(filename: str) -> Tuple[int, str]:
    base = os.path.basename(filename)
    if base.lower().endswith(".md"):
        base = base[:-3]
    m = re.match(r"(\d+)-1\s+(.+)", base)
    if not m:
        raise ValueError(f"Invalid book filename: {filename}")
    return int(m.group(1)), m.group(2).strip()

def scan_and_split_all(input_root: str,
                       target_ids: Optional[set] = None,
                       split_by_section: bool = False) -> List[ChapterSlice]:
    all_chapters: List[ChapterSlice] = []
    if not os.path.isdir(input_root):
        return all_chapters
    dir_names = sorted(
        [d for d in os.listdir(input_root) if os.path.isdir(os.path.join(input_root, d))],
        key=lambda x: int(x) if x.isdigit() else 0)
    for dir_name in dir_names:
        if not dir_name.isdigit():
            continue
        kid = int(dir_name)
        if target_ids and kid not in target_ids:
            continue
        chapter_dir = os.path.join(input_root, dir_name)
        md_files = [f for f in os.listdir(chapter_dir)
                    if f.lower().endswith(".md")
                    and not f.lower().endswith(".source.md")
                    and "_from_source" not in f.lower()
                    and ".merged" not in f.lower()]
        if not md_files:
            with _PRINT_LOCK:
                print(f"[{now_str()}] [跳过] {dir_name} 中没有可处理文件")
            continue
        for mdfn in md_files:
            try:
                _, book_name = parse_book_filename(mdfn)
            except ValueError:
                with _PRINT_LOCK:
                    print(f"[{now_str()}] [跳过] {dir_name}/{mdfn} 格式不符")
                continue
            md_path = os.path.join(chapter_dir, mdfn)
            with _PRINT_LOCK:
                print(f"[{now_str()}] [拆分] {kid}-{book_name}")
            try:
                full_text = safe_read_text(md_path)
                chapters = split_one_book(full_text, kid, book_name, chapter_dir,
                                          split_by_section=split_by_section)
                all_chapters.extend(chapters)
                with _PRINT_LOCK:
                    print(f"[{now_str()}] [拆分完成] {kid}-{book_name} -> {len(chapters)}章")
            except Exception as e:
                with _PRINT_LOCK:
                    print(f"[{now_str()}] [拆分失败] {kid}-{book_name}: {e}")
    all_chapters.sort(key=lambda c: (c.know_id, c.chapter_index))
    return all_chapters

# ============================================================
# LLM Agent 调用
# ============================================================

def run_llm_agent(agent: str, provider: str, system_prompts: List[str], inputs: List[str],
                  output: str, model: Optional[str] = None, key_env: Optional[str] = None,
                  base: Optional[str] = None, timeout: int = 300, stream: bool = True,
                  show_thinking: bool = True, extra: Optional[str] = None,
                  auto_chunk: bool = False, max_tokens: int = 16000) -> None:
    ns = argparse.Namespace(
        agent=agent, provider=provider, model=model,
        system_prompts=system_prompts, inputs=inputs,
        inputs_glob=[], input_dir=None, exts=[".md", ".txt"],
        inputs_file=None, output=output, max_tokens=max_tokens,
        temperature=0.1, api_key=None, base=base, key_env=key_env,
        base_env=None, timeout=timeout, stream=stream,
        show_thinking=show_thinking, extra=extra, auto_chunk=auto_chunk,
        func=run_agent)
    run_agent(ns)

# ============================================================
# 单章处理
# ============================================================

@dataclass
class ChapterResult:
    know_id: int
    book_name: str
    chapter_no: int
    chapter_index: int
    prefix: str
    title: str
    source_path: str
    deepseek_path: str
    doubao_path: str
    merged_path: str
    chapter_hash: str
    status: str
    elapsed_sec: float
    retries: Dict[str, int]
    agent_times: Dict[str, float]

def _compute_merge_input_fingerprint(paths: List[str]) -> str:
    hashes = []
    for p in paths:
        if p and os.path.exists(p):
            try:
                hashes.append(compute_hash(safe_read_text(p)))
            except Exception:
                hashes.append("missing")
        else:
            hashes.append("missing")
    return compute_hash("|".join(hashes))

def process_one_chapter(
    ch: ChapterSlice,
    prompt_path: str,
    retries: int,
    retry_backoff: int,
    incremental_resume: bool,
    skip_merge_unchanged: bool,
    force: bool,
    auto_chunk: bool,
    progress: Dict[str, Any],
) -> ChapterResult:
    """处理单个章节：source -> DeepSeek/Doubao -> merge。"""
    t0 = time.time()
    chapter_dir = ch.chapter_dir
    prefix = ch.prefix
    bk_key = str(ch.know_id)
    ch_key = str(ch.chapter_index)

    with _PRINT_LOCK:
        print(f"[{now_str()}] [章节开始] {ch.book_name} 第{ch.chapter_no}章 {ch.title}")

    source_path = os.path.join(chapter_dir, f"{prefix}.source.md")
    deepseek_path = os.path.join(chapter_dir, f"{prefix}.deepseek_from_source.md")
    doubao_path = os.path.join(chapter_dir, f"{prefix}.doubao_from_source.md")
    merged_path = os.path.join(chapter_dir, f"{prefix}.merged_from_source.md")

    safe_write_text(source_path, ch.content)
    chapter_hash = compute_hash(ch.content)

    existing_source_hash = None
    try:
        if os.path.exists(source_path):
            existing_source_hash = compute_hash(safe_read_text(source_path))
    except Exception:
        pass

    book_rec = progress.get("books", {}).get(bk_key, {})
    ch_rec = book_rec.get("chapters", {}).get(ch_key, {})
    prev_hash = ch_rec.get("chapter_hash")
    prev_merge_fingerprint = ch_rec.get("merge_fingerprint")

    allow_partial = incremental_resume and (
        (prev_hash is not None and prev_hash == chapter_hash)
        or (existing_source_hash is not None and existing_source_hash == chapter_hash))

    db_ok = os.path.exists(deepseek_path)
    dou_ok = os.path.exists(doubao_path)
    merged_ok = os.path.exists(merged_path)
    paths_ok = db_ok and dou_ok and merged_ok

    if (not force) and paths_ok and (prev_hash == chapter_hash or existing_source_hash == chapter_hash):
        with _PRINT_LOCK:
            print(f"[{now_str()}] [章节跳过] {ch.book_name} 第{ch.chapter_no}章 {ch.title} 已是最新")
        return ChapterResult(
            know_id=ch.know_id, book_name=ch.book_name, chapter_no=ch.chapter_no,
            chapter_index=ch.chapter_index,
            prefix=prefix, title=ch.title,
            source_path=source_path, deepseek_path=deepseek_path,
            doubao_path=doubao_path, merged_path=merged_path,
            chapter_hash=chapter_hash, status="done", elapsed_sec=0.0,
            retries={"deepseek": 0, "doubao": 0, "merge": 0},
            agent_times={"deepseek": 0.0, "doubao": 0.0, "merge": 0.0})

    retries_map: Dict[str, int] = {"deepseek": 0, "doubao": 0, "merge": 0}
    agent_times: Dict[str, float] = {"deepseek": 0.0, "doubao": 0.0, "merge": 0.0}

    def with_retries(tag: str, fn):
        attempt = 0
        last_err = None
        while attempt <= retries:
            try:
                fn()
                return
            except Exception as e:
                last_err = e
                attempt += 1
                retries_map[tag] = attempt
                if attempt <= retries:
                    sleep_s = min(retry_backoff * (2 ** (attempt - 1)), 60)
                    with _PRINT_LOCK:
                        print(f"[{now_str()}] [章节重试] {ch.book_name} 第{ch.chapter_no}章 "
                              f"{ch.title} agent={tag} 第{attempt}/{retries+1}次失败；"
                              f"{format_duration_cn(sleep_s)}后重试")
                    time.sleep(sleep_s)
                else:
                    with _PRINT_LOCK:
                        print(f"[{now_str()}] [章节失败] {ch.book_name} 第{ch.chapter_no}章 "
                              f"{ch.title} agent={tag} 在{retries+1}次尝试后仍失败")
                    raise

    def run_deepseek():
        _SEMA_DEEPSEEK.acquire()
        try:
            run_llm_agent(
                agent="deepseek_from_source", provider="openai_compat",
                system_prompts=[prompt_path], inputs=[source_path],
                output=deepseek_path, key_env="DEEPSEEK_API_KEY",
                base="https://api.deepseek.com/v1", timeout=300,
                stream=True, show_thinking=True, auto_chunk=auto_chunk)
        finally:
            try:
                _SEMA_DEEPSEEK.release()
            except Exception:
                pass

    def run_doubao():
        _SEMA_DOUBAO.acquire()
        try:
            run_llm_agent(
                agent="doubao_from_source", provider="openai_compat",
                system_prompts=[prompt_path], inputs=[source_path],
                output=doubao_path, key_env="ARK_API_KEY",
                base="https://ark.cn-beijing.volces.com/api/v3", timeout=300,
                stream=True, show_thinking=True, auto_chunk=auto_chunk)
        finally:
            try:
                _SEMA_DOUBAO.release()
            except Exception:
                pass

    ran_deepseek = False
    ran_doubao = False

    def _task_deepseek():
        nonlocal ran_deepseek
        ts = time.time()
        with_retries("deepseek", run_deepseek)
        agent_times["deepseek"] = time.time() - ts
        ran_deepseek = True

    def _task_doubao():
        nonlocal ran_doubao
        ts = time.time()
        with_retries("doubao", run_doubao)
        agent_times["doubao"] = time.time() - ts
        ran_doubao = True

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = []
        if not (allow_partial and db_ok):
            futs.append(pool.submit(_task_deepseek))
        if not (allow_partial and dou_ok):
            futs.append(pool.submit(_task_doubao))
        errors = []
        for fut in concurrent.futures.as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                errors.append(e)
        if errors:
            raise errors[0]

    # ---- Merge ----
    def run_merge():
        acquired: Optional[str] = None
        try:
            try:
                borrow_timeout = float(os.getenv("MERGE_BORROW_TIMEOUT_SECONDS", "1.0"))
            except Exception:
                borrow_timeout = 1.0
            try:
                got_merge = _SEMA_DEEPSEEK_MERGE.acquire(timeout=max(0.01, borrow_timeout))
            except Exception:
                got_merge = False
            if got_merge:
                acquired = 'merge'
            else:
                _SEMA_DEEPSEEK.acquire()
                acquired = 'deepseek'

            inputs = [p for p in [deepseek_path, doubao_path] if p and os.path.exists(p)]
            if not inputs:
                raise RuntimeError(f"第{ch.chapter_no}章 缺少可融合的总结稿")

            merge_extra = (
                "【章节级融合指令 --- 仅限本章】\n"
                "- 下方提供的两份输入稿是同一章节的两个不同版本，你必须输出唯一一份融合结果，禁止将两份内容叠加输出或重复呈现；\n"
                "- 仅融合并输出当前章节内容，禁止输出任何其他章节、目录或前言；\n"
                "- 顶层标题必须与输入稿中的本章标题一致（以 '## ' 开头）；\n"
                "- 冲突处理：若多稿内容存在冲突，请按 DeepSeek > Doubao 的优先级选择保留要点；\n"
                "- 输出篇幅不应显著超出任意一份输入稿；\n"
                "- 不要输出处理过程、模型自述、道歉或占位提示；\n"
                "- 若上方提示词与本指令存在冲突，以本指令为准。")
            run_llm_agent(
                agent="deepseek_merge_from_source", provider="openai_compat",
                system_prompts=[prompt_path], inputs=inputs,
                output=merged_path, key_env="DEEPSEEK_API_KEY",
                base="https://api.deepseek.com/v1", timeout=300,
                stream=True, show_thinking=True, extra=merge_extra,
                auto_chunk=False, max_tokens=32000)
        except Exception:
            fb_path = next((p for p in [deepseek_path, doubao_path] if p and os.path.exists(p)), None)
            if fb_path:
                safe_write_text(merged_path, safe_read_text(fb_path))
                with _PRINT_LOCK:
                    print(f"[merge:fallback] 深度融合失败，已采用 {os.path.basename(fb_path)}")
            else:
                raise
        finally:
            try:
                if acquired == 'merge':
                    _SEMA_DEEPSEEK_MERGE.release()
                elif acquired == 'deepseek':
                    _SEMA_DEEPSEEK.release()
            except Exception:
                pass

    current_merge_inputs = [p for p in [deepseek_path, doubao_path] if p and os.path.exists(p)]
    current_fingerprint = _compute_merge_input_fingerprint(current_merge_inputs)

    can_skip_merge = False
    if skip_merge_unchanged and merged_ok and prev_merge_fingerprint:
        if current_fingerprint == prev_merge_fingerprint and not (ran_deepseek or ran_doubao):
            can_skip_merge = True
    elif allow_partial and merged_ok and not (ran_deepseek or ran_doubao):
        can_skip_merge = (db_ok and dou_ok)

    if can_skip_merge:
        with _PRINT_LOCK:
            print(f"[{now_str()}] [merge跳过] {ch.book_name} 第{ch.chapter_no}章 {ch.title} "
                  f"{'agent输出未变' if skip_merge_unchanged else '未变且已存在'}")
    else:
        ts = time.time()
        with_retries("merge", run_merge)
        agent_times["merge"] = time.time() - ts

    merge_fp = _compute_merge_input_fingerprint(
        [p for p in [deepseek_path, doubao_path] if p and os.path.exists(p)])

    elapsed = time.time() - t0
    with _PRINT_LOCK:
        print(f"[{now_str()}] [章节完成] {ch.book_name} 第{ch.chapter_no}章 {ch.title} "
              f"用时{format_duration_cn(elapsed)} | "
              f"重试: ds={retries_map.get('deepseek',0)} db={retries_map.get('doubao',0)} "
              f"m={retries_map.get('merge',0)} | "
              f"耗时: ds={format_duration_cn(agent_times.get('deepseek',0))} "
              f"db={format_duration_cn(agent_times.get('doubao',0))} "
              f"m={format_duration_cn(agent_times.get('merge',0))}")

    return ChapterResult(
        know_id=ch.know_id, book_name=ch.book_name, chapter_no=ch.chapter_no,
        chapter_index=ch.chapter_index,
        prefix=prefix, title=ch.title,
        source_path=source_path, deepseek_path=deepseek_path,
        doubao_path=doubao_path, merged_path=merged_path,
        chapter_hash=chapter_hash, status="done", elapsed_sec=elapsed,
        retries=retries_map, agent_times=agent_times)

# ============================================================
# 进度管理
# ============================================================

def load_progress(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"books": {}}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return json.load(f)
    except Exception:
        return {"books": {}}

def save_progress(path: str, data: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    safe_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))

def update_progress_atomic(path: str, mutator) -> Dict[str, Any]:
    try:
        ensure_dir(os.path.dirname(path) or ".")
    except Exception:
        pass
    lock_path = f"{path}.lock"
    with SimpleFileLock(lock_path, timeout=120, poll_interval=0.1):
        data = load_progress(path)
        mutator(data)
        save_progress(path, data)
        return data

# ============================================================
# 标题骨架提取
# ============================================================

RE_HEADING_LINE = re.compile(r'^ {0,3}(#{1,6})\s+(.+)')

def _classify_heading_role(title: str) -> str:
    """根据标题文字预分类角色"""
    title_stripped = title.strip()
    if re.search(r'(篇|单元|模块|部分|编)\s*$', title_stripped) or \
       re.search(r'^第\s*[一二三四五六七八九十\d]+\s*(篇|单元|模块|部分|编)', title_stripped):
        return "container"
    if re.search(r'第\s*[一二三四五六七八九十\d]+\s*(章|讲|课)', title_stripped):
        return "chapter"
    if re.search(r'^\d+\.\d+|^第\s*[一二三四五六七八九十\d]+\s*节', title_stripped):
        return "section"
    return "unknown"

def _count_sub_chapters(skeleton: List[dict], container_idx: int) -> int:
    """统计 container 下方有多少个 chapter（到下一个 container 为止）"""
    count = 0
    for i in range(container_idx + 1, len(skeleton)):
        role = skeleton[i].get("role_hint", "unknown")
        if role == "container":
            break
        if role == "chapter":
            count += 1
    return count

def extract_heading_skeleton(text: str) -> List[dict]:
    """扫描全文提取标题骨架，跳过代码块内的 #"""
    lines = text.splitlines()
    heading_positions: list = []

    in_code_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        m = RE_HEADING_LINE.match(line)
        if m:
            heading_positions.append((i, len(m.group(1)), m.group(2).strip()))

    skeleton = []
    for idx, (line_no, level, title) in enumerate(heading_positions):
        next_line = heading_positions[idx + 1][0] if idx + 1 < len(heading_positions) \
                    else len(lines)
        body_lines = lines[line_no + 1: next_line]
        body_text = "\n".join(body_lines).strip()
        effective_len = len(re.sub(r'\s+', '', body_text))

        prev_level = heading_positions[idx - 1][1] if idx > 0 else None

        entry = {
            "index": idx,
            "line": line_no,
            "title": title,
            "current_level": level,
            "prev_level": prev_level,
            "role_hint": _classify_heading_role(title),
            "body_length": effective_len,
            "context_preview": body_text[:80],
            "sub_chapter_count": 0,
        }
        skeleton.append(entry)

    # 注入子章统计
    for i, h in enumerate(skeleton):
        if h["role_hint"] == "container":
            h["sub_chapter_count"] = _count_sub_chapters(skeleton, i)

    return skeleton

# ============================================================
# AI 标题层级规范化
# ============================================================

def ai_normalize_headings(skeleton: List[dict], know_id: int,
                          book_name: str, final_root: str,
                          book_dir: str = "") -> List[dict]:
    """将标题骨架发送给 DeepSeek，获取 level 分配方案"""
    if not skeleton:
        return []

    if not book_dir:
        book_dir = final_root
    ensure_dir(book_dir)
    sk_path = os.path.join(book_dir, f"_heading_sk_{know_id}.json")
    prompt_path = os.path.join(_BASE, "md", "标题层级排序.md")
    out_path = os.path.join(book_dir, f"_heading_fix_{know_id}.json")

    # 精简骨架：只去掉 context_preview；body_length 必须保留
    # （提示词"层级规则 2.3 / 边界情况"依赖 body_length 判断 unknown 标题）
    slim_sk = [{k: v for k, v in h.items()
                if k not in ("context_preview",)}
               for h in skeleton]
    safe_write_text(sk_path, json.dumps(slim_sk, ensure_ascii=False, indent=2))

    with _PRINT_LOCK:
        print(f"[{time.strftime("%H:%M:%S")}] [标题规范化] {know_id}-{book_name}: "
              f"共 {len(skeleton)} 个标题 → AI 分析中...")

    run_agent(argparse.Namespace(
        agent="heading_normalizer",
        provider="openai_compat",
        system_prompts=[prompt_path],
        inputs=[sk_path],
        output=out_path,
        key_env="DEEPSEEK_API_KEY",
        base="https://api.deepseek.com/v1",
        timeout=1200,
        stream=True,
        show_thinking=True,
        model="deepseek-v4-pro",
        max_tokens=32000,
        temperature=0.2,
        api_key=None,
        base_env=None,
        extra=None,
        auto_chunk=False,
        inputs_file=None,
        inputs_glob=None,
        input_dir=None,
        exts=None,
    ))

    raw = safe_read_text(out_path).strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    try:
        fixes = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            fixes = json.loads(m.group())
        else:
            with _PRINT_LOCK:
                print(f"[{time.strftime("%H:%M:%S")}] [标题规范化失败] {know_id}-{book_name}: "
                      f"无法解析 AI 输出，跳过")
            return []

    with _PRINT_LOCK:
        changed = sum(1 for f in fixes
                      if 0 <= f.get("index", -1) < len(skeleton)
                      and f.get("level") != skeleton[f["index"]]["current_level"])
        print(f"[{time.strftime("%H:%M:%S")}] [标题规范化完成] {know_id}-{book_name}: "
              f"{len(fixes)} 个标题, {changed} 个被修正")

    return fixes

def apply_heading_levels(text: str, fixes: List[dict]) -> str:
    """按 AI 输出的 level 方案原位替换 # 数量，不改标题文字。

    以 AI 的 index 定位：用与 extract_heading_skeleton 相同的规则重新扫描
    文本得到"顺序 -> 行号"，再按下标改写。不信任 AI 输出的 line 字段
    （实测 AI 会写错/重复 line，导致修复落在错误行或丢失）。
    """
    if not fixes:
        return text
    lines = text.splitlines()

    # 与 extract_heading_skeleton 完全一致的扫描，得到标题顺序 -> 行号
    heading_line_nos: List[int] = []
    in_code_block = False
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if RE_HEADING_LINE.match(ln):
            heading_line_nos.append(i)

    level_by_index = {f["index"]: f["level"]
                      for f in fixes if "index" in f and "level" in f}
    for idx, new_level in level_by_index.items():
        if not (0 <= idx < len(heading_line_nos)):
            continue
        ln_no = heading_line_nos[idx]
        m = RE_HEADING_LINE.match(lines[ln_no])
        if m:
            new_level = max(1, min(6, int(new_level)))
            lines[ln_no] = f"{'#' * new_level} {m.group(2)}"

    return "\n".join(lines)

# ============================================================
# 成品拼接
# ============================================================

def stitch_one_book(know_id: int, book_name: str,
                    chapter_results: List[ChapterResult],
                    final_root: str,
                    input_root: str = "") -> str:
    chapter_results = sorted(chapter_results, key=lambda cr: cr.chapter_index)
    parts = []
    for cr in chapter_results:
        if os.path.exists(cr.merged_path):
            parts.append(safe_read_text(cr.merged_path))
    text = "\n\n\n".join(parts)
    ensure_dir(final_root)

    # heading JSON 存入 精简版-按章/{book_id}/ 下
    book_dir = os.path.join(input_root, str(know_id)) if input_root else final_root
    ensure_dir(book_dir)

    # --- 自动标题层级规范化 ---
    skeleton = extract_heading_skeleton(text)
    if skeleton:
        fixes = ai_normalize_headings(skeleton, know_id, book_name, final_root, book_dir)
        if fixes:
            text = apply_heading_levels(text, fixes)

    out_path = os.path.join(final_root, f"{know_id}-2 {book_name}.md")
    safe_write_text(out_path, text)
    return os.path.abspath(out_path)

# ============================================================
# 进度记录辅助
# ============================================================

def _record_success(res: ChapterResult, merge_fingerprint: str, progress_path: str) -> None:
    bk = str(res.know_id)
    ck = str(res.chapter_index)
    def _update(d):
        b = d.setdefault("books", {}).setdefault(bk, {})
        cm = b.setdefault("chapters", {})
        cr = cm.get(ck, {})
        cr.update({
            "status": res.status,
            "chapter_no": res.chapter_no,
            "chapter_index": res.chapter_index,
            "chapter_hash": res.chapter_hash,
            "merge_fingerprint": merge_fingerprint,
            "artifacts": {
                "source": res.source_path,
                "deepseek": res.deepseek_path,
                "doubao": res.doubao_path,
                "merged": res.merged_path,
            },
            "retries": res.retries,
            "agent_times": {},
        })
        cm[ck] = cr
        b["chapters"] = cm
        d["books"][bk] = b
    update_progress_atomic(progress_path, _update)

def _record_failure(ch: ChapterSlice, error_msg: str, progress_path: str) -> None:
    bk = str(ch.know_id)
    ck = str(ch.chapter_index)
    def _update(d):
        b = d.setdefault("books", {}).setdefault(bk, {})
        cm = b.setdefault("chapters", {})
        cr = cm.get(ck, {})
        cr.setdefault("errors", []).append(error_msg)
        cr["status"] = "failed"
        cm[ck] = cr
        b["chapters"] = cm
        d["books"][bk] = b
    update_progress_atomic(progress_path, _update)

# ============================================================
# 主流水线
# ============================================================

def run_pipeline(
    input_root: str,
    final_root: str,
    progress_path: str,
    prompt_path: str,
    force: bool = False,
    ids: Optional[List[int]] = None,
    max_workers: int = 6,
    chapter_max_workers: int = 7,
    retries: int = 2,
    retry_backoff: int = 10,
    incremental_resume: bool = False,
    resume_partial: bool = False,
    skip_merge_unchanged: bool = False,
    auto_chunk: bool = False,
    split_by_section: bool = False,
    limit_deepseek: Optional[int] = None,
    limit_doubao: Optional[int] = None,
) -> None:
    """三阶段流水线：
    Phase 1 - 扫描 & 一次性拆分所有章节
    Phase 2 - 全部章节并发处理 (DeepSeek + Doubao -> Merge)
    Phase 3 - 按书籍拼接成品
    """
    _run_t0 = time.time()
    target_ids = set(ids) if ids else None

    set_provider_limits(deepseek=limit_deepseek, doubao=limit_doubao)
    actual_workers = chapter_max_workers if chapter_max_workers > 0 else max_workers
    ds_lim = limit_deepseek or _safe_int_env("DEEPSEEK_MAX_CONCURRENCY", 3)
    db_lim = limit_doubao or _safe_int_env("DOUBAO_MAX_CONCURRENCY", 3)

    # ====== Phase 1 ======
    with _PRINT_LOCK:
        print(f"[{now_str()}] ===== Phase 1: 全量拆分 =====")
        print(f"[{now_str()}] 限制: DeepSeek={ds_lim} Doubao={db_lim}")
        print(f"[{now_str()}] 章节并发={actual_workers}  重试={retries}次  退避={retry_backoff}s")
        for flag, name in [(incremental_resume, "增量恢复"), (resume_partial, "部分恢复"),
                           (skip_merge_unchanged, "跳过未变merge"), (auto_chunk, "自动分块")]:
            if flag:
                print(f"[{now_str()}] {name}: 开启")

    all_chapters = scan_and_split_all(input_root, target_ids, split_by_section)
    if not all_chapters:
        with _PRINT_LOCK:
            print(f"[{now_str()}] [结束] 没有找到任何章节")
        return

    book_ids = sorted(set(ch.know_id for ch in all_chapters))
    prefix_by_key = {(c.know_id, c.chapter_index): c.prefix for c in all_chapters}
    hash_by_key = {(c.know_id, c.chapter_index): compute_hash(c.content) for c in all_chapters}
    total = len(all_chapters)
    with _PRINT_LOCK:
        print(f"[{now_str()}] [拆分汇总] {len(book_ids)}本书，共{total}章")

    progress = load_progress(progress_path)

    def _init_progress(d: Dict[str, Any]) -> None:
        books = d.setdefault("books", {})
        for kid in book_ids:
            bk = str(kid)
            ch_list = [c for c in all_chapters if c.know_id == kid]
            bname = ch_list[0].book_name if ch_list else ""
            if bk not in books:
                books[bk] = {"know_id": kid, "book_name": bname, "status": "pending",
                             "chapters": {}, "final_path": None, "elapsed_sec_total": 0.0,
                             "book_hash": None, "chapters_total": len(ch_list)}
            else:
                books[bk].setdefault("chapters_total", len(ch_list))
                books[bk].setdefault("chapters", {})
    update_progress_atomic(progress_path, _init_progress)
    progress = load_progress(progress_path)

    # ====== Phase 2 ======
    with _PRINT_LOCK:
        print(f"[{now_str()}] ===== Phase 2: 全章并发 (workers={actual_workers}) =====")

    results: List[ChapterResult] = []
    completed = 0
    completed_lock = threading.Lock()

    def process_and_record(ch: ChapterSlice) -> Optional[ChapterResult]:
        nonlocal completed
        local_progress = load_progress(progress_path)

        if resume_partial or incremental_resume:
            bk = str(ch.know_id)
            ck = str(ch.chapter_index)
            book_rec = local_progress.get("books", {}).get(bk, {})
            ch_rec = book_rec.get("chapters", {}).get(ck, {})
            if ch_rec.get("status") == "done" and ch_rec.get("artifacts"):
                art = ch_rec["artifacts"]
                merged = art.get("merged", "")
                expected_merged = os.path.join(ch.chapter_dir, f"{ch.prefix}.merged_from_source.md")
                if (merged and merged == expected_merged and os.path.exists(merged)
                        and ch_rec.get("chapter_hash") == compute_hash(ch.content)):
                    with _PRINT_LOCK:
                        print(f"[{now_str()}] [跳过] {ch.book_name} 第{ch.chapter_no}章 上次已完成")
                    with completed_lock:
                        completed += 1
                    return ChapterResult(
                        know_id=ch.know_id, book_name=ch.book_name,
                        chapter_no=ch.chapter_no, chapter_index=ch.chapter_index,
                        prefix=ch.prefix, title=ch.title,
                        source_path=art.get("source", ""),
                        deepseek_path=art.get("deepseek", ""),
                        doubao_path=art.get("doubao", ""),
                        merged_path=merged,
                        chapter_hash=ch_rec.get("chapter_hash", ""),
                        status="done", elapsed_sec=0.0,
                        retries=ch_rec.get("retries", {}),
                        agent_times=ch_rec.get("agent_times", {}))

        try:
            res = process_one_chapter(
                ch=ch, prompt_path=prompt_path, retries=retries,
                retry_backoff=retry_backoff, incremental_resume=incremental_resume,
                skip_merge_unchanged=skip_merge_unchanged, force=force,
                auto_chunk=auto_chunk, progress=local_progress)
        except Exception as e:
            with _PRINT_LOCK:
                print(f"[{now_str()}] [章节错误] {ch.book_name} 第{ch.chapter_no}章: {e}")
            _record_failure(ch, str(e), progress_path)
            with completed_lock:
                completed += 1
            return None

        current_inputs = [p for p in [res.deepseek_path, res.doubao_path]
                          if p and os.path.exists(p)]
        merge_fp = _compute_merge_input_fingerprint(current_inputs)
        _record_success(res, merge_fp, progress_path)

        with completed_lock:
            completed += 1
            if completed % 10 == 0 or completed == total:
                with _PRINT_LOCK:
                    elapsed_total = format_duration_cn(time.time() - _run_t0)
                    print(f"[{now_str()}] [进度] {completed}/{total}章 已运行{elapsed_total}")
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=actual_workers) as pool:
        future_to_ch = {pool.submit(process_and_record, ch): ch for ch in all_chapters}
        for fut in concurrent.futures.as_completed(future_to_ch):
            r = fut.result()
            if r is not None:
                results.append(r)

    # ====== Phase 3 ======
    with _PRINT_LOCK:
        print(f"[{now_str()}] ===== Phase 3: 拼接成品 =====")

    book_groups: Dict[int, List[ChapterResult]] = {}
    for r in results:
        book_groups.setdefault(r.know_id, []).append(r)

    if resume_partial:
        fresh_progress = load_progress(progress_path)
        for kid in book_ids:
            bk = str(kid)
            book_rec = fresh_progress.get("books", {}).get(bk, {})
            existing_ids = {r.chapter_index for r in book_groups.get(kid, [])}
            for ck, ch_rec in book_rec.get("chapters", {}).items():
                cidx = int(ck)
                expected_prefix = prefix_by_key.get((kid, cidx), "")
                if cidx not in existing_ids and ch_rec.get("status") == "done" and ch_rec.get("artifacts"):
                    art = ch_rec["artifacts"]
                    merged = art.get("merged", "")
                    expected_merged = os.path.join(input_root, str(kid), f"{expected_prefix}.merged_from_source.md")
                    if (merged and merged == expected_merged and os.path.exists(merged)
                            and ch_rec.get("chapter_hash") == hash_by_key.get((kid, cidx))):
                        ch_name = ch_rec.get("book_name", "")
                        book_groups.setdefault(kid, []).append(ChapterResult(
                            know_id=kid, book_name=ch_name or "",
                            chapter_no=ch_rec.get("chapter_no", cidx),
                            chapter_index=cidx,
                            prefix=expected_prefix, title="",
                            source_path=art.get("source", ""),
                            deepseek_path=art.get("deepseek", ""),
                            doubao_path=art.get("doubao", ""),
                            merged_path=merged,
                            chapter_hash=ch_rec.get("chapter_hash", ""),
                            status="done", elapsed_sec=0.0,
                            retries=ch_rec.get("retries", {}),
                            agent_times=ch_rec.get("agent_times", {})))

    phase3_stats = []
    for kid in sorted(book_groups.keys()):
        ch_results = book_groups[kid]
        bname = ch_results[0].book_name if ch_results else ""
        total_ch = len([c for c in all_chapters if c.know_id == kid])
        succ_ch = len(ch_results)

        if succ_ch == 0:
            def _mark_failed(d):
                d["books"][str(kid)]["status"] = "failed"
                d["books"][str(kid)]["chapters_succeeded"] = 0
            update_progress_atomic(progress_path, _mark_failed)
            phase3_stats.append((kid, bname, "failed", 0, total_ch, ""))
            continue

        final_path = stitch_one_book(kid, bname, ch_results, final_root, input_root)
        book_hash_val = compute_hash("|".join(
            cr.chapter_hash for cr in sorted(ch_results, key=lambda x: x.chapter_index)))
        status = "done" if succ_ch == total_ch else "partial"

        def _finalize(d):
            b = d["books"].setdefault(str(kid), {})
            b.update({"status": status, "final_path": final_path,
                      "book_hash": book_hash_val, "chapters_total": total_ch,
                      "chapters_succeeded": succ_ch})
        update_progress_atomic(progress_path, _finalize)
        phase3_stats.append((kid, bname, status, succ_ch, total_ch, final_path))

    _run_t1 = time.time()
    lines = [
        f"开始: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_run_t0))}  "
        f"结束: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_run_t1))}  "
        f"总耗时: {format_duration_cn(_run_t1 - _run_t0)}",
        f"限制: DS={ds_lim} DB={db_lim}  "
        f"书籍: {len(book_ids)}本  章节: {total}章  成功: {len(results)}章  失败: {total - len(results)}章",
        "",
    ]
    for kid, bname, status, succ, total_ch, fp in phase3_stats:
        icon = "OK" if status == "done" else ("~" if status == "partial" else "X")
        lines.append(f"  [{icon}] {kid} {bname}: {succ}/{total_ch}章 {status}")

    with _PRINT_LOCK:
        print("\n" + "=" * 60)
        print("\n".join(lines))
        print("=" * 60)

# ============================================================
# CLI
# ============================================================

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def main():
    parser = argparse.ArgumentParser(description="精简版教材处理流水线 v2")
    parser.add_argument("--input-root",
        default=os.path.join(_BASE, "exports", "multi_agent", "final", "精简版-按章"))
    parser.add_argument("--final-root",
        default=os.path.join(_BASE, "exports", "multi_agent", "final", "精简版"))
    parser.add_argument("--progress",
        default=os.path.join(_BASE, "exports", "multi_agent", "final", "精简版-按章", "progress.json"))
    parser.add_argument("--prompt",
        default=os.path.join(_BASE, "md", "精简版提示词1.md"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ids", type=int, nargs="*")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--chapter-max-workers", type=int, default=7)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff", type=int, default=10)
    parser.add_argument("--incremental-resume", action="store_true")
    parser.add_argument("--resume-partial", action="store_true")
    parser.add_argument("--skip-merge-unchanged", action="store_true")
    parser.add_argument("--auto-chunk", action="store_true")
    parser.add_argument("--split-by-section", action="store_true")
    parser.add_argument("--limit-deepseek", type=int, default=None)
    parser.add_argument("--limit-doubao", type=int, default=None)

    args = parser.parse_args()

    if not os.path.exists(args.prompt):
        print(f"[错误] 提示词文件不存在：{args.prompt}")
        sys.exit(1)

    run_pipeline(
        input_root=args.input_root, final_root=args.final_root,
        progress_path=args.progress, prompt_path=args.prompt,
        force=args.force, ids=args.ids,
        max_workers=args.max_workers,
        chapter_max_workers=args.chapter_max_workers,
        retries=args.retries, retry_backoff=args.retry_backoff,
        incremental_resume=args.incremental_resume,
        resume_partial=args.resume_partial,
        skip_merge_unchanged=args.skip_merge_unchanged,
        auto_chunk=args.auto_chunk,
        split_by_section=args.split_by_section,
        limit_deepseek=args.limit_deepseek,
        limit_doubao=args.limit_doubao)

if __name__ == "__main__":
    main()
