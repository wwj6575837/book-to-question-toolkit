from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Reuse runner utilities for safe IO
from .utils import safe_read_text, safe_write_text, SimpleFileLock
from .agent_runner import run_agent, _PRINT_LOCK
import threading
# Provider-level concurrency limits (tunable via env vars)
def _safe_int_env(key: str, default: int) -> int:
    """Safely parse environment variable as positive integer."""
    try:
        val = int(os.getenv(key, str(default)))
        return max(1, val)  # Ensure at least 1
    except (ValueError, TypeError):
        return default

_SEMA_KIMI = threading.Semaphore(_safe_int_env("KIMI_MAX_CONCURRENCY", 2))
_SEMA_DEEPSEEK = threading.Semaphore(_safe_int_env("DEEPSEEK_MAX_CONCURRENCY", 3))
_SEMA_DOUBAO = threading.Semaphore(_safe_int_env("DOUBAO_MAX_CONCURRENCY", 3))
_SEMA_DEEPSEEK_MERGE = threading.Semaphore(_safe_int_env("DEEPSEEK_MERGE_MAX_CONCURRENCY", 2))

def set_provider_concurrency_limits(kimi: Optional[int] = None,
                                    deepseek: Optional[int] = None,
                                    doubao: Optional[int] = None) -> None:
    """Dynamically adjust provider-level concurrency semaphores.
    Call before launching chapter tasks. Passing None keeps current value.
    """
    global _SEMA_KIMI, _SEMA_DEEPSEEK, _SEMA_DOUBAO
    try:
        if kimi is not None:
            _SEMA_KIMI = threading.Semaphore(max(1, int(kimi)))
        if deepseek is not None:
            _SEMA_DEEPSEEK = threading.Semaphore(max(1, int(deepseek)))
        if doubao is not None:
            _SEMA_DOUBAO = threading.Semaphore(max(1, int(doubao)))
    except Exception:
        # Keep existing semaphores on invalid inputs
        pass

# -------------------------------
# Regex helpers for filename parsing
# -------------------------------
# NEW: Separate pattern for "篇" (part_section) - must be checked BEFORE chapter
RE_PART_SECTION = re.compile(r"第\s*([一二三四五六七八九十百千万\d]+)\s*篇")
# NEW: Pattern for "模块" (module) - top-level hierarchy
RE_MODULE = re.compile(r"模块\s*([一二三四五六七八九十百千万\d]+)")
# NEW: Pattern for "项目" (project) - second-level hierarchy
RE_PROJECT = re.compile(r"项目\s*([一二三四五六七八九十百千万\d]+)")
# Accept both '第X(章|单元|讲|编|课)' and '(章|单元|讲|编|课)X' - EXCLUDING "篇" and "模块"
RE_CHAPTER = re.compile(
    r"(?:第\s*([一二三四五六七八九十百千万\d]+)\s*(章|单元|讲|编|课)"
    r"|(?:章|单元|讲|编|课)\s*([一二三四五六七八九十百千万\d]+))"
)
RE_SECTION = re.compile(r"第\s*([一二三四五六七八九十百千万\d]+)\s*节")
RE_NUM_TOKEN = re.compile(r"(\d+)")
RE_SUBNUM = re.compile(r"(?:_|\b|（|\()(\d+)(?:\.|）|\))")
# New: recognize reorganizer's part segment and common suffixes
RE_PART_SEG = re.compile(r"(?:^|[-_\s])部分\s*(\d{1,3})", re.UNICODE)
RE_VARIANT_SUFFIX = re.compile(r"(_full|_lite)(?=$|[-_])", re.IGNORECASE)
RE_DUP_SUFFIX = re.compile(r"-dup\d{2,}$", re.IGNORECASE)
RE_APPENDIX = re.compile(r"附录\s*([A-Za-z])", re.IGNORECASE)

# Special chapters mapping aligned with reorganizer
SPECIAL_CHAPTERS = {
    "序言": -3, "序": -3,
    "前言": -2,
    "引言": -1, "导论": -1,
    "绪论": 0, "导言": 0,
}

CHN_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10,
}


def _collect_chunk_stats(chapter_dir: str, prefix: str, max_retries: int = 3) -> Dict[str, Any]:
    """收集某个章节的AutoChunk统计信息（线程安全，带重试机制）。
    
    Args:
        chapter_dir: 章节目录
        prefix: 文件前缀（可以是精确前缀或模糊匹配用）
        max_retries: 读取失败时的最大重试次数（防止读取到正在写入的不完整文件）
    
    Returns:
        各agent的chunk统计信息字典
    """
    import glob
    stats = {}  # 每次调用创建新的dict，避免共享状态
    
    for agent_name in ["deepseek_from_source", "doubao_from_source"]:
        # 优先精确匹配
        stats_path = os.path.join(chapter_dir, f"{prefix}.{agent_name}.chunk_stats.json")
        
        # 如果精确匹配不存在，尝试模糊匹配（处理文件名包含章节标题的情况）
        if not os.path.exists(stats_path):
            # 提取bid和book_name用于模糊匹配
            # prefix格式可能是: "1606-生物学七年级下册-8" 或 "1606-生物学七年级下册-1-2"
            try:
                # 构造模糊匹配模式: {bid}-{book_name}-*章*.{agent}.chunk_stats.json
                parts = prefix.split("-")
                if len(parts) >= 3:
                    bid = parts[0]
                    # 从prefix中提取章节号
                    ch_part = parts[-1]  # 最后一部分是章节号
                    # 尝试不同的模式
                    patterns = [
                        os.path.join(chapter_dir, f"{prefix}*.{agent_name}.chunk_stats.json"),
                        os.path.join(chapter_dir, f"*-第{ch_part.zfill(2)}章*.{agent_name}.chunk_stats.json"),
                        os.path.join(chapter_dir, f"*第{ch_part}章*.{agent_name}.chunk_stats.json"),
                    ]
                    for pattern in patterns:
                        matches = glob.glob(pattern)
                        if matches:
                            stats_path = matches[0]  # 取第一个匹配
                            break
            except Exception:
                pass
        
        if os.path.exists(stats_path):
            # 带重试的读取（处理可能的写入竞争）
            for attempt in range(max_retries):
                try:
                    content = safe_read_text(stats_path)
                    if not content or not content.strip():
                        # 文件为空，可能正在写入，稍等后重试
                        if attempt < max_retries - 1:
                            time.sleep(0.1 * (attempt + 1))
                            continue
                        break
                    import json
                    agent_stats = json.loads(content)
                    if agent_stats.get("auto_chunk_enabled"):
                        # 验证必要字段存在
                        if "health_score" in agent_stats and "num_chunks" in agent_stats:
                            # 创建新的dict副本，避免引用共享
                            stats[agent_name] = dict(agent_stats)
                            break
                        elif attempt < max_retries - 1:
                            time.sleep(0.1 * (attempt + 1))
                            continue
                except json.JSONDecodeError:
                    # JSON解析失败，可能文件正在写入，重试
                    if attempt < max_retries - 1:
                        time.sleep(0.1 * (attempt + 1))
                        continue
                except Exception:
                    break
    return stats


def format_duration_cn(seconds: float) -> str:
    """将秒格式化为中文时长：如 75 -> '1分15秒'，3700 -> '1小时1分40秒'"""
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


def now_str() -> str:
    """Return local timestamp 'YYYY-MM-DD HH:MM:SS'."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _build_hierarchy_desc(part_num: int, module_num: int, project_num: int, chapter_num: int) -> str:
    """构建层级描述字符串，用于日志输出。
    根据实际存在的层级生成类似 '第X篇-模块X-项目X' 的格式。
    """
    parts = []
    if part_num > 0:
        parts.append(f"第{part_num}篇")
    if module_num > 0:
        parts.append(f"模块{module_num}")
    if project_num > 0:
        parts.append(f"项目{project_num}")
    elif chapter_num > 0:
        # 如果没有project，但有chapter，显示章节号
        parts.append(f"第{chapter_num}章")
    
    return "-".join(parts) if parts else f"第{chapter_num}章"


def cn_to_int(s: str) -> int:
    s = s.strip()
    if not s:
        return 0
    # If it's digits already
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return 0
    # Very simple Chinese numeral to int (up to 99 is enough for chapters/sections)
    total = 0
    if "十" in s:
        parts = s.split("十")
        left = parts[0]
        right = parts[1] if len(parts) > 1 else ""
        a = CHN_MAP.get(left, 1 if left == "" else 0)  # 十 -> 10; 二十 -> 2*10; 十五 -> 1*10 + 5
        b = CHN_MAP.get(right, 0) if right != "" else 0
        total = a * 10 + b
    else:
        # 单个数字
        total = CHN_MAP.get(s, 0)
    return int(total) if isinstance(total, int) else 0


@dataclass
class FileMeta:
    path: str
    name: str
    part_section_num: int  # NEW: 篇号 (0 if no part_section)
    module_num: int  # NEW: 模块号 (0 if no module)
    project_num: int  # NEW: 项目号 (0 if no project)
    chapter_no: int
    section_nums: List[int]
    subnum: int
    level_key: int  # 0: chapter-only; 1: single section; 2: summary (multiple sections)
    orig_id: int


def extract_part_section_no(name: str) -> int:
    """Extract part section number (篇号) from filename."""
    m = RE_PART_SECTION.search(name)
    if not m:
        return 0
    raw = m.group(1)
    if not raw:
        return 0
    return cn_to_int(raw) if not raw.isdigit() else int(raw)


def extract_module_no(name: str) -> int:
    """Extract module number (模块号) from filename."""
    m = RE_MODULE.search(name)
    if not m:
        return 0
    raw = m.group(1)
    if not raw:
        return 0
    return cn_to_int(raw) if not raw.isdigit() else int(raw)


def extract_project_no(name: str) -> int:
    """Extract project number (项目号) from filename."""
    m = RE_PROJECT.search(name)
    if not m:
        return 0
    raw = m.group(1)
    if not raw:
        return 0
    return cn_to_int(raw) if not raw.isdigit() else int(raw)


def extract_chapter_no(name: str) -> int:
    """Extract chapter number, avoiding confusion with part_section (篇) and module (模块)."""
    m = RE_CHAPTER.search(name)
    if not m:
        return -1
    # group(1) for '第X(章|单元|讲|编|课)', group(3) for '(章|单元|讲|编|课)X'
    raw = m.group(1) or m.group(3)
    if not raw:
        return -1
    return cn_to_int(raw) if not raw.isdigit() else int(raw)


def extract_section_nums(name: str) -> List[int]:
    nums: List[int] = []
    for m in RE_SECTION.finditer(name):
        raw = m.group(1)
        if raw.isdigit():
            nums.append(int(raw))
        else:
            nums.append(cn_to_int(raw))
    # If filename follows reorganizer's pattern with '-部分NN', prefer that as the section indicator
    try:
        m_part = RE_PART_SEG.search(name)
        if m_part:
            val = int(m_part.group(1))
            if val >= 0:
                return [val]
    except Exception:
        pass
    return [n for n in nums if n >= 0]


def extract_subnum(name: str) -> int:
    m = RE_SUBNUM.search(name)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def extract_orig_id(name: str) -> int:
    # Use first number token in filename as a stable original id fallback
    m = RE_NUM_TOKEN.search(name)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    return 0


def build_file_meta(path: str) -> Optional[FileMeta]:
    name = os.path.basename(path)
    if not name.lower().endswith(".md"):
        return None
    if name.startswith("000-"):
        return None
    base = name[:-3]
    
    # Strip '_full' suffix if present (support reorganizer's full chapter files)
    original_base = base
    if base.endswith("_full"):
        base = base[:-5]
    
    # NEW: Extract hierarchical identifiers in order
    part_sec_no = extract_part_section_no(base)  # 篇
    module_no = extract_module_no(base)  # 模块
    project_no = extract_project_no(base)  # 项目
    
    # Determine effective chapter number based on hierarchy presence
    # Priority: project > module > chapter
    ch_no = -1
    if project_no > 0:
        # If project exists, use it as chapter-level identifier
        ch_no = project_no
    elif module_no > 0:
        # If only module exists (no project), use module as chapter
        ch_no = module_no
    else:
        # Fall back to traditional chapter extraction
        ch_no = extract_chapter_no(base)
    
    if ch_no < 0:
        # Try special chapters (前言/序言/绪论/引言/导论/导言) or 附录X
        try:
            # Appendix like '附录A'
            m_app = RE_APPENDIX.search(base)
            if m_app:
                letter = m_app.group(1).upper()
                ch_no = 900 + (ord(letter) - ord('A'))
            else:
                # 修正:精确匹配特殊章节名,避免子字符串误匹配(如"秩序"中的"序")
                for key, val in SPECIAL_CHAPTERS.items():
                    # 精确匹配:base等于特殊名,或者base以特殊名开头(用分隔符分隔)
                    if base == key or base.startswith(key + ' ') or base.startswith(key + '_') or base.startswith(key + '-'):
                        ch_no = val
                        break
        except Exception:
            ch_no = -1
    if ch_no < 0:
        # Not a chapter content file
        return None
    # Prefer reorganizer's '-部分NN' as the within-chapter ordering if present
    sec_nums = extract_section_nums(base)
    subnum = extract_subnum(base)
    level_key = 0 if len(sec_nums) == 0 else (1 if len(sec_nums) == 1 else 2)
    orig_id = extract_orig_id(base)
    return FileMeta(
        path=path,
        name=original_base,  # Keep original name including '_full' suffix
        part_section_num=part_sec_no,
        module_num=module_no,
        project_num=project_no,
        chapter_no=ch_no,
        section_nums=sec_nums,
        subnum=subnum,
        level_key=level_key,
        orig_id=orig_id,
    )


def sort_within_chapter(items: List[FileMeta]) -> List[FileMeta]:
    """Sort files within the same chapter by section, subnum, etc."""
    def key(it: FileMeta) -> Tuple:
        if it.level_key == 0:
            sec_key = 0
        elif it.level_key == 1:
            sec_key = it.section_nums[0] if it.section_nums else 0
        else:
            sec_key = max(it.section_nums) if it.section_nums else 0
        return (
            it.level_key,  # 章级(0) < 单节(1) < 汇总(2)
            sec_key,
            it.subnum,
            # tags lexicographic not available; fallback empty
            "",
            it.orig_id,
            it.name,
        )

    return sorted(items, key=key)


def group_by_chapter(files: List[FileMeta]) -> Dict[str, List[FileMeta]]:
    """Group files by composite key '<part>-<module>-<project>' and sort within each group.
    This prevents collapsing chapters with the same chapter_no across different hierarchies.
    The key format is zero-padded, e.g. '02-03-005' for 第2篇-模块3-项目5.
    When module/project are absent (0), they still participate in the key to maintain uniqueness.
    """
    mp: Dict[str, List[FileMeta]] = {}
    for f in files:
        # Construct hierarchical key: part-module-chapter
        # For files with project: chapter_no already carries project_no
        # For backward compatibility with pure chapter-based files
        key = f"{max(0, int(f.part_section_num)):02d}-{max(0, int(f.module_num)):02d}-{max(0, int(f.chapter_no)):03d}"
        mp.setdefault(key, []).append(f)
    for k, lst in mp.items():
        mp[k] = sort_within_chapter(lst)
    return mp


def read_concat(paths: List[str]) -> str:
    parts: List[str] = []
    for p in paths:
        try:
            parts.append(safe_read_text(p))
        except Exception:
            parts.append("")
    return "\n\n".join(parts)


def compute_hash(text: str) -> str:
    h = hashlib.sha1()
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def sanitize_horizontal_rules(text: str) -> str:
    """Remove standalone Markdown horizontal rule lines like '---', '***', '___'.
    - Preserves table header separators (which contain '|' or ':')
    - Does not modify lines inside fenced code blocks
    """
    try:
        out: List[str] = []
        in_code = False
        for ln in text.splitlines():
            sl = ln.strip()
            # Toggle fenced code blocks (```)
            if sl.startswith("```"):
                in_code = not in_code
                out.append(ln)
                continue
            if in_code:
                out.append(ln)
                continue
            # Keep potential table separator lines: they often contain '|' or ':'
            if ('|' in sl) or (':' in sl):
                out.append(ln)
                continue
            # Remove pure horizontal rules composed only of '-', '*', '_' and whitespace
            if re.fullmatch(r"[\s\-_*]{3,}", sl):
                # but require at least 3 of the same symbol to avoid removing random dashes
                if re.fullmatch(r"\s*-{3,}\s*", sl) or re.fullmatch(r"\s*\*{3,}\s*", sl) or re.fullmatch(r"\s*_{3,}\s*", sl):
                    continue
            out.append(ln)
        return "\n".join(out)
    except Exception:
        return text

def int_to_cn(n: int) -> str:
    """Convert 1..99 to Chinese numerals used in headings (一, 二, ... 十, 十一...)."""
    try:
        n = int(n)
    except Exception:
        return ""
    if n <= 0:
        return ""
    digits = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}
    if n <= 9:
        return digits[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + digits.get(n - 10, "")
    ten, one = divmod(n, 10)
    s = digits.get(ten, "") + "十"
    if one:
        s += digits.get(one, "")
    return s


def _extract_section_from_name(base: str) -> Tuple[int, str]:
    """Try to parse '第X节-标题' from a filename base; return (section_no, title)."""
    try:
        m = re.search(r"第\s*([一二三四五六七八九十百千万\d]+)\s*节[-_\s]*(.+)", base)
        if m:
            tok = m.group(1)
            sec_no = int(tok) if tok.isdigit() else cn_to_int(tok)
            title = (m.group(2) or "").strip()
            return (max(0, sec_no), title)
    except Exception:
        pass
    # Fallback: cannot parse
    return (0, "")


def _emit_section_outputs(merged_path: str, items: List[FileMeta], chapter_dir: str) -> None:
    """Emit per-section merged files by slicing the merged chapter output at H3 (###) boundaries.
    Output filenames follow the section source filenames, e.g. '<base>.merged_from_source.md'.
    This is best-effort; if a section heading cannot be located, that section is skipped.
    """
    try:
        if not os.path.exists(merged_path):
            return
        text = safe_read_text(merged_path)
        lines = text.splitlines()
        # Collect all H3 indices
        h3_idx: List[int] = []
        for i, ln in enumerate(lines):
            if ln.strip().startswith("### "):
                h3_idx.append(i)
        def find_block(start_i: int) -> Tuple[int, int]:
            end = len(lines)
            for j in range(start_i + 1, len(lines)):
                if lines[j].strip().startswith("### "):
                    end = j
                    break
            return (start_i, end)

        # Build section targets from single-section files in items
        section_targets: List[Tuple[int, str, str]] = []  # (sec_no, sec_title, out_path)
        for fm in items:
            # Only consider files that look like single section
            sec_no, sec_title = _extract_section_from_name(fm.name)
            if sec_no <= 0:
                continue
            out_path = os.path.join(chapter_dir, f"{fm.name}.merged_from_source.md")
            section_targets.append((sec_no, sec_title, out_path))

        # For each section, try to find the matching H3 index
        for sec_no, sec_title, out_path in section_targets:
            target_cn = int_to_cn(sec_no)
            best_i = -1
            best_score = -1
            for i in h3_idx:
                s = lines[i].strip()
                score = 0
                # Match '### 第X节' (rare but try)
                try:
                    if re.match(rf"^###\s*第\s*{sec_no}\s*节", s) or (target_cn and re.match(rf"^###\s*第\s*{target_cn}\s*节", s)):
                        score += 2
                except Exception:
                    pass
                # Match '### 一、' style
                try:
                    if target_cn and re.match(rf"^###\s*{target_cn}[、,.．]", s):
                        score += 2
                except Exception:
                    pass
                # Title substring boost
                if sec_title and (sec_title in s):
                    score += 1
                if score > best_score:
                    best_score = score
                    best_i = i
            if best_i >= 0:
                a, b = find_block(best_i)
                seg = "\n".join(lines[a:b]).strip() + "\n"
                try:
                    safe_write_text(out_path, seg)
                except Exception:
                    continue
    except Exception:
        # Silent best-effort; do not fail the chapter
        return

def derive_chapter_prefix(items: List[FileMeta]) -> str:
    """Prefer a pure chapter-level filename as prefix; otherwise trim after '第X章-章名'."""
    # 1) pick a chapter-only item if exists
    cand = None
    for it in items:
        if it.level_key == 0:
            cand = it
            break
    if cand is None:
        cand = items[0]
    base = cand.name  # without .md
    # Trim any '第Y节' and after
    m = RE_CHAPTER.search(base)
    if m:
        end = m.end()
        # Base after chapter marker
        tail = base[end:]
        # If there is a '第Y节' in tail, cut before it
        ms = RE_SECTION.search(tail)
        if ms:
            cut = end + ms.start()
            base = base[:cut]
    # Strip reorganizer-specific suffixes regardless of chapter marker presence
    try:
        # Remove any '-部分NN' occurrences
        base = re.sub(r"[-_\s]部分\s*\d{1,3}", "", base)
        # Remove trailing variant markers
        base = RE_VARIANT_SUFFIX.sub("", base)
        # Remove trailing duplicate suffix
        base = RE_DUP_SUFFIX.sub("", base)
    except Exception:
        pass
    return base.rstrip("-_ ")


def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def run_llm_agent(agent: str,
                   provider: str,
                   system_prompts: List[str],
                   inputs: List[str],
                   output: str,
                   model: Optional[str] = None,
                   key_env: Optional[str] = None,
                   base: Optional[str] = None,
                   timeout: int = 300,
                   stream: bool = True,
                   show_thinking: bool = True,
                   extra: Optional[str] = None,
                   auto_chunk: bool = False) -> None:
    ns = argparse.Namespace(
        agent=agent,
        provider=provider,
        model=model,
        system_prompts=system_prompts,
        inputs=inputs,
        inputs_glob=[],
        input_dir=None,
        exts=[".md", ".txt"],
        inputs_file=None,
        output=output,
        max_tokens=16000,
        temperature=0.1,
        api_key=None,
        base=base,
        key_env=key_env,
        base_env=None,
        timeout=timeout,
        stream=stream,
        show_thinking=show_thinking,
        extra=extra,
        auto_chunk=auto_chunk,
        func=run_agent,
    )
    run_agent(ns)


def _infer_expected_h2_from_text(text: str) -> Optional[str]:
    """Infer the preferred chapter-level H2 from raw markdown text."""
    try:
        for _ln in text.splitlines():
            s = (_ln or "").strip()
            if not s:
                continue
            if s.startswith("##"):
                m = re.match(r"^##\s*(.+?)\s*$", s)
                if m and m.group(1).strip():
                    return "## " + m.group(1).strip()
            if s.startswith("#"):
                m = re.match(r"^#\s*(.+?)\s*$", s)
                if m and m.group(1).strip():
                    return "## " + m.group(1).strip()
    except Exception:
        return None
    return None


def _parse_h2_heading_line(line: str) -> Tuple[Optional[str], Optional[int], str]:
    """Parse '##' heading into (unit, number, rest_title)."""
    s = (line or "").strip()
    if not s.startswith("##"):
        return (None, None, "")
    body = re.sub(r"^##\s*", "", s).strip()
    if not body:
        return (None, None, "")
    m = re.match(r"^(?:第\s*)?([一二三四五六七八九十百千万\d]+)\s*(章|单元|模块|项目|任务|部分|篇)\s*(.*)$", body)
    if m:
        tok = m.group(1)
        unit = m.group(2)
        rest = (m.group(3) or "").strip()
        try:
            no = int(tok) if tok.isdigit() else cn_to_int(tok)
        except Exception:
            no = None
        return (unit, no, rest)
    m = re.match(r"^(章|单元|模块|项目|任务|部分|篇)\s*([一二三四五六七八九十百千万\d]+)\s*(.*)$", body)
    if m:
        unit = m.group(1)
        tok = m.group(2)
        rest = (m.group(3) or "").strip()
        try:
            no = int(tok) if tok.isdigit() else cn_to_int(tok)
        except Exception:
            no = None
        return (unit, no, rest)
    return (None, None, body)


def _fallback_expected_h2(chapter_no: int, module_num: int, project_num: int) -> str:
    if project_num > 0:
        return f"## 项目{project_num}"
    if module_num > 0:
        return f"## 模块{module_num}"
    return f"## 第{chapter_no}章"


def _same_logical_h2(line: str, expected_h2: str) -> bool:
    cur_unit, cur_no, _cur_rest = _parse_h2_heading_line(line)
    exp_unit, exp_no, _exp_rest = _parse_h2_heading_line(expected_h2)
    if not expected_h2:
        return False
    if (line or "").strip() == (expected_h2 or "").strip():
        return True
    return (
        cur_no is not None
        and exp_no is not None
        and cur_no == exp_no
        and (cur_unit == exp_unit or cur_unit is None or exp_unit is None)
    )


def _strip_matching_top_h2(text: str, expected_h2: Optional[str]) -> str:
    """Remove a leading chapter H2 when stitching split outputs."""
    if not text or not expected_h2:
        return text
    lines = text.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        if ln.strip():
            idx = i
            break
    if idx is None:
        return text
    first = lines[idx].strip()
    if not first.startswith("## "):
        return text
    if not _same_logical_h2(first, expected_h2):
        return text
    rest = lines[idx + 1:]
    while rest and not rest[0].strip():
        rest = rest[1:]
    return "\n".join(rest).strip()


def _stitch_split_outputs(input_paths: List[str], output_path: str, expected_h2: Optional[str]) -> None:
    """Stitch already-summarized split parts back into one chapter artifact."""
    parts: List[str] = []
    for p in input_paths:
        if not p or not os.path.exists(p):
            continue
        txt = safe_read_text(p).strip()
        if not txt:
            continue
        body = _strip_matching_top_h2(txt, expected_h2).strip()
        if body:
            parts.append(body)
    if expected_h2:
        stitched_body = "\n\n\n".join(parts).strip()
        text = expected_h2.strip()
        if stitched_body:
            text = f"{text}\n\n{stitched_body}"
        safe_write_text(output_path, text.rstrip() + "\n")
        return
    safe_write_text(output_path, ("\n\n\n".join(parts).strip() + "\n") if parts else "")


def _should_use_split_mode(items: List[FileMeta]) -> bool:
    """Use split mode only when filenames explicitly mark section/part slices."""
    if len(items) <= 1:
        return False
    if any(it.level_key == 0 for it in items):
        return False
    for it in items:
        name = it.name or ""
        has_explicit_section = bool(RE_SECTION.search(name))
        has_explicit_part = bool(RE_PART_SEG.search(name))
        if not (has_explicit_section or has_explicit_part):
            return False
    return True


def _safe_remove_file(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _required_artifact_paths(chapter_dir: str, prefix: str, split_mode: bool) -> List[str]:
    _ = split_mode
    return [
        os.path.join(chapter_dir, f"{prefix}.deepseek_from_source.md"),
        os.path.join(chapter_dir, f"{prefix}.doubao_from_source.md"),
        os.path.join(chapter_dir, f"{prefix}.merged_from_source.md"),
    ]


def _compute_ordered_content_hash(parts: List[Tuple[str, str]]) -> str:
    """Compute a stable hash from ordered (name, content) pairs."""
    arr: List[str] = []
    for name, text in parts:
        arr.append(f"##FILE## {name}")
        arr.append(text or "")
    return compute_hash("\n\n".join(arr))


def _infer_expected_h2_from_items(items: List[FileMeta], chapter_no: int, module_num: int, project_num: int) -> str:
    """Infer chapter-level H2 from the first split item, then fall back to hierarchy."""
    for fm in items:
        try:
            text = safe_read_text(fm.path)
        except Exception:
            text = ""
        h2 = _infer_expected_h2_from_text(text)
        if h2:
            return h2
    return _fallback_expected_h2(chapter_no, module_num, project_num)


def _process_split_bundle(book_name: str,
                          bundle_name: str,
                          chapter_dir: str,
                          prefix: str,
                          source_text: str,
                          chapter_no: int,
                          module_num: int,
                          project_num: int,
                          retries: int,
                          retry_backoff: int,
                          auto_chunk: bool,
                          enable_kimi: bool = False,
                          include_kimi_in_merge: bool = False) -> Dict[str, Any]:
    """Run the standard source->agent->merge flow for one split file."""
    source_path = os.path.join(chapter_dir, f"{prefix}.source.md")
    kimi_path = os.path.join(chapter_dir, f"{prefix}.kimi_from_source.md")
    deepseek_path = os.path.join(chapter_dir, f"{prefix}.deepseek_from_source.md")
    doubao_path = os.path.join(chapter_dir, f"{prefix}.doubao_from_source.md")
    merged_path = os.path.join(chapter_dir, f"{prefix}.merged_from_source.md")

    source_hash = compute_hash(source_text)
    source_matches_prev = False
    try:
        if os.path.exists(source_path):
            source_matches_prev = (compute_hash(safe_read_text(source_path)) == source_hash)
    except Exception:
        source_matches_prev = False
    safe_write_text(source_path, source_text)

    expected_h2 = _infer_expected_h2_from_text(source_text)
    if not expected_h2:
        expected_h2 = _fallback_expected_h2(chapter_no, module_num, project_num)

    retries_map: Dict[str, int] = {"kimi": 0, "deepseek": 0, "doubao": 0, "merge": 0}
    agent_times: Dict[str, float] = {"kimi": 0.0, "deepseek": 0.0, "doubao": 0.0, "merge": 0.0}
    prompt_v5 = os.path.join("md", "知识点总结提示词V5.1.md")
    prompt_deepseek = os.path.join("multi_agent", "prompts", "deepseek_agent.md")
    prompt_doubao = os.path.join("multi_agent", "prompts", "doubao_agent.md")
    prompt_merge = os.path.join("multi_agent", "prompts", "llm_merge_agent.md")

    def with_retries(tag: str, fn) -> None:
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
                        print(f"[{now_str()}] [分片重试] {book_name} {bundle_name} agent={tag} 第{attempt}/{retries+1}次失败：{e}；{format_duration_cn(sleep_s)}后重试")
                    time.sleep(sleep_s)
                else:
                    raise
        if last_err:
            raise last_err

    def run_kimi() -> None:
        _SEMA_KIMI.acquire()
        try:
            run_llm_agent(
                agent="kimi_from_source",
                provider="kimi",
                system_prompts=[prompt_v5, os.path.join("multi_agent", "prompts", "kimi_agent.md")],
                inputs=[source_path],
                output=kimi_path,
                timeout=300,
                stream=True,
                show_thinking=True,
                auto_chunk=auto_chunk,
            )
        finally:
            _SEMA_KIMI.release()

    def run_deepseek() -> None:
        _SEMA_DEEPSEEK.acquire()
        try:
            run_llm_agent(
                agent="deepseek_from_source",
                provider="openai_compat",
                system_prompts=[prompt_v5, prompt_deepseek],
                inputs=[source_path],
                output=deepseek_path,
                key_env="DEEPSEEK_API_KEY",
                base="https://api.deepseek.com/v1",
                timeout=300,
                stream=True,
                show_thinking=True,
                auto_chunk=auto_chunk,
            )
        finally:
            _SEMA_DEEPSEEK.release()

    def run_doubao() -> None:
        _SEMA_DOUBAO.acquire()
        try:
            run_llm_agent(
                agent="doubao_from_source",
                provider="openai_compat",
                system_prompts=[prompt_v5, prompt_doubao],
                inputs=[source_path],
                output=doubao_path,
                key_env="ARK_API_KEY",
                base="https://ark.cn-beijing.volces.com/api/v3",
                timeout=300,
                stream=True,
                show_thinking=True,
                auto_chunk=auto_chunk,
            )
        finally:
            _SEMA_DOUBAO.release()

    allow_partial = source_matches_prev
    ran_kimi = False
    ran_deepseek = False
    ran_doubao = False

    def _task_kimi() -> None:
        nonlocal ran_kimi
        ts = time.time()
        with_retries("kimi", run_kimi)
        agent_times["kimi"] = time.time() - ts
        ran_kimi = True

    def _task_deepseek() -> None:
        nonlocal ran_deepseek
        ts = time.time()
        with_retries("deepseek", run_deepseek)
        agent_times["deepseek"] = time.time() - ts
        ran_deepseek = True

    def _task_doubao() -> None:
        nonlocal ran_doubao
        ts = time.time()
        with_retries("doubao", run_doubao)
        agent_times["doubao"] = time.time() - ts
        ran_doubao = True

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = []
        if not (allow_partial and os.path.exists(deepseek_path)):
            futs.append(pool.submit(_task_deepseek))
        if not (allow_partial and os.path.exists(doubao_path)):
            futs.append(pool.submit(_task_doubao))
        if enable_kimi and not (allow_partial and os.path.exists(kimi_path)):
            futs.append(pool.submit(_task_kimi))
        errors: List[Exception] = []
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                errors.append(e)
        if errors:
            raise errors[0]

    def run_merge() -> None:
        inputs: List[str] = []
        if os.path.exists(deepseek_path):
            inputs.append(deepseek_path)
        if os.path.exists(doubao_path):
            inputs.append(doubao_path)
        if include_kimi_in_merge and os.path.exists(kimi_path):
            inputs.append(kimi_path)
        if not inputs:
            raise RuntimeError(f"{bundle_name} 缺少可融合的总结稿")
        merge_extra = (
            "【分片级融合指令】\n"
            "- 仅融合当前输入稿对应的单个分片/小节内容，禁止扩写到其他章节；\n"
            "- 保留输入中的标题层级与知识点细节，不要泛化、压缩成过短摘要；\n"
            "- 冲突处理：若多稿存在冲突，请按 DeepSeek > Doubao"
            + (" > Kimi" if include_kimi_in_merge else "")
            + " 的优先级保留；\n"
            "- 不要输出处理过程、模型自述、道歉或占位提示。"
        )
        run_llm_agent(
            agent="deepseek_merge_from_source",
            provider="openai_compat",
            system_prompts=[prompt_v5, prompt_merge],
            inputs=inputs,
            output=merged_path,
            key_env="DEEPSEEK_API_KEY",
            base="https://api.deepseek.com/v1",
            timeout=300,
            stream=True,
            show_thinking=True,
            extra=merge_extra,
            auto_chunk=auto_chunk,
        )

    can_skip_merge = (
        allow_partial
        and os.path.exists(deepseek_path)
        and os.path.exists(doubao_path)
        and os.path.exists(merged_path)
        and not (ran_kimi or ran_deepseek or ran_doubao)
    )
    if not can_skip_merge:
        ts = time.time()
        try:
            with_retries("merge", run_merge)
        except Exception:
            fallback = None
            for pth in [deepseek_path, doubao_path, kimi_path]:
                if pth and os.path.exists(pth):
                    fallback = pth
                    break
            if not fallback:
                raise
            safe_write_text(merged_path, safe_read_text(fallback))
        agent_times["merge"] = time.time() - ts

    if not enable_kimi:
        _safe_remove_file(kimi_path)

    return {
        "prefix": prefix,
        "source_path": source_path,
        "kimi_path": kimi_path,
        "deepseek_path": deepseek_path,
        "doubao_path": doubao_path,
        "merged_path": merged_path,
        "expected_h2": expected_h2,
        "source_hash": source_hash,
        "retries": retries_map,
        "agent_times": agent_times,
    }


@dataclass
class ChapterResult:
    part_section_num: int
    chapter_no: int
    prefix: str
    source_path: str
    kimi_path: str
    deepseek_path: str
    doubao_path: str
    merged_path: str
    chapter_hash: str
    status: str
    elapsed_sec: float
    retries: Dict[str, int]
    agent_times: Dict[str, float]


def process_chapter(book_dir: str,
                   book_name: str,
                   chapter_no: int,
                   items: List[FileMeta],
                   retries: int,
                   retry_backoff: int,
                   prompts_root: str,
                   incremental_resume: bool = False,
                   prev_chapter_hash: Optional[str] = None,
                   skip_merge_unchanged: bool = False,
                   auto_chunk: bool = False) -> ChapterResult:
    t0 = time.time()
    prefix = derive_chapter_prefix(items)
    # Derive part section number for richer logging
    try:
        part_num = items[0].part_section_num if items else 0
        module_num = items[0].module_num if items else 0
        project_num = items[0].project_num if items else 0
    except Exception:
        part_num = 0
        module_num = 0
        project_num = 0
    try:
        hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, chapter_no)
        with _PRINT_LOCK:
            print(f"[{now_str()}] [章节开始] {book_name} {hier_desc} 任务启动")
    except Exception:
        pass

    # Paths
    chapter_dir = book_dir  # archive in place
    source_path = os.path.join(chapter_dir, f"{prefix}.source.md")
    kimi_path = os.path.join(chapter_dir, f"{prefix}.kimi_from_source.md")
    deepseek_path = os.path.join(chapter_dir, f"{prefix}.deepseek_from_source.md")
    doubao_path = os.path.join(chapter_dir, f"{prefix}.doubao_from_source.md")
    merged_path = os.path.join(chapter_dir, f"{prefix}.merged_from_source.md")

    split_mode = _should_use_split_mode(items)
    ordered_parts: List[Tuple[str, str]] = []
    contents: List[str] = []
    for fm in items:
        txt = safe_read_text(fm.path)
        ordered_parts.append((fm.name, txt))
        contents.append(txt)

    if split_mode:
        try:
            with _PRINT_LOCK:
                print(f"[{now_str()}] [拆分章模式] {book_name} {hier_desc} 检测到{len(items)}个分片文件，改为逐文件总结后顺序拼接")
        except Exception:
            pass
        chapter_hash = _compute_ordered_content_hash(ordered_parts)
        chapter_expected_h2 = _infer_expected_h2_from_items(items, chapter_no, module_num, project_num)
        source_path = ""
        _safe_remove_file(os.path.join(chapter_dir, f"{prefix}.source.md"))

        bundle_results: List[Dict[str, Any]] = []
        agg_retries: Dict[str, int] = {"deepseek": 0, "doubao": 0, "merge": 0}
        agg_times: Dict[str, float] = {"deepseek": 0.0, "doubao": 0.0, "merge": 0.0}
        for fm in items:
            bundle_result = _process_split_bundle(
                book_name=book_name,
                bundle_name=fm.name,
                chapter_dir=chapter_dir,
                prefix=fm.name,
                source_text=safe_read_text(fm.path),
                chapter_no=chapter_no,
                module_num=module_num,
                project_num=project_num,
                retries=retries,
                retry_backoff=retry_backoff,
                auto_chunk=auto_chunk,
                enable_kimi=False,
                include_kimi_in_merge=False,
            )
            bundle_results.append(bundle_result)
            for k, v in bundle_result.get("retries", {}).items():
                agg_retries[k] = agg_retries.get(k, 0) + int(v)
            for k, v in bundle_result.get("agent_times", {}).items():
                agg_times[k] = agg_times.get(k, 0.0) + float(v)

        _stitch_split_outputs([r["deepseek_path"] for r in bundle_results], deepseek_path, chapter_expected_h2)
        _stitch_split_outputs([r["doubao_path"] for r in bundle_results], doubao_path, chapter_expected_h2)
        _safe_remove_file(kimi_path)
        _stitch_split_outputs([r["merged_path"] for r in bundle_results], merged_path, chapter_expected_h2)

        elapsed = time.time() - t0
        try:
            with _PRINT_LOCK:
                print(
                    f"[{now_str()}] [章节完成] {book_name} {hier_desc} 用时{format_duration_cn(elapsed)} | "
                    f"拆分模式={len(items)}个分片 | "
                    f"重试：deepseek={agg_retries.get('deepseek', 0)}次 "
                    f"doubao={agg_retries.get('doubao', 0)}次 merge={agg_retries.get('merge', 0)}次 | "
                    f"耗时：deepseek={format_duration_cn(agg_times.get('deepseek', 0))} "
                    f"doubao={format_duration_cn(agg_times.get('doubao', 0))} "
                    f"merge={format_duration_cn(agg_times.get('merge', 0))}"
                )
        except Exception:
            pass
        return ChapterResult(
            part_section_num=part_num,
            chapter_no=chapter_no,
            prefix=prefix,
            source_path=source_path,
            kimi_path="",
            deepseek_path=deepseek_path,
            doubao_path=doubao_path,
            merged_path=merged_path,
            chapter_hash=chapter_hash,
            status="done",
            elapsed_sec=elapsed,
            retries=agg_retries,
            agent_times=agg_times,
        )

    # Build chapter source for normal chapter mode only.
    # Use plain blank lines to separate sections; avoid '---' to prevent Markdown HR/page breaks
    source_text = ("\n\n").join(contents)
    safe_write_text(source_path, source_text)
    chapter_hash = compute_hash(source_text)

    def _infer_expected_h2_from_source(text: str) -> Optional[str]:
        """Infer top-level expected H2 heading from source text.

        - Prefer first '## ...'
        - If only '# ...' exists, promote to '## ...' (preserve full title text)
        """
        try:
            for _ln in text.splitlines():
                s = (_ln or "").strip()
                if not s:
                    continue
                if s.startswith("##"):
                    m = re.match(r"^##\s*(.+?)\s*$", s)
                    if m and m.group(1).strip():
                        return "## " + m.group(1).strip()
                if s.startswith("#"):
                    # Promote H1 to H2 to match downstream merge template
                    m = re.match(r"^#\s*(.+?)\s*$", s)
                    if m and m.group(1).strip():
                        return "## " + m.group(1).strip()
        except Exception:
            return None
        return None

    def _parse_h2_heading(line: str) -> Tuple[Optional[str], Optional[int], str]:
        """Parse '##' heading into (unit, number, rest_title).
        Supports both '第1章 标题' and '项目1 标题' / '模块一 标题' styles.
        """
        s = (line or "").strip()
        if not s.startswith("##"):
            return (None, None, "")
        body = re.sub(r"^##\s*", "", s).strip()
        if not body:
            return (None, None, "")
        # Pattern A: (optional 第) + number + unit + rest
        m = re.match(r"^(?:第\s*)?([一二三四五六七八九十百千万\d]+)\s*(章|单元|模块|项目|任务|部分|篇)\s*(.*)$", body)
        if m:
            tok = m.group(1)
            unit = m.group(2)
            rest = (m.group(3) or "").strip()
            try:
                no = int(tok) if tok.isdigit() else cn_to_int(tok)
            except Exception:
                no = None
            return (unit, no, rest)
        # Pattern B: unit + number + rest
        m = re.match(r"^(章|单元|模块|项目|任务|部分|篇)\s*([一二三四五六七八九十百千万\d]+)\s*(.*)$", body)
        if m:
            unit = m.group(1)
            tok = m.group(2)
            rest = (m.group(3) or "").strip()
            try:
                no = int(tok) if tok.isdigit() else cn_to_int(tok)
            except Exception:
                no = None
            return (unit, no, rest)
        return (None, None, body)

    def _is_generic_heading(line: str) -> bool:
        """Whether the heading is a generic placeholder like '## 第1章' with no title."""
        s = (line or "").strip()
        if not s.startswith("##"):
            return False
        body = re.sub(r"^##\s*", "", s).strip()
        if not body:
            return False
        # Matches "第1章" / "第十章" only (no trailing title)
        return bool(re.fullmatch(r"(?:第\s*)?[一二三四五六七八九十百千万\d]+\s*章", body))

    # Derive expected chapter top-level heading:
    # 1) from source_text (#/##)
    # 2) fallback based on hierarchy (project > module > chapter)
    expected_h2 = None
    if not expected_h2:
        expected_h2 = _infer_expected_h2_from_source(source_text)
    if not expected_h2:
        # Hierarchy-aware fallback (avoid rewriting '项目/模块' as '第X章')
        if project_num > 0:
            expected_h2 = f"## 项目{project_num}"
        elif module_num > 0:
            expected_h2 = f"## 模块{module_num}"
        else:
            expected_h2 = f"## 第{chapter_no}章"

    # Hash inputs for skip logic
    chapter_hash = compute_hash(source_text)

    # Run two agents concurrently, with retries handled here for each agent.
    retries_map: Dict[str, int] = {"deepseek": 0, "doubao": 0, "merge": 0}

    def with_retries(tag: str, fn):
        nonlocal retries_map
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
                    try:
                        hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, chapter_no)
                    except Exception:
                        hier_desc = f"第{chapter_no}章"
                    with _PRINT_LOCK:
                        print(f"[{now_str()}] [章节重试] {book_name} {hier_desc} agent={tag} 第{attempt}/{retries+1}次失败：{e}；{format_duration_cn(sleep_s)}后重试")
                    time.sleep(sleep_s)
                else:
                    try:
                        hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, chapter_no)
                    except Exception:
                        hier_desc = f"第{chapter_no}章"
                    with _PRINT_LOCK:
                        print(f"[{now_str()}] [章节失败] {book_name} {hier_desc} agent={tag} 在{retries+1}次尝试后仍失败：{e}")
                    raise

    # Paths to prompts (reuse job config paths)
    prompt_v5 = os.path.join("md", "知识点总结提示词V5.1.md")
    prompt_deepseek = os.path.join("multi_agent", "prompts", "deepseek_agent.md")
    prompt_doubao = os.path.join("multi_agent", "prompts", "doubao_agent.md")
    prompt_merge = os.path.join("multi_agent", "prompts", "llm_merge_agent.md")

    # Deepseek
    def run_deepseek():
        _acq = _SEMA_DEEPSEEK.acquire
        _rel = _SEMA_DEEPSEEK.release
        _acq()
        try:
            run_llm_agent(
                agent="deepseek_from_source",
                provider="openai_compat",
                system_prompts=[prompt_v5, prompt_deepseek],
                inputs=[source_path],
                output=deepseek_path,
                model=None,  # runner infers deepseek-reasoner
                key_env="DEEPSEEK_API_KEY",
                base="https://api.deepseek.com/v1",
                timeout=300,
                stream=True,
                show_thinking=True,
                auto_chunk=auto_chunk,
            )
        finally:
            try:
                _rel()
            except Exception as rel_err:
                with _PRINT_LOCK:
                    print(f"[警告] Deepseek信号量释放失败：{rel_err}")

    # Doubao
    def run_doubao():
        _acq = _SEMA_DOUBAO.acquire
        _rel = _SEMA_DOUBAO.release
        _acq()
        try:
            run_llm_agent(
                agent="doubao_from_source",
                provider="openai_compat",
                system_prompts=[prompt_v5, prompt_doubao],
                inputs=[source_path],
                output=doubao_path,
                model=None,  # runner infers doubao-seed-2-0-pro-260215
                key_env="ARK_API_KEY",
                base="https://ark.cn-beijing.volces.com/api/v3",
                timeout=300,
                stream=True,
                show_thinking=True,
                auto_chunk=auto_chunk,
            )
        finally:
            try:
                _rel()
            except Exception as rel_err:
                with _PRINT_LOCK:
                    print(f"[警告] Doubao信号量释放失败：{rel_err}")

    # Decide whether we can incrementally resume existing agent outputs.
    # Allow partial skip when chapter content unchanged, determined by either progress hash or existing source.md hash.
    try:
        existing_source = os.path.join(chapter_dir, f"{prefix}.source.md")
        source_matches_prev = False
        if os.path.exists(existing_source):
            source_matches_prev = (compute_hash(safe_read_text(existing_source)) == chapter_hash)
    except Exception:
        source_matches_prev = False
    allow_partial = incremental_resume and ((prev_chapter_hash is not None and prev_chapter_hash == chapter_hash) or source_matches_prev)

    # Run two agents with retries (skip those already present when allow_partial)
    ran_deepseek = False
    ran_doubao = False
    agent_times: Dict[str, float] = {"deepseek": 0.0, "doubao": 0.0, "merge": 0.0}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    futs = []

    def _task_deepseek():
        nonlocal ran_deepseek
        _ts = time.time()
        with_retries("deepseek", run_deepseek)
        agent_times["deepseek"] = time.time() - _ts
        ran_deepseek = True

    def _task_doubao():
        nonlocal ran_doubao
        _ts = time.time()
        with_retries("doubao", run_doubao)
        agent_times["doubao"] = time.time() - _ts
        ran_doubao = True

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Submit tasks in priority order: deepseek > doubao
        if not (allow_partial and os.path.exists(deepseek_path)):
            futs.append(pool.submit(_task_deepseek))
        if not (allow_partial and os.path.exists(doubao_path)):
            futs.append(pool.submit(_task_doubao))
        errors: List[Exception] = []
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                errors.append(e)
        if errors:
            # Re-raise first error to preserve existing failure semantics
            raise errors[0]

    # Helper to enforce merged top heading matches expected_h2
    def _ensure_merged_top_heading(path: str, expected_h2_text: str) -> None:
        try:
            text0 = safe_read_text(path)
        except Exception:
            return
        try:
            lines = text0.splitlines()
            idx = None
            for i, ln in enumerate(lines):
                if ln.strip().startswith("## "):
                    idx = i
                    break
            changed = False
            exp_unit, exp_no, exp_rest = _parse_h2_heading(expected_h2_text.strip())

            if idx is None:
                # Prepend expected heading and a blank line
                if expected_h2_text.strip():
                    lines.insert(0, expected_h2_text.strip())
                    lines.insert(1, "")
                    changed = True
            else:
                cur = lines[idx].strip()
                cur_unit, cur_no, cur_rest = _parse_h2_heading(cur)
                exp_line = expected_h2_text.strip()

                # If expected is generic placeholder, never overwrite a more informative current heading
                if exp_line and _is_generic_heading(exp_line) and cur and not _is_generic_heading(cur) and len(cur) >= len(exp_line):
                    pass
                else:
                    # If both parse and refer to the same logical unit+number, prefer the most informative title
                    if exp_no is not None and cur_no is not None and exp_no == cur_no and (exp_unit == cur_unit or exp_unit is None or cur_unit is None):
                        # Prefer "has title" then longer length
                        def info_score(line0: str) -> Tuple[int, int]:
                            u, n, r = _parse_h2_heading(line0)
                            has_title = 1 if (r or "").strip() else 0
                            return (has_title, len((line0 or "").strip()))
                        best = max([cur, exp_line], key=info_score)
                        if best and best != cur:
                            lines[idx] = best
                            changed = True
                    else:
                        # Mismatch: only replace when expected is clearly better (non-empty and not generic-worse)
                        if exp_line and cur != exp_line:
                            # If expected has title/rest while current doesn't, prefer expected.
                            exp_has_title = 1 if (exp_rest or "").strip() else 0
                            cur_has_title = 1 if (cur_rest or "").strip() else 0
                            if exp_has_title > cur_has_title:
                                lines[idx] = exp_line
                                changed = True
                            else:
                                # If current is unparseable or looks wrong and expected isn't generic, replace.
                                if (cur_no is None and not cur_has_title) and not _is_generic_heading(exp_line):
                                    lines[idx] = exp_line
                                    changed = True
            if changed:
                safe_write_text(path, "\n".join(lines))
        except Exception:
            return

    # Merge (allow partial: if a path missing, it will still attempt with existing ones)
    def run_merge():
        # Prefer independent merge semaphore to avoid contention with DeepSeek generation.
        # If not available within a short timeout, borrow DeepSeek generation permit as a fallback.
        acquired: str | None = None  # 'merge' or 'deepseek'
        try:
            try:
                borrow_timeout = float(os.getenv("MERGE_BORROW_TIMEOUT_SECONDS", "1.0"))
            except Exception:
                borrow_timeout = 1.0
            got_merge = False
            try:
                # Try dedicated merge semaphore first
                got_merge = _SEMA_DEEPSEEK_MERGE.acquire(timeout=max(0.01, borrow_timeout))
            except Exception:
                got_merge = False
            if got_merge:
                acquired = 'merge'
                # Optional: minimal visibility
                try:
                    with _PRINT_LOCK:
                        print(f"[merge] 使用独立合并并发许可 (DEEPSEEK_MERGE_MAX_CONCURRENCY)")
                except Exception:
                    pass
            else:
                # Borrow a DeepSeek generation slot to prevent starvation
                _SEMA_DEEPSEEK.acquire()
                acquired = 'deepseek'
                try:
                    with _PRINT_LOCK:
                        print(f"[merge:borrow] 合并阶段借用 DeepSeek 并发许可以避免等待（borrow_timeout={borrow_timeout:.1f}s）")
                except Exception:
                    pass
            inputs = []
            # Helper: slice a single chapter section by matching H2 heading for this chapter_no
            def _slice_chapter_text(text: str, ch_no: int) -> str:
                try:
                    lines = text.splitlines()
                    start = None
                    for i, ln in enumerate(lines):
                        s = ln.strip()
                        if not s.startswith("## "):
                            continue
                        u, v, _rest = _parse_h2_heading(s)
                        # When this chapter is a project/module, prefer matching unit accordingly
                        prefer_unit = "项目" if project_num > 0 else ("模块" if module_num > 0 else None)
                        if v == ch_no and (prefer_unit is None or u == prefer_unit):
                            start = i
                            break
                    if start is None:
                        # fallback: take from first H2 if exists
                        for i, ln in enumerate(lines):
                            if ln.strip().startswith("## "):
                                start = i
                                break
                    if start is None:
                        return text  # no H2 at all, return original
                    end = len(lines)
                    for j in range(start + 1, len(lines)):
                        if lines[j].strip().startswith("## "):
                            end = j
                            break
                    return "\n".join(lines[start:end]).strip() + "\n"
                except Exception:
                    return text

            # Gather and clamp each agent output to this chapter only.
            # Priority order: deepseek > doubao
            slice_map = []  # (orig_path, agent_tag)
            if os.path.exists(deepseek_path):
                slice_map.append((deepseek_path, "deepseek"))
            if os.path.exists(doubao_path):
                slice_map.append((doubao_path, "doubao"))

            for orig, tag in slice_map:
                try:
                    raw = safe_read_text(orig)
                    clipped = _slice_chapter_text(raw, chapter_no)
                    # If clipping changed content materially, write a .slice.md for merge input
                    use_path = orig
                    try:
                        if clipped and clipped.strip() and (clipped.strip() != raw.strip()):
                            sp = os.path.join(chapter_dir, f"{prefix}.{tag}_from_source.slice.md")
                            safe_write_text(sp, clipped)
                            use_path = sp
                    except Exception:
                        # Fallback to original on any error
                        use_path = orig
                    inputs.append(use_path)
                except Exception:
                    # If read fails, skip this input
                    continue
            # Optional gemini ignored
            # Strong per-chapter directive to avoid full-book restart from 第1章
            merge_extra = (
                "【章节级融合指令 — 仅限本章】\n"
                "- 仅融合并输出当前章节内容，禁止输出任何其他章节、目录或前言；\n"
                "- 顶层标题必须与输入稿中的本章标题一致（以 '## ' 开头，编号+名称整行保留），若为“项目/模块/任务”等单位，禁止改写成“第1章”；\n"
                "- 输出严格限定在本章（包含本章内的所有知识点与小节），不要跨章节；\n"
                "- 冲突处理：若多稿内容存在冲突，请按 DeepSeek > Doubao 的优先级选择保留要点；\n"
                "- 不要输出处理过程、模型自述、道歉或占位提示；\n"
                "- 若上方提示词与本指令存在冲突，以本指令为准。"
            )
            # Prefer the most informative expected heading from agent outputs.
            # Priority order: deepseek > doubao
            best_expected = expected_h2
            try:
                cands: List[str] = []
                for pth in [deepseek_path, doubao_path]:
                    if os.path.exists(pth):
                        txt = safe_read_text(pth)
                        for _ln in txt.splitlines():
                            s = _ln.strip()
                            if s.startswith("## "):
                                u, v, _rest = _parse_h2_heading(s)
                                prefer_unit = "项目" if project_num > 0 else ("模块" if module_num > 0 else None)
                                if v == chapter_no and (prefer_unit is None or u == prefer_unit):
                                    cands.append(s)
                                break
                if cands:
                    # Prefer "has title" then longer length
                    def info_score(line0: str) -> Tuple[int, int]:
                        u, n, r = _parse_h2_heading(line0)
                        has_title = 1 if (r or "").strip() else 0
                        return (has_title, len((line0 or "").strip()))
                    cand = max(cands, key=info_score)
                    if info_score(cand) > info_score(best_expected):
                        best_expected = cand
            except Exception:
                pass
            try:
                run_llm_agent(
                    agent="deepseek_merge_from_source",
                    provider="openai_compat",
                    system_prompts=[prompt_v5, prompt_merge],
                    inputs=inputs,
                    output=merged_path,
                    model=None,  # default deepseek-reasoner
                    key_env="DEEPSEEK_API_KEY",
                    base="https://api.deepseek.com/v1",
                    timeout=300,
                    stream=True,
                    show_thinking=True,
                    extra=merge_extra,
                    auto_chunk=auto_chunk,
                )
                # Post-fix: enforce top heading to match expected chapter title
                try:
                    _ensure_merged_top_heading(merged_path, best_expected)
                except Exception:
                    pass
            except Exception as merge_err:
                # Fallback: if merge fails, select the first available sliced input by priority (deepseek > doubao)
                fb_path = None
                try:
                    # Prefer the first element already in inputs (constructed in priority order and possibly sliced)
                    if inputs:
                        fb_path = inputs[0]
                    else:
                        # As a last resort, choose any existing agent output by priority
                        for pth in (deepseek_path, doubao_path):
                            if os.path.exists(pth):
                                fb_path = pth
                                break
                    if fb_path and os.path.exists(fb_path):
                        safe_write_text(merged_path, safe_read_text(fb_path))
                        try:
                            _ensure_merged_top_heading(merged_path, best_expected)
                        except Exception:
                            pass
                        with _PRINT_LOCK:
                            print(f"[merge:fallback] 深度融合失败，已采用优先路径输出：{os.path.basename(fb_path)}")
                    else:
                        raise merge_err
                except Exception:
                    # Re-raise original merge error to trigger retries
                    raise merge_err
        finally:
            try:
                if acquired == 'merge':
                    _SEMA_DEEPSEEK_MERGE.release()
                elif acquired == 'deepseek':
                    _SEMA_DEEPSEEK.release()
            except Exception as rel_err:
                try:
                    with _PRINT_LOCK:
                        print(f"[警告] Deepseek(merge)信号量释放失败：{rel_err}")
                except Exception:
                    pass

    # Optionally skip merge when everything unchanged and merged exists
    can_skip_merge = (
        skip_merge_unchanged
        and (prev_chapter_hash is not None)
        and (prev_chapter_hash == chapter_hash)
        and os.path.exists(deepseek_path)
        and os.path.exists(doubao_path)
        and os.path.exists(merged_path)
        and not (ran_deepseek or ran_doubao)
    )
    if can_skip_merge:
        print(f"[章节跳过] {book_name} 第{chapter_no}章 融合未变且已存在，跳过 merge")
        # Still emit per-section merged outputs best-effort
        try:
            _emit_section_outputs(merged_path, items, chapter_dir)
        except Exception:
            pass
    else:
        _ts = time.time()
        with_retries("merge", run_merge)
        agent_times["merge"] = time.time() - _ts
        # Emit per-section merged outputs best-effort
        try:
            _emit_section_outputs(merged_path, items, chapter_dir)
        except Exception:
            pass

    _safe_remove_file(kimi_path)

    elapsed = time.time() - t0
    # 中文友好计时输出
    try:
        with _PRINT_LOCK:
            try:
                hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, chapter_no)
            except Exception:
                hier_desc = f"第{chapter_no}章"
            print(
                f"[{now_str()}] [章节完成] {book_name} {hier_desc} 用时{format_duration_cn(elapsed)} | "
                f"重试：deepseek={retries_map.get('deepseek', 0)}次 doubao={retries_map.get('doubao', 0)}次 merge={retries_map.get('merge', 0)}次 | "
                f"耗时：deepseek={format_duration_cn(agent_times.get('deepseek', 0))} "
                f"doubao={format_duration_cn(agent_times.get('doubao', 0))} merge={format_duration_cn(agent_times.get('merge', 0))}"
            )
    except Exception:
        pass
    # Derive part_section_num from items (consistent within a chapter group)
    try:
        part_num = items[0].part_section_num if items else 0
    except Exception:
        part_num = 0
    return ChapterResult(
        part_section_num=part_num,
        chapter_no=chapter_no,
        prefix=prefix,
        source_path=source_path,
        kimi_path="",
        deepseek_path=deepseek_path,
        doubao_path=doubao_path,
        merged_path=merged_path,
        chapter_hash=chapter_hash,
        status="done",
        elapsed_sec=elapsed,
        retries=retries_map,
        agent_times=agent_times,
    )


def get_chapter_sort_key(chapter_map: Dict[int, List[FileMeta]], ch_no: int) -> Tuple[int, int]:
    """Get sort key (part_section_num, chapter_no) for a chapter.
    Uses the first FileMeta's part_section_num as representative.
    """
    items = chapter_map.get(ch_no, [])
    if not items:
        return (0, ch_no)
    # All items in same chapter should have same part_section_num
    return (items[0].part_section_num, ch_no)


def stitch_book_final(know_id: int, book_name: str, chapter_results: List[ChapterResult], final_root: str) -> str:
    """Stitch chapters into final book, ordering by (part_section_num, chapter_no)."""
    # Sort by (part_section_num, chapter_no)
    chapter_results = sorted(chapter_results, key=lambda cr: (int(cr.part_section_num), int(cr.chapter_no)))
    parts: List[str] = []
    # 保证按篇-章号严格顺序拼接
    for ch in chapter_results:
        if os.path.exists(ch.merged_path):
            parts.append(safe_read_text(ch.merged_path))
    text = "\n\n\n".join(parts)
    # Output path
    out_dir = os.path.join(final_root)
    ensure_dir(out_dir)
    out_path = os.path.join(out_dir, f"{know_id}-1 {book_name}.md")
    safe_write_text(out_path, text)
    return os.path.abspath(out_path)


def load_progress(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"books": {}}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return json.load(f)
    except Exception:
        return {"books": {}}


def save_progress(path: str, data: Dict[str, Any]) -> None:
    """Atomic save progress using safe_write_text."""
    ensure_dir(os.path.dirname(path))
    text = json.dumps(data, ensure_ascii=False, indent=2)
    safe_write_text(path, text)


def update_progress_atomic(path: str, mutator) -> Dict[str, Any]:
    """Lock, load, mutate, save, and return the updated progress dict.
    The mutator receives the current progress dict and may mutate in place.
    """
    # Ensure containing directory exists before creating lock file
    try:
        ensure_dir(os.path.dirname(path) or ".")
    except Exception:
        pass
    lock_path = f"{path}.lock"
    with SimpleFileLock(lock_path, timeout=120, poll_interval=0.1):
        data = load_progress(path)
        try:
            mutator(data)
        except Exception:
            # Do not break locking lifecycle; re-raise after releasing
            raise
        save_progress(path, data)
        return data


def _normalize_book_name(name: str) -> str:
    # strip trailing underscores/spaces
    return name.rstrip(" _-")


def backfill_progress_from_library(final_root: str, progress_path: str) -> int:
    """Scan final_root for existing '<know_id>-1 <book_name>.md' and backfill progress.json.
    Returns number of books added/updated.
    """
    progress = load_progress(progress_path)
    os.makedirs(final_root, exist_ok=True)
    added = 0

    pattern = re.compile(r"^(\d+)-1\s+(.+)\.md$", re.IGNORECASE)

    for root, _, files in os.walk(final_root):
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            m = pattern.match(fn)
            if not m:
                continue
            kid = int(m.group(1))
            bname = _normalize_book_name(m.group(2))
            final_path = os.path.join(root, fn)
            # compute hash of final file content (as a stand-in for book_hash)
            try:
                final_text = safe_read_text(final_path)
                final_hash = compute_hash(final_text)
            except Exception:
                final_hash = None

            bk_key = str(kid)
            book_rec = progress.setdefault("books", {}).get(bk_key)
            if not book_rec:
                progress["books"][bk_key] = {
                    "know_id": kid,
                    "book_name": bname,
                    "status": "done",
                    "chapters": {},
                    "final_path": os.path.relpath(final_path).replace("\\", "/"),
                    "elapsed_sec_total": 0.0,
                    "book_hash": final_hash,
                }
                added += 1
            else:
                # update if missing final_path or status not done
                updated = False
                if not book_rec.get("final_path"):
                    book_rec["final_path"] = os.path.relpath(final_path).replace("\\", "/")
                    updated = True
                if book_rec.get("status") != "done":
                    book_rec["status"] = "done"
                    updated = True
                # refresh book_name if empty/wrong
                if book_rec.get("book_name") and book_rec["book_name"].endswith("_"):
                    book_rec["book_name"] = bname
                    updated = True
                if not book_rec.get("book_hash") and final_hash:
                    book_rec["book_hash"] = final_hash
                    updated = True
                if updated:
                    progress["books"][bk_key] = book_rec
                    added += 1

    save_progress(progress_path, progress)
    return added


def parse_book_dirname(dirname: str) -> Tuple[int, str]:
    # "1004-中国旅游地理 第二版_organized" -> (1004, "中国旅游地理 第二版")
    base = dirname
    if base.endswith("/"):
        base = base[:-1]
    base = os.path.basename(base)
    # cut suffix like: _organized, md_organized, MD_organized, organized (with or without preceding separators)
    # handle cases like: " 第二版md_organized", " 第三版organized", "-organized", "_organized"
    cleaned = re.sub(r"[\s_\-]*(?:md[\s_\-]*)?organized$", "", base, flags=re.IGNORECASE)
    # trim any residual trailing separators
    cleaned = cleaned.strip(" _-")
    # split by first hyphen (allow surrounding whitespace)
    m = re.match(r"(\d+)\s*[-－—]\s*(.+)", cleaned)
    if not m:
        raise ValueError(f"Invalid book dir name: {dirname}")
    return int(m.group(1)), m.group(2).strip()


def collect_book_files(book_dir: str) -> Dict[str, List[FileMeta]]:
    """Collect and group files by chapter_no.
    Returns a dict mapping composite key '<part>-<chapter>' to list of FileMeta.
    The part_section_num is preserved in each FileMeta for later global sorting.
    """
    metas: List[FileMeta] = []
    skipped_files: List[str] = []  # Track files that couldn't be parsed
    
    for fn in os.listdir(book_dir):
        if not fn.lower().endswith(".md"):
            continue
        # Skip pipeline artifacts to avoid feeding generated content back into source
        low = fn.lower()
        if (
            low.endswith(".source.md")
            or low.endswith(".merged_from_source.md")
            or low.endswith("_from_source.md")
            or low.endswith("_from_source.slice.md")
        ):
            continue
        full = os.path.join(book_dir, fn)
        meta = build_file_meta(full)
        if meta:
            metas.append(meta)
        else:
            # File was not recognized - log it for visibility
            if not fn.startswith("000-"):  # Don't warn about index files
                skipped_files.append(fn)
    
    # Report skipped files with warning
    if skipped_files:
        with _PRINT_LOCK:
            print(f"[文件识别警告] {book_dir} 中有{len(skipped_files)}个文件无法识别章节结构:")
            for sf in skipped_files[:10]:  # Show first 10 to avoid spam
                print(f"  - {sf}")
            if len(skipped_files) > 10:
                print(f"  ... 还有{len(skipped_files) - 10}个文件")
    
    return group_by_chapter(metas)


def compute_book_hash(chapter_results: List[ChapterResult]) -> str:
    # Concatenate chapter hashes by (part_section_num, chapter_no) order to avoid collisions across parts
    arr = [cr.chapter_hash for cr in sorted(chapter_results, key=lambda x: (int(x.part_section_num), int(x.chapter_no)))]
    return compute_hash("|".join(arr))


def process_one_book(book_dir: str,
                     final_root: str,
                     progress_path: str,
                     force: bool,
                     target_chapters: Optional[List[int]],
                     retries: int,
                     retry_backoff: int,
                     skip_book_if_final: bool = True,
                     incremental_resume: bool = False,
                     skip_merge_unchanged: bool = False,
                     chapter_max_workers: int = 1,
                     auto_chunk: bool = False) -> None:
    t_book = time.time()
    know_id, book_name = parse_book_dirname(book_dir)

    # Ensure a book record exists and persist atomically
    bk_key = str(know_id)
    def _ensure_book(d: Dict[str, Any]) -> None:
        books = d.setdefault("books", {})
        rec = books.get(bk_key)
        if not rec:
            books[bk_key] = {
                "know_id": know_id,
                "book_name": book_name,
                "status": "pending",
                "chapters": {},
                "final_path": None,
                "elapsed_sec_total": 0.0,
                "book_hash": None,
            }
        else:
            # backfill missing keys if any
            rec.setdefault("know_id", know_id)
            rec.setdefault("book_name", book_name)
            rec.setdefault("status", "pending")
            rec.setdefault("chapters", {})
            rec.setdefault("final_path", None)
            rec.setdefault("elapsed_sec_total", 0.0)
            rec.setdefault("book_hash", None)
            books[bk_key] = rec
    progress = update_progress_atomic(progress_path, _ensure_book)
    book_rec = progress.get("books", {}).get(bk_key, {})

    # Early skip: only when book status is done and final exists (avoid skipping partial/failed)
    if (not force) and skip_book_if_final:
        status0 = book_rec.get("status")
        final_path = book_rec.get("final_path")
        chapters_rec = book_rec.get("chapters") or {}
        check_path = None
        if final_path:
            try:
                check_path = final_path if os.path.isabs(final_path) else os.path.abspath(final_path)
            except Exception:
                check_path = final_path
        if status0 == "done" and check_path and os.path.exists(check_path) and len(chapters_rec) > 0:
            with _PRINT_LOCK:
                print(f"[{now_str()}] [跳过书籍] {know_id}-{book_name} 已存在成品：{final_path}")
            return

    chapter_map = collect_book_files(book_dir)
    # Sort chapter keys by (part_section_num, module_num, chapter_no)
    def _parse_key(k: str) -> Tuple[int, int, int]:
        try:
            parts = k.split("-")
            if len(parts) >= 3:
                return (int(parts[0]), int(parts[1]), int(parts[2]))
            elif len(parts) == 2:
                # Backward compatibility: old format 'part-chapter'
                return (int(parts[0]), 0, int(parts[1]))
            else:
                return (0, 0, 0)
        except Exception:
            return (0, 0, 0)
    chapter_keys = sorted(chapter_map.keys(), key=_parse_key)
    if target_chapters:
        st = set(target_chapters)
        # Match against the final component (project/chapter number)
        chapter_keys = [k for k in chapter_keys if _parse_key(k)[2] in st]

    chapter_results: List[ChapterResult] = []
    
    # Count total source files for better reporting
    try:
        total_md_files = len([f for f in os.listdir(book_dir) 
                             if f.lower().endswith(".md") 
                             and not f.startswith("000-")
                             and not any(f.lower().endswith(suffix) for suffix in 
                                       [".source.md", ".merged_from_source.md", 
                                        "_from_source.md", "_from_source.slice.md"])])
        recognized_file_count = sum(len(items) for items in chapter_map.values())
        if total_md_files > recognized_file_count:
            with _PRINT_LOCK:
                print(f"[{now_str()}] [文件统计] {book_name} 总计{total_md_files}个源文件，识别到{recognized_file_count}个，未识别{total_md_files - recognized_file_count}个")
    except Exception:
        pass

    if chapter_max_workers <= 1:
        for ch_key in chapter_keys:
            items = chapter_map[ch_key]
            prefix = derive_chapter_prefix(items)
            # derive hierarchical numbers from items
            part_num = items[0].part_section_num if items else 0
            module_num = items[0].module_num if items else 0
            project_num = items[0].project_num if items else 0
            ch_no = items[0].chapter_no if items else 0
            # progress key uses composite key to avoid collisions across hierarchies
            # Build candidate source to compute hash for skip
            split_mode = _should_use_split_mode(items)
            ordered_parts: List[Tuple[str, str]] = []
            contents = []
            for fm in items:
                txt = safe_read_text(fm.path)
                ordered_parts.append((fm.name, txt))
                contents.append(txt)
            # Avoid Markdown horizontal rules as separators
            source_text = ("\n\n").join(contents)
            ch_hash = _compute_ordered_content_hash(ordered_parts) if split_mode else compute_hash(source_text)

            chapters_map = book_rec.setdefault("chapters", {})
            ch_rec = chapters_map.get(ch_key, {
                "status": "pending",
                "chapter_hash": None,
                "artifacts": {},
                "agent_times": {},
                "retries": {},
                "errors": [],
            })

            # Skip if up-to-date and artifacts exist
            if (not force):
                chapter_dir = book_dir
                paths = _required_artifact_paths(chapter_dir, prefix, split_mode)
                kimi_artifact = ""
                # fallback: if progress.json missing hash, but existing source.md matches current computed hash, allow skip
                existing_source = os.path.join(chapter_dir, f"{prefix}.source.md")
                source_matches = False
                try:
                    if (not split_mode) and os.path.exists(existing_source):
                        source_matches = (compute_hash(safe_read_text(existing_source)) == ch_hash)
                except Exception:
                    source_matches = False
                if (ch_rec.get("chapter_hash") == ch_hash or source_matches) and all(os.path.exists(p) for p in paths):
                    if split_mode:
                        _safe_remove_file(existing_source)
                    try:
                        hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, ch_no)
                    except Exception:
                        hier_desc = f"第{ch_no}章"
                    with _PRINT_LOCK:
                        print(f"[{now_str()}] [章节跳过] {book_name} {hier_desc} 已是最新，跳过")
                    # Update chapter record to reflect artifacts and hash
                    def _update_skip(d: Dict[str, Any]) -> None:
                        books = d.setdefault("books", {})
                        b = books.setdefault(bk_key, book_rec)
                        cms = b.setdefault("chapters", {})
                        cr = cms.get(ch_key, {})
                        cr.update({
                            "status": "done",
                            "chapter_hash": ch_hash,
                            "artifacts": {
                                "source": "" if split_mode else os.path.join(chapter_dir, f"{prefix}.source.md"),
                                "deepseek": paths[0],
                                "doubao": paths[1],
                                "kimi": kimi_artifact,
                                "merged": paths[-1],
                            },
                        })
                        cms[ch_key] = cr
                        b["chapters"] = cms
                        books[bk_key] = b
                    update_progress_atomic(progress_path, _update_skip)
                    # Legacy chapter-level slicing is only needed for non-split chapters.
                    if not split_mode:
                        try:
                            _emit_section_outputs(paths[-1], items, chapter_dir)
                        except Exception:
                            pass
                    chapter_results.append(ChapterResult(
                        part_section_num=part_num,
                        chapter_no=ch_no,
                        prefix=prefix,
                        source_path="" if split_mode else os.path.join(chapter_dir, f"{prefix}.source.md"),
                        deepseek_path=paths[0],
                        doubao_path=paths[1],
                        kimi_path=kimi_artifact,
                        merged_path=paths[-1],
                        chapter_hash=ch_hash,
                        status="done",
                        elapsed_sec=0.0,
                        retries={"deepseek": 0, "doubao": 0, "merge": 0},
                        agent_times={"deepseek": 0.0, "doubao": 0.0, "merge": 0.0},
                    ))
                    continue

            try:
                res = process_chapter(
                    book_dir=book_dir,
                    book_name=book_name,
                    chapter_no=ch_no,
                    items=items,
                    retries=retries,
                    retry_backoff=retry_backoff,
                    prompts_root="multi_agent/prompts",
                    incremental_resume=incremental_resume,
                    prev_chapter_hash=ch_rec.get("chapter_hash"),
                    skip_merge_unchanged=skip_merge_unchanged,
                    auto_chunk=auto_chunk,
                )
                chapter_results.append(res)
                # Update chapter record atomically
                def _update_chapter(d: Dict[str, Any]) -> None:
                    books = d.setdefault("books", {})
                    b = books.setdefault(bk_key, book_rec)
                    cms = b.setdefault("chapters", {})
                    cr = cms.get(ch_key, {})
                    cr.update({
                        "status": res.status,
                        "chapter_hash": res.chapter_hash,
                        "artifacts": {
                            "source": res.source_path,
                            "kimi": res.kimi_path,
                            "deepseek": res.deepseek_path,
                            "doubao": res.doubao_path,
                            "merged": res.merged_path,
                        },
                        "agent_times": {},
                        "retries": res.retries,
                    })
                    cms[ch_key] = cr
                    b["chapters"] = cms
                    books[bk_key] = b
                update_progress_atomic(progress_path, _update_chapter)
            except Exception as e:
                # 先取出到普通局部变量：except 块退出时 e 会被删除（PEP 3110），
                # 闭包若在块外被调用会抛 NameError。详见 docs/CODE_STANDARDS.md 1.4
                err_msg = str(e)
                # Persist failure atomically instead of mutating shared ch_rec snapshot
                def _update_error(d: Dict[str, Any]) -> None:
                    books = d.setdefault("books", {})
                    b = books.setdefault(bk_key, {})
                    cms = b.setdefault("chapters", {})
                    cr = cms.get(ch_key, {})
                    cr.setdefault("errors", []).append(err_msg)
                    cr["status"] = "failed"
                    cms[ch_key] = cr
                    b["chapters"] = cms
                    books[bk_key] = b
                update_progress_atomic(progress_path, _update_error)
                try:
                    hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, ch_no)
                except Exception:
                    hier_desc = f"第{ch_no}章"
                with _PRINT_LOCK:
                    print(f"[{now_str()}] [章节错误] {book_name} {hier_desc} 出错：{e}")
                # Even on failure, continue other chapters

            # Persist incremental progress already done above; still refresh local snapshot
            progress = load_progress(progress_path)
    else:
        # Parallel chapter processing
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def _process_one_chapter(ch_key: str, items: List[FileMeta]) -> Optional[ChapterResult]:
            prefix = derive_chapter_prefix(items)
            # derive hierarchical numbers from items
            part_num = items[0].part_section_num if items else 0
            module_num = items[0].module_num if items else 0
            project_num = items[0].project_num if items else 0
            ch_no = items[0].chapter_no if items else 0
            split_mode = _should_use_split_mode(items)
            ordered_parts: List[Tuple[str, str]] = []
            contents = []
            for fm in items:
                txt = safe_read_text(fm.path)
                ordered_parts.append((fm.name, txt))
                contents.append(txt)
            source_text = ("\n\n").join(contents)
            ch_hash = _compute_ordered_content_hash(ordered_parts) if split_mode else compute_hash(source_text)

            # Read chapter record from progress atomically to avoid race condition
            current_progress = load_progress(progress_path)
            current_book = current_progress.get("books", {}).get(bk_key, {})
            chapters_map = current_book.get("chapters", {})
            ch_rec = chapters_map.get(ch_key, {
                "status": "pending",
                "chapter_hash": None,
                "artifacts": {},
                "agent_times": {},
                "retries": {},
                "errors": [],
            })

            chapter_dir = book_dir
            paths = _required_artifact_paths(chapter_dir, prefix, split_mode)
            kimi_artifact = ""
            # Robust skip: allow skip when either progress hash matches or existing source.md hash matches
            existing_source = os.path.join(chapter_dir, f"{prefix}.source.md")
            source_matches = False
            try:
                if (not split_mode) and os.path.exists(existing_source):
                    source_matches = (compute_hash(safe_read_text(existing_source)) == ch_hash)
            except Exception:
                source_matches = False
            if (not force) and (ch_rec.get("chapter_hash") == ch_hash or source_matches) and all(os.path.exists(p) for p in paths):
                if split_mode:
                    _safe_remove_file(existing_source)
                try:
                    hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, ch_no)
                except Exception:
                    hier_desc = f"第{ch_no}章"
                with _PRINT_LOCK:
                    print(f"[{now_str()}] [章节跳过] {book_name} {hier_desc} 已是最新，跳过")
                def _update_skip(d: Dict[str, Any]) -> None:
                    books = d.setdefault("books", {})
                    b = books.setdefault(bk_key, {})
                    cms = b.setdefault("chapters", {})
                    cr = cms.get(ch_key, {})
                    cr.update({
                        "status": "done",
                        "chapter_hash": ch_hash,
                        "artifacts": {
                            "source": "" if split_mode else os.path.join(chapter_dir, f"{prefix}.source.md"),
                            "deepseek": paths[0],
                            "doubao": paths[1],
                            "kimi": kimi_artifact,
                            "merged": paths[-1],
                        },
                    })
                    cms[ch_key] = cr
                    b["chapters"] = cms
                    books[bk_key] = b
                update_progress_atomic(progress_path, _update_skip)
                # Legacy chapter-level slicing is only needed for non-split chapters.
                if not split_mode:
                    try:
                        _emit_section_outputs(paths[-1], items, chapter_dir)
                    except Exception:
                        pass
                return ChapterResult(
                    part_section_num=part_num,
                    chapter_no=ch_no,
                    prefix=prefix,
                    source_path="" if split_mode else os.path.join(chapter_dir, f"{prefix}.source.md"),
                    deepseek_path=paths[0],
                    doubao_path=paths[1],
                    kimi_path=kimi_artifact,
                    merged_path=paths[-1],
                    chapter_hash=ch_hash,
                    status="done",
                    elapsed_sec=0.0,
                    retries={"deepseek": 0, "doubao": 0, "merge": 0},
                    agent_times={"deepseek": 0.0, "doubao": 0.0, "merge": 0.0},
                )

            try:
                res = process_chapter(
                    book_dir=book_dir,
                    book_name=book_name,
                    chapter_no=ch_no,
                    items=items,
                    retries=retries,
                    retry_backoff=retry_backoff,
                    prompts_root="multi_agent/prompts",
                    incremental_resume=incremental_resume,
                    prev_chapter_hash=ch_rec.get("chapter_hash"),
                    skip_merge_unchanged=skip_merge_unchanged,
                    auto_chunk=auto_chunk,
                )
                def _update_chapter(d: Dict[str, Any]) -> None:
                    books = d.setdefault("books", {})
                    b = books.setdefault(bk_key, {})
                    cms = b.setdefault("chapters", {})
                    cr = cms.get(ch_key, {})
                    cr.update({
                        "status": res.status,
                        "chapter_hash": res.chapter_hash,
                        "artifacts": {
                            "source": res.source_path,
                            "kimi": res.kimi_path,
                            "deepseek": res.deepseek_path,
                            "doubao": res.doubao_path,
                            "merged": res.merged_path,
                        },
                        "agent_times": {},
                        "retries": res.retries,
                    })
                    cms[ch_key] = cr
                    b["chapters"] = cms
                    books[bk_key] = b
                update_progress_atomic(progress_path, _update_chapter)
                return res
            except Exception as e:
                # 先取出到普通局部变量（PEP 3110：except 块退出时 e 会被删除）
                err_msg = str(e)
                # Update failure atomically without relying on shared ch_rec
                def _update_error(d: Dict[str, Any]) -> None:
                    books = d.setdefault("books", {})
                    b = books.setdefault(bk_key, {})
                    cms = b.setdefault("chapters", {})
                    cr = cms.get(ch_key, {})
                    cr.setdefault("errors", []).append(err_msg)
                    cr["status"] = "failed"
                    cms[ch_key] = cr
                    b["chapters"] = cms
                    books[bk_key] = b
                update_progress_atomic(progress_path, _update_error)
                try:
                    hier_desc = _build_hierarchy_desc(part_num, module_num, project_num, ch_no)
                except Exception:
                    hier_desc = f"第{ch_no}章"
                with _PRINT_LOCK:
                    print(f"[章节错误] {book_name} {hier_desc} 出错：{e}")
                return None

        with ThreadPoolExecutor(max_workers=chapter_max_workers) as pool:
            futs = {pool.submit(_process_one_chapter, ch_key, chapter_map[ch_key]): ch_key for ch_key in chapter_keys}
            for fut in as_completed(futs):
                r = fut.result()  # Will re-raise exception if _process_one_chapter raised
                if r is not None:
                    chapter_results.append(r)
        # No need to refresh progress snapshot here; it's already atomic

    # After chapters processed, finalize book according to success count
    recognized_total = len(chapter_keys)
    succeeded = len(chapter_results)
    elapsed_book = time.time() - t_book
    if recognized_total == 0:
        # No recognizable chapters; mark as empty and do NOT write a final file
        def _mark_empty(d: Dict[str, Any]) -> None:
            books = d.setdefault("books", {})
            b = books.setdefault(bk_key, book_rec)
            b.update({
                "status": "empty",
                "final_path": None,
                "elapsed_sec_total": elapsed_book,
                "book_hash": None,
                "chapters_total": 0,
                "chapters_succeeded": 0,
            })
            books[bk_key] = b
        update_progress_atomic(progress_path, _mark_empty)
        try:
            with _PRINT_LOCK:
                print(f"[{now_str()}] [书籍空内容] {know_id}-{book_name} 未识别到章节，不生成成品；用时{format_duration_cn(elapsed_book)}")
        except Exception:
            pass
    elif succeeded == 0:
        # Recognized chapters but all failed; mark failed and do NOT write final file
        def _mark_failed(d: Dict[str, Any]) -> None:
            books = d.setdefault("books", {})
            b = books.setdefault(bk_key, book_rec)
            b.update({
                "status": "failed",
                "final_path": None,
                "elapsed_sec_total": elapsed_book,
                "book_hash": None,
                "chapters_total": recognized_total,
                "chapters_succeeded": 0,
            })
            books[bk_key] = b
        update_progress_atomic(progress_path, _mark_failed)
        try:
            with _PRINT_LOCK:
                print(f"[{now_str()}] [书籍失败] {know_id}-{book_name} 所有{recognized_total}章均失败；用时{format_duration_cn(elapsed_book)}")
        except Exception:
            pass
    else:
        # At least one chapter succeeded; stitch only the succeeded chapters
        final_path = stitch_book_final(know_id, book_name, chapter_results, final_root)
        book_hash = compute_book_hash(chapter_results)
        book_status = "done" if succeeded == recognized_total else "partial"
        def _finalize_book(d: Dict[str, Any]) -> None:
            books = d.setdefault("books", {})
            b = books.setdefault(bk_key, book_rec)
            b.update({
                "status": book_status,
                "final_path": final_path,
                "elapsed_sec_total": elapsed_book,
                "book_hash": book_hash,
                "chapters_total": recognized_total,
                "chapters_succeeded": succeeded,
            })
            books[bk_key] = b
        update_progress_atomic(progress_path, _finalize_book)
        try:
            with _PRINT_LOCK:
                if book_status == "done":
                    print(f"[{now_str()}] [书籍完成] {know_id}-{book_name} 共{recognized_total}章全部成功；用时{format_duration_cn(elapsed_book)}，成品：{final_path}")
                else:
                    print(f"[{now_str()}] [书籍部分完成] {know_id}-{book_name} 成功{succeeded}/{recognized_total}章；用时{format_duration_cn(elapsed_book)}，成品：{final_path}")
        except Exception:
            pass

    # Also write lightweight marker in book dir (inside function scope)
    done_marker = os.path.join(book_dir, "._done.json")
    try:
        with open(done_marker, "w", encoding="utf-8") as f:
            json.dump({
                "know_id": know_id,
                "book_name": book_name,
                "book_hash": locals().get("book_hash", None),
                "final_path": locals().get("final_path", None),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    


def find_book_dirs(root: str) -> List[str]:
    out: List[str] = []
    for fn in os.listdir(root):
        full = os.path.join(root, fn)
        if os.path.isdir(full):
            try:
                _ = parse_book_dirname(full)
                out.append(full)
            except Exception:
                continue
    # sort by know_id asc
    def key(p: str):
        try:
            kid, _ = parse_book_dirname(p)
            return kid
        except Exception:
            return 1 << 30
    return sorted(out, key=key)


def _parse_chapter_tokens(tokens: Optional[List[str]]) -> Optional[List[int]]:
    """Parse a list of chapter tokens like ["1", "3", "5-7"] into a sorted, deduped list of ints.
    Accepts ranges A-B (inclusive). Returns None if tokens is falsy.
    """
    if not tokens:
        return None
    out: set[int] = set()
    for tok in tokens:
        if not tok:
            continue
        # Allow comma-separated within a single token, e.g. "1,2,3"
        parts = [p.strip() for p in re.split(r"[，,]", tok) if p.strip()]
        for p in parts:
            if "-" in p:
                try:
                    a_str, b_str = p.split("-", 1)
                    a = int(a_str.strip())
                    b = int(b_str.strip())
                    if a > b:
                        a, b = b, a
                    for v in range(a, b + 1):
                        if v >= 1:
                            out.add(v)
                except Exception:
                    # ignore malformed
                    continue
            else:
                try:
                    v = int(p)
                    if v >= 1:
                        out.add(v)
                except Exception:
                    continue
    return sorted(out) if out else None


def run_pipeline(books_root: str,
                 final_root: str,
                 progress_path: str,
                 ids: Optional[List[int]],
                 target_chapters: Optional[List[int]],
                 force: bool,
                 max_workers: int,
                 chapter_max_workers: int,
                 retries: int,
                 retry_backoff: int,
                 skip_book_if_final: bool = True,
                 backfill_only: bool = False,
                 incremental_resume: bool = False,
                 skip_merge_unchanged: bool = False,
                 auto_chunk: bool = False,
                 book_lock: bool = True) -> None:
    # Record run start time for summary
    _run_t0 = time.time()
    # Optionally backfill from existing final library and exit
    if backfill_only:
        n = backfill_progress_from_library(final_root, progress_path)
        with _PRINT_LOCK:
            print(f"[{now_str()}] [backfill] updated {n} book record(s) from '{final_root}'")
        return
    books = find_book_dirs(books_root)
    if ids:
        st = set(ids)
        books = [b for b in books if parse_book_dirname(b)[0] in st]

    # Process books sequentially or with limited concurrency
    # We'll use a ThreadPool for overlapping IO and network (LLM calls)
    if max_workers <= 1:
        for b in books:
            try:
                bid, bname = parse_book_dirname(b)
                with _PRINT_LOCK:
                    print(f"[{now_str()}] [开始处理书籍] {bid}-{bname}")
            except Exception:
                with _PRINT_LOCK:
                    print(f"[{now_str()}] [开始处理书籍] {b}")
            if book_lock:
                lock_path = os.path.join(b, "._book.lock")
                with SimpleFileLock(lock_path, timeout=3600, poll_interval=0.2):
                    process_one_book(
                        book_dir=b,
                        final_root=final_root,
                        progress_path=progress_path,
                        force=force,
                        target_chapters=target_chapters,
                        retries=retries,
                        retry_backoff=retry_backoff,
                        skip_book_if_final=skip_book_if_final,
                        incremental_resume=incremental_resume,
                        skip_merge_unchanged=skip_merge_unchanged,
                        chapter_max_workers=chapter_max_workers,
                        auto_chunk=auto_chunk,
                    )
            else:
                process_one_book(
                    book_dir=b,
                    final_root=final_root,
                    progress_path=progress_path,
                    force=force,
                    target_chapters=target_chapters,
                    retries=retries,
                    retry_backoff=retry_backoff,
                    skip_book_if_final=skip_book_if_final,
                    incremental_resume=incremental_resume,
                    skip_merge_unchanged=skip_merge_unchanged,
                    chapter_max_workers=chapter_max_workers,
                    auto_chunk=auto_chunk,
                )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = []
            def _run_book_with_lock(book_path: str) -> None:
                if book_lock:
                    lock_path = os.path.join(book_path, "._book.lock")
                    with SimpleFileLock(lock_path, timeout=3600, poll_interval=0.2):
                        process_one_book(
                            book_dir=book_path,
                            final_root=final_root,
                            progress_path=progress_path,
                            force=force,
                            target_chapters=target_chapters,
                            retries=retries,
                            retry_backoff=retry_backoff,
                            skip_book_if_final=skip_book_if_final,
                            incremental_resume=incremental_resume,
                            skip_merge_unchanged=skip_merge_unchanged,
                            chapter_max_workers=chapter_max_workers,
                            auto_chunk=auto_chunk,
                        )
                else:
                    process_one_book(
                        book_dir=book_path,
                        final_root=final_root,
                        progress_path=progress_path,
                        force=force,
                        target_chapters=target_chapters,
                        retries=retries,
                        retry_backoff=retry_backoff,
                        skip_book_if_final=skip_book_if_final,
                        incremental_resume=incremental_resume,
                        skip_merge_unchanged=skip_merge_unchanged,
                        chapter_max_workers=chapter_max_workers,
                        auto_chunk=auto_chunk,
                    )
            for b in books:
                futs.append(pool.submit(_run_book_with_lock, b))
            for f in concurrent.futures.as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    with _PRINT_LOCK:
                        print(f"[{now_str()}] [book] error: {e}")

    # ----------------------------------------
    # Final summary across processed books
    # ----------------------------------------
    try:
        progress = load_progress(progress_path)
        # Collect processed book ids from current run (books list already filtered by ids if provided)
        processed_ids: List[int] = []
        for b in books:
            try:
                bid, _ = parse_book_dirname(b)
                processed_ids.append(bid)
            except Exception:
                continue

        lines: List[str] = []
        # Run-level header
        _run_t1 = time.time()
        _dur = format_duration_cn(_run_t1 - _run_t0)
        _start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_run_t0))
        _end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_run_t1))
        lines.append(f"[run] 开始: {_start} 结束: {_end} 耗时: {_dur} 处理书籍数: {len(processed_ids)}")
        any_failed = False
        # 使用线程本地存储收集chunk信息，避免并发冲突
        all_chunked_chapters: List[str] = []
        # 收集merge诊断信息(书籍级汇总)
        all_merge_diagnostics: List[Dict[str, Any]] = []
        for bid in processed_ids:
            rec = (progress.get("books", {}) or {}).get(str(bid)) or {}
            book_name = rec.get("book_name") or ""
            status = rec.get("status") or "pending"
            chapters = rec.get("chapters") or {}
            # Determine failed chapters from chapter records
            failed_chs: List[str] = []
            for ch_key, chrec in chapters.items():
                try:
                    if (chrec or {}).get("status") == "failed":
                        failed_chs.append(str(ch_key))
                except Exception:
                    continue
            failed_chs = sorted(set(failed_chs))
            total = rec.get("chapters_total") or (len(chapters) if chapters else 0)
            succ = rec.get("chapters_succeeded") or (total - len(failed_chs) if total else 0)
            elapsed_s = rec.get("elapsed_sec_total") or 0
            final_path = rec.get("final_path") or ""
            header = f"[汇总] {bid}-{book_name} 状态:{status} 成功{succ}/{total} 用时:{format_duration_cn(elapsed_s)} 成品:{final_path}"
            if failed_chs:
                any_failed = True
                header += f" 失败章节:{','.join(str(x) for x in failed_chs)}"
            lines.append(header)
            # Append brief error details per failed chapter (last message)
            for ck in failed_chs:
                errs = (chapters.get(str(ck)) or {}).get("errors") or []
                last_err = errs[-1] if errs else ""
                # Pretty label: try to parse 'pp-cc' into '第pp篇-第cc章'
                label = str(ck)
                try:
                    if isinstance(ck, str) and "-" in ck:
                        a, b = ck.split("-", 1)
                        label = f"第{int(a)}篇-第{int(b)}章"
                except Exception:
                    pass
                lines.append(f"  - {label}失败: {last_err[:300]}")
            
            # 收集AutoChunk信息（每个书籍独立收集，避免并发污染）
            book_chunked_chapters: List[str] = []  # 本书籍的chunk信息
            try:
                book_dir = None
                for b in books:
                    try:
                        if parse_book_dirname(b)[0] == bid:
                            book_dir = b
                            break
                    except Exception:
                        continue
                
                if book_dir and chapters:
                    # 遍历所有章节
                    for ch_key in sorted(chapters.keys()):
                        try:
                            # 推导 chapter prefix
                            if "-" in str(ch_key):
                                prefix = f"{bid}-{book_name}-{ch_key}"
                            else:
                                prefix = f"{bid}-{book_name}-{ch_key}"
                            
                            # 调用_collect_chunk_stats（已线程安全）
                            chunk_stats = _collect_chunk_stats(book_dir, prefix)
                            if chunk_stats:
                                # 有AutoChunk信息
                                agents_list = []
                                for agent, astats in sorted(chunk_stats.items()):
                                    health = astats.get('health_score', 100)
                                    chunks = astats.get('num_chunks', 0)
                                    # 提取agent简称
                                    agent_short = agent.split('_')[0]
                                    agents_list.append(f"{agent_short}({chunks}片段,健康度{health})")
                                if agents_list:
                                    book_chunked_chapters.append(f"{bid}-{book_name} 第{ch_key}章: {', '.join(agents_list)}")
                        except Exception as e:
                            # 静默忽略单个章节的错误
                            pass
            except Exception as e:
                # 静默忽略单个书籍的错误
                pass
            
            # 将本书籍的chunk信息添加到总列表
            all_chunked_chapters.extend(book_chunked_chapters)
            
            # 收集Merge诊断信息(每个书籍独立收集)
            try:
                book_dir = None
                for b in books:
                    try:
                        if parse_book_dirname(b)[0] == bid:
                            book_dir = b
                            break
                    except Exception:
                        continue
                
                if book_dir and chapters:
                    # 遍历所有章节，收集merge诊断文件
                    for ch_key in sorted(chapters.keys()):
                        try:
                            # 推导 chapter prefix
                            if "-" in str(ch_key):
                                prefix = f"{bid}-{book_name}-{ch_key}"
                            else:
                                prefix = f"{bid}-{book_name}-{ch_key}"
                            
                            # 查找诊断文件: merge_diagnostics_{prefix}_*.txt
                            import glob
                            pattern = os.path.join(book_dir, f"merge_diagnostics_{prefix}_*.txt")
                            diag_files = glob.glob(pattern)
                            if diag_files:
                                # 取最新的一个
                                latest_diag = max(diag_files, key=os.path.getmtime)
                                all_merge_diagnostics.append({
                                    "book_id": bid,
                                    "book_name": book_name,
                                    "chapter": ch_key,
                                    "diag_file": latest_diag
                                })
                        except Exception:
                            pass
            except Exception:
                pass

        # 将AutoChunk信息添加到lines列表（这样会写入汇总文件）
        if all_chunked_chapters:
            lines.append("")
            lines.append("----- AutoChunk 触发章节 -----")
            for ch_info in all_chunked_chapters:
                lines.append(f"  {ch_info}")
            lines.append(f"总计: {len(all_chunked_chapters)} 个章节触发了AutoChunk")
            lines.append("说明: (X片段,健康度Y) = 切分为X个片段，健康度评分Y分")

        # Print concise summary to console
        if processed_ids:
            with _PRINT_LOCK:
                print("\n===== 本次运行汇总 =====")
                if lines:
                    print(lines[0])
                    if len(lines) > 1:
                        try:
                            ac_start = next(
                                i for i, ln in enumerate(lines)
                                if ln.startswith("----- AutoChunk 触发章节 -----")
                            )
                            console_lines = lines[1:ac_start]
                        except StopIteration:
                            console_lines = lines[1:]
                        for ln in console_lines:
                            print(ln)
                else:
                    print("(无可汇总记录)")
                
                if any_failed:
                    print("提示: 可用 --ids <know_id> --chapters <列表> 重跑失败章节，例如: --ids 562 --chapters 3 7")
                print("====================\n")

        # Write timestamped summary report under the parent of final_root (e.g., exports/multi_agent/final)
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            report_dir = os.path.dirname(final_root) or "."
            ensure_dir(report_dir)
            report_path = os.path.join(report_dir, f"run_summary_{ts}.txt")
            safe_write_text(report_path, "\n".join(lines))
            with _PRINT_LOCK:
                rel_path = os.path.relpath(report_path).replace('\\', '/')
                print(f"[{now_str()}] [summary] 汇总报告已写入: {rel_path}")
        except Exception:
            pass
    except Exception as e:
        with _PRINT_LOCK:
            print(f"[{now_str()}] [summary] 生成汇总失败: {e}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("multi_agent.chapter_pipeline")
    p.add_argument("--books-root", default=os.path.join("exports", "Organized_Books"), help="Root directory that contains <know_id>-<book_name>_organized/ folders")
    p.add_argument("--final-root", default=os.path.join("exports", "multi_agent", "final", "书籍库"), help="Directory to write final book files '<know_id>-1 <book_name>.md'")
    p.add_argument("--progress-file", default=os.path.join("exports", "multi_agent", "final", "progress.json"), help="Global progress json path")
    p.add_argument("--ids", nargs="*", type=int, default=None, help="Specific know_id list to process; default: all in ascending order")
    p.add_argument("--chapters", nargs="*", default=None, help="Specific chapter numbers or ranges per book, e.g. 1 3 5-7")
    p.add_argument("--force", action="store_true", help="Force re-run even if chapter/book hash unchanged")
    p.add_argument("--max-workers", type=int, default=1, help="Max concurrent books")
    p.add_argument("--chapter-max-workers", type=int, default=1, help="Max concurrent chapters per book")
    # Optional per-provider concurrency limits (override env)
    p.add_argument("--limit-kimi", type=int, default=None, help="Max concurrent Kimi generations per process (overrides KIMI_MAX_CONCURRENCY env)")
    p.add_argument("--limit-deepseek", type=int, default=None, help="Max concurrent Deepseek generations per process (overrides DEEPSEEK_MAX_CONCURRENCY env)")
    p.add_argument("--limit-doubao", type=int, default=None, help="Max concurrent Doubao generations per process (overrides DOUBAO_MAX_CONCURRENCY env)")
    p.add_argument("--retries", type=int, default=2, help="Retries per agent and merge")
    p.add_argument("--retry-backoff", type=int, default=10, help="Initial backoff seconds for retries (exponential)")
    p.add_argument("--backfill-only", action="store_true", help="Scan final-root for existing '<know_id>-1 <book_name>.md' and backfill progress.json, then exit")
    p.add_argument("--no-skip-book-if-final", dest="skip_book_if_final", action="store_false", help="Do not early-skip books that already have final_path in progress.json")
    p.set_defaults(skip_book_if_final=True)
    p.add_argument("--resume-partial", dest="resume_partial", action="store_true", help="When chapter content unchanged, only re-run missing agent outputs and then merge; otherwise rerun all")
    p.add_argument("--skip-merge-unchanged", dest="skip_merge_unchanged", action="store_true", help="When chapter content unchanged and all agent outputs+merged exist and no agent re-ran, skip merge step")
    # Book-level lock (enabled by default)
    p.add_argument("--no-book-lock", dest="book_lock", action="store_false", help="Disable per-book lock file to prevent concurrent processing of the same book across processes")
    # Auto-chunking for oversized prompts (off by default)
    p.add_argument("--auto-chunk", dest="auto_chunk", action="store_true", help="Enable semantic auto-chunking for oversized prompts with sliding window and code-fence handling")
    p.set_defaults(book_lock=True)
    return p


def main() -> None:
    ap = build_arg_parser()
    args = ap.parse_args()
    # Apply provider concurrency limits if specified via CLI
    try:
        set_provider_concurrency_limits(
            kimi=getattr(args, "limit_kimi", None),
            deepseek=getattr(args, "limit_deepseek", None),
            doubao=getattr(args, "limit_doubao", None),
        )
    except Exception:
        pass
    chapters = None
    try:
        chapters = _parse_chapter_tokens(getattr(args, "chapters", None))
    except Exception:
        chapters = None
    run_pipeline(
        books_root=args.books_root,
        final_root=args.final_root,
        progress_path=args.progress_file,
        ids=args.ids,
        target_chapters=chapters,
        force=args.force,
        max_workers=int(args.max_workers or 1),
        chapter_max_workers=int(getattr(args, "chapter_max_workers", 1) or 1),
        retries=int(args.retries or 0),
        retry_backoff=int(args.retry_backoff or 10),
        skip_book_if_final=bool(getattr(args, "skip_book_if_final", True)),
        backfill_only=bool(getattr(args, "backfill_only", False)),
        incremental_resume=bool(getattr(args, "resume_partial", False)),
        skip_merge_unchanged=bool(getattr(args, "skip_merge_unchanged", False)),
        auto_chunk=bool(getattr(args, "auto_chunk", False)),
        book_lock=bool(getattr(args, "book_lock", True)),
    )


if __name__ == "__main__":
    main()
