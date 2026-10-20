from __future__ import annotations

import argparse
import json
import re
import os
from typing import List, Dict, Any
import glob
import threading

# Global print lock for orderly console output across threads
_PRINT_LOCK = threading.Lock()

# 流式输出缓冲区（减少锁竞争）
_thread_local = threading.local()


def _get_stream_buffer():
    """获取线程本地流式输出缓冲区"""
    if not hasattr(_thread_local, 'buffer'):
        _thread_local.buffer = []
        _thread_local.last_flush = time.time()
        _thread_local.char_count = 0
        _thread_local.is_thinking = False  # 标记是否为思维链内容
    return _thread_local.buffer


def _flush_stream_buffer(force: bool = False, is_thinking: bool = False):
    """批量刷新流式输出缓冲区

    策略：根据内容类型动态调整缓冲阈值
    - 思维链（thinking）：300字符或1.0秒（用户对延迟不敏感）
    - 正文（content）：150字符或0.5秒（平衡实时性和性能）
    """
    if not hasattr(_thread_local, 'buffer'):
        return

    buf = _thread_local.buffer
    now = time.time()
    elapsed = now - _thread_local.last_flush

    # 根据内容类型选择不同的刷新阈值
    if is_thinking or getattr(_thread_local, 'is_thinking', False):
        # 思维链：更激进的缓冲（用户不急着看）
        char_threshold = 300
        time_threshold = 1.0
    else:
        # 正文内容：适中的缓冲（保持一定实时性）
        char_threshold = 150
        time_threshold = 0.5

    # 刷新条件：强制刷新、字符数达标、或时间间隔达标
    should_flush = force or _thread_local.char_count >= char_threshold or elapsed > time_threshold

    if should_flush and buf:
        content = "".join(buf)
        with _PRINT_LOCK:
            print(content, end="", flush=True)
        buf.clear()
        _thread_local.char_count = 0
        _thread_local.last_flush = now


import requests

from .utils import safe_read_text, safe_write_text
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


def read_files(paths: List[str]) -> List[Dict[str, str]]:
    items = []
    for p in paths:
        text = safe_read_text(p)
        items.append({"path": p, "text": text})
    return items


def build_messages(system_files: List[str], input_files: List[str], extra_instructions: str | None = None) -> List[
    Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    # system prompts
    for sf in system_files:
        content = safe_read_text(sf)
        messages.append({"role": "system", "content": content})
    # guardrail for rendering preference
    guard = (
        "统一输出规范：\n"
        "1) 标题层级统一：顶层一律使用 ##，子级依次使用 ###、####、#####（最多到 #####；更深层用列表/加粗小标题承接），不要使用 # 作为一级标题；\n"
        "2) 标题编号保持原格式：保留教材原有编号（如'第一节'或'一、'），禁止混用编号（如'一、第一节'是错误的）；\n"
        "3) 一次性完整输出：禁止任何交互提示（如'输入1或继续'、'是否继续'），不等待用户输入；\n"
        "4) Markdown 规则：避免整篇代码块包裹全文；使用标准标题/列表；表格使用 GFM 且表格后空一行；\n"
        "5) 流程与对比：流程使用 A → B → C；对比使用 GFM 表格；\n"
    )
    messages.append({"role": "system", "content": guard})
    if extra_instructions:
        messages.append({"role": "system", "content": extra_instructions})

    # user message packs input files
    chunks: List[str] = []
    chunks.append("以下为本地输入文件内容，请据此完成任务：\n")
    for idx, f in enumerate(read_files(input_files), start=1):
        lang = "markdown" if f["path"].lower().endswith(".md") else "text"
        chunks.append(f"=== 文件{idx}: {f['path']} ===")
        chunks.append(f"```{lang}\n{f['text']}\n```")
        chunks.append("")
    messages.append({"role": "user", "content": "\n".join(chunks)})
    return messages


# -------------------------------
# Token budgeting helpers
# -------------------------------

def _detect_deepseek(provider: str, base: str | None, model: str | None, agent_name: str | None) -> bool:
    try:
        b = (base or "").lower()
        m = (model or "").lower()
        a = (agent_name or "").lower()
        return provider == "openai_compat" and (
                    "deepseek" in b or "deepseek" in m or "reasoner" in m or "deepseek" in a or "reasoner" in a)
    except Exception:
        return False


def _context_limit_for_model(provider: str, base: str | None, model: str | None, agent_name: str | None) -> int | None:
    # Provider/model heuristics for context windows (conservative when uncertain)
    # DeepSeek Reasoner
    if _detect_deepseek(provider, base, model, agent_name):
        return 131072
    # Kimi (Moonshot)
    try:
        b = (base or "").lower()
        m = (model or "").lower()
        a = (agent_name or "").lower()
        if provider == "kimi" or "moonshot" in b or "kimi" in m or "kimi" in a:
            # Kimi often supports very large windows. Use a conservative 200k to avoid oversize errors.
            return 200000
    except Exception:
        pass
    # Doubao (ByteDance Ark)
    try:
        b = (base or "").lower()
        m = (model or "").lower()
        a = (agent_name or "").lower()
        if (provider == "openai_compat" and ("ark" in b or "volces" in b)) or "doubao" in m or "doubao" in a:
            # Many Doubao variants have 32k-128k contexts; choose 64k conservatively
            return 65536
    except Exception:
        pass
    # Gemini (usually 1M tokens for 1.5 family)
    if provider == "gemini":
        return 1000000
    # OpenAI (if used)
    try:
        b = (base or "").lower()
        if provider == "openai" or "api.openai" in b:
            # Default to 128k for modern GPT-4o/4.1 family
            return 128000
    except Exception:
        pass
    return None


def _estimate_tokens_text(text: str) -> int:
    """Best-effort token estimate.
    Try tiktoken (o200k_base/cl100k_base). Fallback heuristics for CJK vs Latin.
    """
    try:
        import tiktoken  # type: ignore
        enc = None
        try:
            enc = tiktoken.get_encoding("o200k_base")  # larger-context encoding
        except Exception:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                pass
        if enc is None:
            try:
                enc = tiktoken.encoding_for_model("gpt-4o")
            except Exception:
                enc = None
        if enc is not None:
            return len(enc.encode(text))
    except Exception:
        pass
    # Heuristic fallback
    # Roughly: CJK-heavy text ~1 char per token; Latin-heavy text ~ 3-4 chars per token
    try:
        cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        ratio_cjk = cjk / max(1, len(text))
        if ratio_cjk > 0.25:
            # CJK-dominant
            return int(len(text) * 1.0)
        else:
            return int(len(text) / 3.2)
    except Exception:
        return max(1, len(text) // 3)


def _estimate_tokens_messages(messages: List[Dict[str, str]]) -> int:
    total = 0
    for m in messages:
        # include a small overhead per message
        total += 6  # role/format overhead approximation
        total += _estimate_tokens_text(m.get("content", ""))
    return total


def _dynamic_prompt_budget(context_limit: int, max_tokens: int, current_usage: int) -> int:
    """根据当前使用量动态调整提示预算，兼顾安全缓冲和模型上下文窗口。

    优化策略：目标使用率65-70%，避免接近边界导致的质量下降和溢出风险。
    """
    MAX_CONTEXT_WINDOW = max(1, int(context_limit))
    ABSOLUTE_MAX = MAX_CONTEXT_WINDOW
    usage_ratio = float(current_usage) / float(MAX_CONTEXT_WINDOW)
    # 基础预算：上下文窗口 - 生成上限 - 安全缓冲（增大到2048）
    base_budget = max(2048, MAX_CONTEXT_WINDOW - int(max_tokens or 0) - 2048)
    # 调整预算策略：更激进地使用上下文窗口，提升分片质量
    if usage_ratio > 0.85:
        adj = int(MAX_CONTEXT_WINDOW * 0.75) - int(max_tokens or 0)
        base_budget = max(2048, adj)
    elif usage_ratio > 0.7:
        adj = int(MAX_CONTEXT_WINDOW * 0.82) - int(max_tokens or 0)
        base_budget = max(2048, adj)
    # 默认目标：约72%利用率，提升分片大小
    else:
        adj = int(MAX_CONTEXT_WINDOW * 0.72) - int(max_tokens or 0)
        base_budget = max(2048, adj)
    return base_budget


def _balance_code_fences(text: str) -> str:
    try:
        # Balance both ``` and ~~~ fences based on line-level fence toggles
        fence_re = re.compile(r"(?m)^[ \t]*(```+|~~~+)[^\n]*$")
        backtick_open = False
        tilde_open = False
        last_unclosed = None  # store the marker string of last unmatched fence
        for m in fence_re.finditer(text):
            marker = m.group(1)
            if marker.startswith("```"):
                backtick_open = not backtick_open
                last_unclosed = marker if backtick_open else None
            elif marker.startswith("~~~"):
                tilde_open = not tilde_open
                last_unclosed = marker if tilde_open else None
        # Append required closing fences (close the last opened first)
        closing = ""
        if backtick_open or tilde_open:
            if not text.endswith("\n"):
                closing += "\n"
            # Close the last unmatched first to maintain visual continuity
            if last_unclosed is not None:
                closing += ("```" if last_unclosed.startswith("```") else "~~~")
                # if both remained open (rare), close the other as well
                if backtick_open and tilde_open:
                    closing += "\n" + ("~~~" if last_unclosed.startswith("```") else "```")
            else:
                # Fallback if we lost track
                if backtick_open:
                    closing += "```"
                if tilde_open:
                    if closing and not closing.endswith("\n"):
                        closing += "\n"
                    closing += "~~~"
        return text + closing
    except Exception:
        return text


def _truncate_user_message_to_budget(messages: List[Dict[str, str]], prompt_budget: int,
                                     print_lock: threading.Lock | None = None) -> None:
    """In-place truncate the single user message content to fit prompt_budget tokens.
    Keeps system messages intact. Uses binary search over character length with token estimates.
    """
    if not messages:
        return
    # Identify system vs user
    system_msgs = [m for m in messages if m.get("role") == "system"]
    user_idxs = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not user_idxs:
        return
    user_idx = user_idxs[-1]

    # If already within budget, nothing to do
    est_total = _estimate_tokens_messages(messages)
    if est_total <= prompt_budget:
        return

    # Compute tokens consumed by non-user messages
    non_user_tokens = _estimate_tokens_messages([m for i, m in enumerate(messages) if i != user_idx])
    # Ensure we have room for some user content
    target_user_tokens = max(256, prompt_budget - non_user_tokens)

    user_content = messages[user_idx].get("content", "")
    # Binary search char length to fit user tokens
    lo, hi = 0, len(user_content)
    best_text = user_content[:0]
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = _balance_code_fences(user_content[:mid])
        messages[user_idx]["content"] = cand
        est = _estimate_tokens_messages(messages)
        if est <= prompt_budget:
            best_text = cand
            lo = mid + 1
        else:
            hi = mid - 1

    if best_text:
        messages[user_idx]["content"] = best_text
    else:
        # As a last resort, keep a very small head with a notice
        head = user_content[:2048]
        head = _balance_code_fences(head)
        notice = "\n\n[... 输入内容过长，已截断以满足上下文窗口限制 ...]"
        messages[user_idx]["content"] = head + notice

    try:
        if print_lock:
            with print_lock:
                new_total = _estimate_tokens_messages(messages)
                print(
                    f"[guard] Truncated user content to fit prompt budget: ~{new_total} tokens (budget {prompt_budget})")
    except Exception:
        pass


# -----------------------------------
# Semantic break and code-fence utils
# -----------------------------------
def _find_semantic_break(text: str, start: int, candidate_end: int, window: int = 1200) -> int:
    """Find a semantically better break position near candidate_end.
    Preference order (增强版):
    1) Markdown H2/H3 boundary: "\n## ", "\n### "
    2) 增强章节/标题/序号识别（#1-#6、中文序号、数字序号、粗体标题、列表/引用块）
    3) 段落边界：多空行、列表项、引用块
    4) 语义单元边界：句子结束 + 换行
    5) Fallback: 最后一个换行或句号

    仅在 candidate_end 之前的滑动窗口内搜索，返回绝对索引；未找到返回 -1。
    """
    try:
        if candidate_end <= start:
            return -1
        ws = max(start, candidate_end - max(200, window))
        seg = text[ws:candidate_end]
        # 优先：明确的 H2/H3 边界
        for sep in ["\n## ", "\n### "]:
            pos = seg.rfind(sep)
            if pos != -1:
                return ws + pos
        # 1) 增强章节/标题识别
        section_patterns = [
            r"\n#{1,6}\s+",  # Markdown 标题
            r"\n\*\*\s+",  # 粗体标题（近似识别）
            r"\n\s*[（(]?[一二三四五六七八九十百千]+[、\.\)）]",  # 中文序号（含括号）
            r"\n\s*[0-9]+[\.\)]",  # 数字序号
            r"\n\s*(?:[IVXLCDM]+)[\.\)]\s+",  # 罗马数字序号
            r"\n=== 文件[^\n]*$",  # 文件分界标记
            r"\n\s*第[一二三四五六七八九十百千0-9]+\s*(章|节|讲|篇|部分)\b",  # 中文章节模板
        ]
        # 2) 段落结构
        paragraph_breaks = [
            r"\n\n+",  # 多个换行
            r"\n\s*[-*+]\s+",  # 列表项（含+）
            r"\n\s*>\s+",  # 引用块
            r"\n[ \t]*[-*_]{3,}[ \t]*\n",  # 水平分隔线
        ]
        # 3) 语义单元边界
        semantic_units = [
            r"[。！？]\s*\n",  # 句子结束 + 换行
            r"\.\s+\n",  # 英文句号 + 换行
        ]

        def _last_regex_pos(patterns: list[str]) -> int:
            last = -1
            for pat in patterns:
                try:
                    for m in re.finditer(pat, seg, flags=re.MULTILINE):
                        last = max(last, m.start())
                except Exception:
                    continue
            return last

        p = _last_regex_pos(section_patterns)
        if p != -1:
            return ws + p
        p = _last_regex_pos(paragraph_breaks)
        if p != -1:
            return ws + p
        p = _last_regex_pos(semantic_units)
        if p != -1:
            return ws + p
        # 次级回退：句号
        for sep in ["。", "."]:
            pos = seg.rfind(sep)
            if pos != -1:
                return ws + pos
        # 最终回退：最后一个换行
        nl = seg.rfind("\n")
        if nl >= 0:
            return ws + nl
        return -1
    except Exception:
        return -1


def _last_code_fence_lang(text: str, start: int, end: int) -> str:
    """Detect the language of the last opening code fence (```lang) between [start, end).
    Returns language token or empty string if unknown.
    """
    try:
        seg = text[start:end]
        fpos = seg.rfind("```")
        if fpos == -1:
            return ""
        # Move to after backticks within the segment
        abs_pos = start + fpos
        # Find end of the fence line
        try:
            line_end = text.find("\n", abs_pos)
            if line_end == -1:
                line_end = end
        except Exception:
            line_end = end
        lang = text[abs_pos + 3:line_end].strip().strip("`")
        # guard against accidental long captures
        if len(lang) > 32:
            return ""
        return lang
    except Exception:
        return ""


def _last_open_fence(text: str, start: int, end: int) -> tuple[str, str]:
    """Return (marker, lang) for the last OPEN (unclosed) fence in [start, end).
    marker is one of ("```", "~~~") or empty if none is open.
    """
    try:
        seg = text[start:end]
        fence_re = re.compile(r"(?m)^[ \t]*(```+|~~~+)([^\n]*)$")
        bt_open = False
        td_open = False
        last_bt_lang = ""
        last_td_lang = ""
        for m in fence_re.finditer(seg):
            marker = m.group(1)
            lang_raw = (m.group(2) or "").strip().strip("`~")
            if len(lang_raw) > 32:
                lang_raw = ""
            if marker.startswith("```"):
                bt_open = not bt_open
                if bt_open:
                    last_bt_lang = lang_raw
            else:
                td_open = not td_open
                if td_open:
                    last_td_lang = lang_raw
        if bt_open:
            return ("```", last_bt_lang)
        if td_open:
            return ("~~~", last_td_lang)
        return ("", "")
    except Exception:
        return ("", "")


# -------------------------------
# 内容类型识别与自适应重叠策略
# -------------------------------

def _detect_content_type(text: str) -> str:
    """自动识别内容类型：code / textbook / technical / narrative"""
    try:
        lines = text.splitlines()
        code_blocks = len(re.findall(r"(?:```[\s\S]*?```|~~~[\s\S]*?~~~)", text))
        code_lines = len(re.findall(r"^\s*(def|class|import|function)\b", text, re.MULTILINE))
        headings = len(re.findall(r"^#{1,6}\s+", text, re.MULTILINE))
        lists = len(re.findall(r"^\s*[-*]\s+", text, re.MULTILINE))
        tables = len(re.findall(r"\|.*\|", text))
        code_ratio = (code_blocks * 10 + code_lines) / max(1, len(lines))
        heading_density = headings / max(1, len(lines))
        if code_ratio > 0.1:
            return "code"
        elif heading_density > 0.05:
            return "textbook"
        elif lists > 10 or tables > 3:
            return "technical"
        else:
            return "narrative"
    except Exception:
        return "technical"


def _adaptive_overlap_strategy(content_type: str, chunk_size: int) -> tuple[int, int]:
    """根据内容类型与分片大小动态调整重叠长度，返回(output_tail_chars, input_tail_chars)。

    优化：适度增加重叠窗口，提升片段间上下文连贯性。
    """
    overlap_configs = {
        "code": (1200, 800),  # 代码：增加重叠以保持代码块完整性
        "textbook": (2200, 1600),  # 教材：大幅增加重叠，提升章节连贯性
        "technical": (1800, 1200),  # 技术文档：增加重叠以保持技术概念连续性
        "narrative": (2600, 2000),  # 叙述性：最大重叠，保持故事流畅性
    }
    base_output, base_input = overlap_configs.get(content_type, (1400, 1000))
    scale_factor = min(2.5, max(0.6, float(chunk_size) / 4000.0))
    return int(base_output * scale_factor), int(base_input * scale_factor)


# Chunking utilities
def _find_safe_break(text: str, start: int, candidate_end: int, semantic_window: int = 2000) -> int:
    """Find a safe break position at or before candidate_end.
    Priority:
    1) Avoid splitting inside code fences. If segment indicates an unbalanced fence, backtrack before the last fence, preferably to a newline.
    2) Prefer semantic boundaries near candidate_end: H2/H3 headings, paragraph breaks, sentence boundaries.
    3) Prefer breaking at the last newline within the segment.
    Returns an end index >= start.
    """
    if candidate_end <= start:
        return start
    seg = text[start:candidate_end]
    try:
        # Toggle fences line-by-line to determine if inside a code block
        fence_re = re.compile(r"(?m)^[ \t]*(```+|~~~+)[^\n]*$")
        bt_open = False
        td_open = False
        last_fence = -1
        for m in fence_re.finditer(seg):
            marker = m.group(1)
            if marker.startswith("```"):
                bt_open = not bt_open
            else:
                td_open = not td_open
            last_fence = m.start()
        if bt_open or td_open:
            # Likely inside a code block — first try to semantically break BEFORE the last fence
            if last_fence > 0:
                # Restrict semantic search window to content before last fence
                sem_end = start + last_fence
                sem_break = _find_semantic_break(text, start, sem_end, window=semantic_window)
                if sem_break != -1 and sem_break > start:
                    return sem_break
                # Otherwise, try newline prior to the fence
                safe_cut = seg.rfind("\n", 0, last_fence)
                if safe_cut > 0:
                    return start + safe_cut
                return start + last_fence
            # If no fence found, fallback to semantic break or last newline
            sem_break = _find_semantic_break(text, start, candidate_end, window=semantic_window)
            if sem_break != -1 and sem_break > start:
                return sem_break
            nl = seg.rfind("\n")
            if nl >= 0:
                return start + nl
            return max(start, candidate_end - 1)
        # Balanced fences: try semantic boundaries first
        sem_break = _find_semantic_break(text, start, candidate_end)
        if sem_break != -1 and sem_break > start:
            return sem_break
        # Fallback to last newline in range
        nl = seg.rfind("\n")
        if nl >= 0:
            return start + nl
        return candidate_end
    except Exception:
        # Fallback to last newline if any
        nl = seg.rfind("\n")
        if nl >= 0:
            return start + nl
        return candidate_end


def _split_user_into_chunk_messages(messages: List[Dict[str, str]], prompt_budget: int, guidance_margin: int = 768,
                                    semantic_window: int = 2000, fallback_step: int = 1024) -> List[
    List[Dict[str, str]]]:
    """Split the single-user-content messages into multiple chunks that each fit prompt_budget tokens.
    Strategy:
    1) Prefer to split on per-file boundaries as marked by lines starting with '=== 文件'.
    2) If a single file segment still exceeds budget, split within that segment using binary search and safe breaks.
    Keeps system messages intact. Returns a list of message lists (one per chunk).
    """
    if not messages:
        return [messages]
    user_idxs = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not user_idxs:
        return [messages]
    user_idx = user_idxs[-1]

    base_messages = [m.copy() for m in messages]
    user_content = base_messages[user_idx].get("content", "")

    # Split content into segments by file markers
    lines = user_content.splitlines(keepends=True)
    segments: List[str] = []
    cur: List[str] = []
    for ln in lines:
        if ln.startswith("=== 文件") and cur:
            segments.append("".join(cur))
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        segments.append("".join(cur))

    # If we failed to detect markers (single segment), fall back to whole text
    if not segments:
        segments = [user_content]

    # Pre-calc tokens of non-user part
    non_user_tokens = _estimate_tokens_messages([m for i, m in enumerate(base_messages) if i != user_idx])
    # Keep a small margin for later injecting per-chunk guidance prefix
    effective_prompt_budget = max(512, prompt_budget - guidance_margin)

    chunks: List[List[Dict[str, str]]] = []
    acc = ""
    first_segment = True

    def flush_acc() -> None:
        nonlocal acc
        if acc:
            cm = [m.copy() for m in base_messages]
            cm[user_idx] = {"role": "user", "content": _balance_code_fences(acc)}
            chunks.append(cm)
            acc = ""

    for seg in segments:
        # Try to add whole segment
        candidate = acc + seg if acc else seg
        cand_msgs = [m.copy() for m in base_messages]
        cand_msgs[user_idx] = {"role": "user", "content": _balance_code_fences(candidate)}
        est = _estimate_tokens_messages(cand_msgs)
        if est <= effective_prompt_budget:
            acc = candidate
            continue

        # If current acc is non-empty, finalize it first
        if acc:
            flush_acc()

        # Now handle seg alone: if fits, start new acc; else split within seg
        single_msgs = [m.copy() for m in base_messages]
        single_msgs[user_idx] = {"role": "user", "content": _balance_code_fences(seg)}
        if _estimate_tokens_messages(single_msgs) <= effective_prompt_budget:
            acc = seg
            continue

        # Split inside this large seg by binary search windows
        start = 0
        n = len(seg)
        # If we cut inside a code block, we will close the fence at the end of this part
        # and reopen it at the beginning of the next part using the detected language.
        pending_reopen_marker = ""
        pending_reopen_lang = ""
        while start < n:
            lo, hi = start + 1, n
            best_e = min(start + 2000, n)
            while lo <= hi:
                mid = (lo + hi) // 2
                e = _find_safe_break(seg, start, mid, semantic_window)
                # If we owe an opening fence from previous cut, prepend a lightweight placeholder
                # for estimation purposes (language token has minor impact on tokens)
                if pending_reopen_marker:
                    test_piece = f"{pending_reopen_marker}{pending_reopen_lang}\n" + seg[start:e]
                else:
                    test_piece = seg[start:e]
                test_content = _balance_code_fences(test_piece)
                test_msgs = [m.copy() for m in base_messages]
                test_msgs[user_idx] = {"role": "user", "content": test_content}
                if _estimate_tokens_messages(test_msgs) <= effective_prompt_budget:
                    best_e = max(best_e, e)
                    lo = mid + 1
                else:
                    hi = mid - 1

            if best_e <= start:
                # Force forward progress: try newline within an expanded window, else minimal step
                fb = fallback_step // 2 if pending_reopen_marker else fallback_step
                sub_seg = seg[start:min(n, start + fb * 2)]
                forced_nl = sub_seg.rfind('\n')
                if forced_nl > 0:
                    best_e = start + forced_nl
                else:
                    best_e = min(n, start + fb)
                if best_e <= start:
                    best_e = start + 1

            # Build part content with smart code-fence handling
            current_slice = seg[start:best_e]
            # If previous part ended inside a code block, reopen it for continuity
            if pending_reopen_marker:
                reopen_prefix = f"{pending_reopen_marker}{pending_reopen_lang}\n"
            else:
                reopen_prefix = ""
            part_content = _balance_code_fences(reopen_prefix + current_slice)
            # If we cut inside a code block (odd number of fences in this slice),
            # plan to reopen the same language fence at the beginning of the next part.
            try:
                fence_re = re.compile(r"(?m)^[ \t]*(```+|~~~+)[^\n]*$")
                bt_open = False
                td_open = False
                for m in fence_re.finditer(current_slice):
                    marker = m.group(1)
                    if marker.startswith("```"):
                        bt_open = not bt_open
                    else:
                        td_open = not td_open
                inside_code = bt_open or td_open
            except Exception:
                inside_code = False
            if inside_code:
                marker, lang = _last_open_fence(seg, start, best_e)
                pending_reopen_marker = marker
                pending_reopen_lang = lang
            else:
                pending_reopen_marker = ""
                pending_reopen_lang = ""
            pm = [m.copy() for m in base_messages]
            pm[user_idx] = {"role": "user", "content": part_content}
            chunks.append(pm)
            start = best_e

    # Flush any remaining acc
    flush_acc()

    return chunks


# --- Chunk quality evaluation ---
def _calculate_semantic_integrity(chunk_text: str) -> float:
    try:
        # Sentence end ratio: prefer chunk ending at sentence boundary or newline
        ends_good = bool(re.search(r'[。！？\.!?]\s*$', chunk_text)) or chunk_text.strip().endswith('\n')
        sentence_score = 1.0 if ends_good else 0.5
        # Code fence balance for both ``` and ~~~
        balanced_bt = (chunk_text.count('```') % 2 == 0)
        balanced_td = (chunk_text.count('~~~') % 2 == 0)
        fence_balance = 1.0 if (balanced_bt and balanced_td) else 0.5
        return (sentence_score + fence_balance) / 2.0
    except Exception:
        return 0.5


def _calculate_boundary_quality(chunk_text: str, idx: int, chunks: List[List[Dict[str, str]]]) -> float:
    try:
        start_ok = chunk_text.lstrip().startswith('## ') or chunk_text.lstrip().startswith(
            '### ') or chunk_text.startswith('\n')
        end_ok = chunk_text.rstrip().endswith('\n') or bool(re.search(r'[。！？\.!?]\s*$', chunk_text))
        return (1.0 if start_ok else 0.6) * 0.5 + (1.0 if end_ok else 0.6) * 0.5
    except Exception:
        return 0.6


def _calculate_coherence_score(chunk_text: str, chunks: List[List[Dict[str, str]]], i: int) -> float:
    try:
        # Simple heuristic: chunks that end mid-word or mid-code reduce coherence
        bad_tail = bool(re.search(r'\w$|`$', chunk_text)) and not chunk_text.endswith('\n')
        return 0.7 if not bad_tail else 0.5
    except Exception:
        return 0.6


def _evaluate_chunk_quality(chunks: List[List[Dict[str, str]]]) -> Dict[str, Dict[str, float]]:
    scores: Dict[str, Dict[str, float]] = {}
    try:
        texts: List[str] = []
        for cm in chunks:
            try:
                txt = next((m.get('content', '') for m in cm if m.get('role') == 'user'), '')
                texts.append(txt)
            except Exception:
                texts.append('')
        for i, chunk in enumerate(texts):
            semantic_score = _calculate_semantic_integrity(chunk)
            boundary_score = _calculate_boundary_quality(chunk, i, chunks)
            coherence_score = _calculate_coherence_score(chunk, chunks, i)
            # 调整权重：语义完整性更重要，边界和连贯性次之
            overall = (semantic_score * 0.5 + boundary_score * 0.3 + coherence_score * 0.2)
            scores[f'chunk_{i}'] = {
                'semantic': semantic_score,
                'boundary': boundary_score,
                'coherence': coherence_score,
                'overall': overall,
            }
    except Exception:
        pass
    return scores


# Enhanced guidance for chunked runs
def _extract_context_keywords(text: str, max_terms: int = 5) -> str:
    """Extract key terms from previous context for continuity guidance"""
    try:
        lines = text.split('\n')
        terms = []

        # Extract from headers
        for line in lines:
            line = line.strip()
            if line.startswith('## ') or line.startswith('### '):
                clean_title = re.sub(r'^#+\s*', '', line)
                # Extract meaningful words (Chinese characters or important terms)
                words = re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}', clean_title)
                terms.extend(words)
            elif re.search(r'\*\*.+?\*\*', line):
                # Extract bold content
                bold_terms = re.findall(r'\*\*(.+?)\*\*', line)
                terms.extend(bold_terms)

        # Remove duplicates and limit
        unique_terms = list(dict.fromkeys(terms))[:max_terms]
        return "、".join(unique_terms) if unique_terms else ""
    except Exception:
        return ""


def _extract_last_heading(text: str) -> tuple[str, int]:
    """提取文本中最后一个标题及其层级。

    Returns:
        (heading_text, level) - 标题文本和层级（2=##, 3=###, 4=####），未找到返回("", 0)
    """
    try:
        lines = text.split('\n')
        for line in reversed(lines):
            line = line.strip()
            match = re.match(r'^(#{2,5})\s+(.+)$', line)
            if match:
                level = len(match.group(1))
                heading = match.group(2).strip()
                return heading, level
        return "", 0
    except Exception:
        return "", 0


def _extract_key_entities(text: str, max_entities: int = 8) -> list[str]:
    """提取文本中的关键实体（专有名词、术语、数字等）用于上下文一致性。

    Returns:
        关键实体列表
    """
    entities = []
    try:
        # 1. 提取加粗内容（通常是重要术语）
        bold_terms = re.findall(r'\*\*(.+?)\*\*', text)
        entities.extend(bold_terms)

        # 2. 提取【类型】标签内容
        label_terms = re.findall(r'【([^】]+)】', text)
        entities.extend(label_terms)

        # 3. 提取行内代码
        code_terms = re.findall(r'`([^`]+)`', text)
        entities.extend([t for t in code_terms if len(t) < 30])

        # 4. 提取带数字的术语（如"第1章"、"步骤3"）
        numbered_terms = re.findall(r'[\u4e00-\u9fff]+\d+[\u4e00-\u9fff]*', text)
        entities.extend(numbered_terms)

        # 5. 提取英文专有名词（首字母大写）
        proper_nouns = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)
        entities.extend(proper_nouns)

        # 去重并限制数量
        unique = list(dict.fromkeys(entities))[:max_entities]
        return unique
    except Exception:
        return []


def _find_semantic_boundary(text: str, target_length: int, search_range: int = 500) -> int:
    """在目标长度附近找到语义边界（段落、句子结尾）。

    Args:
        text: 要处理的文本
        target_length: 目标截取长度
        search_range: 在目标长度前后搜索的范围

    Returns:
        最佳截取位置
    """
    if len(text) <= target_length:
        return len(text)

    try:
        # 搜索范围
        start = max(0, target_length - search_range)
        end = min(len(text), target_length + search_range // 2)
        search_text = text[start:end]

        # 优先级：段落 > 句号 > 分号/冒号 > 逗号
        boundaries = []

        # 1. 段落边界（双换行）
        for m in re.finditer(r'\n\n', search_text):
            pos = start + m.end()
            if abs(pos - target_length) < search_range:
                boundaries.append((pos, 0))  # 优先级0最高

        # 2. 句号结尾
        for m in re.finditer(r'[。！？.!?]\s*\n?', search_text):
            pos = start + m.end()
            if abs(pos - target_length) < search_range:
                boundaries.append((pos, 1))

        # 3. 分号/冒号结尾
        for m in re.finditer(r'[；：;:]\s*\n?', search_text):
            pos = start + m.end()
            if abs(pos - target_length) < search_range:
                boundaries.append((pos, 2))

        # 4. 换行符
        for m in re.finditer(r'\n', search_text):
            pos = start + m.end()
            if abs(pos - target_length) < search_range:
                boundaries.append((pos, 3))

        if boundaries:
            # 按优先级排序，同优先级按接近目标长度排序
            boundaries.sort(key=lambda x: (x[1], abs(x[0] - target_length)))
            return boundaries[0][0]

        return target_length
    except Exception:
        return target_length


def _build_context_summary(prev_output: str, prev_input: str, max_chars: int = 800) -> str:
    """构建结构化的上下文摘要，用于自然衔接。

    Returns:
        格式化的上下文摘要字符串
    """
    summary_parts = []

    try:
        # 1. 提取最后标题
        last_heading, heading_level = _extract_last_heading(prev_output)
        if last_heading:
            level_name = {2: "章节", 3: "小节", 4: "子节", 5: "细节"}.get(heading_level, "标题")
            summary_parts.append(f"• 当前{level_name}：{last_heading}")

        # 2. 提取关键实体
        entities = _extract_key_entities(prev_output)
        if entities:
            summary_parts.append(f"• 已出现术语：{', '.join(entities[:5])}")

        # 3. 提取最后完整段落（用于衔接）
        if prev_output:
            # 找到最后一个完整段落
            paragraphs = [p.strip() for p in prev_output.split('\n\n') if p.strip()]
            if paragraphs:
                last_para = paragraphs[-1]
                # 取最后段落的最后几行
                lines = last_para.split('\n')
                tail_lines = lines[-3:] if len(lines) > 3 else lines
                tail_text = '\n'.join(tail_lines)
                if len(tail_text) > 300:
                    tail_text = tail_text[-300:]
                    # 找到句子边界
                    for sep in ['。', '！', '？', '；', '\n']:
                        idx = tail_text.find(sep)
                        if idx > 0:
                            tail_text = tail_text[idx + 1:]
                            break
                summary_parts.append(f"• 前文结尾：「{tail_text.strip()}」")

        # 4. 检测未完成的结构
        if prev_output:
            # 检查是否有未闭合的列表
            last_lines = prev_output.split('\n')[-10:]
            in_list = any(line.strip().startswith(('-', '*', '1.', '2.')) for line in last_lines)
            if in_list:
                summary_parts.append("• 注意：前文列表可能未完成，请继续保持列表格式")

            # 检查是否有未闭合的表格
            if '|' in '\n'.join(last_lines) and not last_lines[-1].strip().startswith('|'):
                pass  # 表格已闭合
            elif '|' in '\n'.join(last_lines):
                summary_parts.append("• 注意：前文表格可能未完成，请继续表格格式")

        return '\n'.join(summary_parts) if summary_parts else ""
    except Exception:
        return ""


def _chunk_guidance_text(i: int, n: int, content_type: str = "technical", prev_context: str = "",
                         compact: bool = False) -> str:
    """Enhanced chunk processing guidance with context awareness"""

    # Compact mode: minimal guidance to save tokens when budget is tight
    if compact:
        try:
            keywords = _extract_context_keywords(prev_context) if prev_context else ""
        except Exception:
            keywords = ""
        lines = [
            f"## 分片处理（第 {i}/{n}）",
            "- 仅基于本片段撰写；不复述【前文回顾】或【生成结尾】。",
            "- 术语前后一致；不要引入新主题。",
            "- 标题层级：## / ### / ####；配平代码围栏（```）。",
        ]
        if keywords:
            lines.append(f"- 前文关键词：{keywords}")
        return "\n".join(lines)

    # Negative prohibition list (inserted at front for maximum attention)
    prohibition_list = (
        "### ⚠️ 严禁行为\n"
        "❌ 不要改写/总结【前文回顾】【生成结尾】的段落\n"
        "❌ 不要替换/改写前文已出现的术语、定义、专有名词\n"
        "❌ 不要在代码续写时添加注释或改为伪代码\n"
        "❌ 不要用 # 做顶层标题，不要省略表格前后空行\n"
        f"❌ {'不要引入新主题或概念' if i < n else '不要重新总结全文'}\n\n"
    )

    # Base instructions for all chunks
    base_instructions = (
        f"## 分片处理说明：第 {i}/{n} 个片段\n\n"
        "### 核心处理原则\n"
        "- **专注当前内容**：仅基于本片段材料进行撰写\n"
        "- **上下文感知**：理解前文逻辑但不重复具体内容\n"
        "- **格式一致性**：严格遵守Markdown层级规范（## 顶层，### 子级）\n"
        "- **内容完整性**：确保本片段内容逻辑自洽\n\n"
    )

    # Content type specific guidance
    content_guidance = {
        "code": (
            "### 代码处理规范\n"
            "- **语法完整性**：不截断函数、类或代码块\n"
            "- **格式保持**：延续代码时确保缩进和语法正确\n"
            "- **注释原样**：保持注释内容，不添加解释\n"
        ),
        "textbook": (
            "### 教材内容规范\n"
            "- **编号连续性**：知识点编号保持逻辑顺序\n"
            "- **术语一致**：专业术语和定义前后统一\n"
            "- **示例完整**：案例和示例保持完整性\n"
        ),
        "technical": (
            "### 技术文档规范\n"
            "- **术语规范**：技术术语和缩略语保持一致\n"
            "- **步骤清晰**：操作步骤保持逻辑顺序\n"
            "- **表格规范**：表格格式符合GFM标准\n"
        ),
        "narrative": (
            "### 叙述性内容规范\n"
            "- **叙述连贯**：保持故事或论述的流畅性\n"
            "- **引用一致**：人物、事件引用保持统一\n"
            "- **逻辑清晰**：时间线和因果关系明确\n"
        )
    }

    # Position specific guidance
    if i == 1:
        position_guidance = (
            "### 起始片段注意事项\n"
            "- **适当引言**：可包含必要的背景介绍\n"
            "- **结构建立**：清晰定义章节框架\n"
            "- **避免冗长**：开头简洁，重点突出\n"
        )
    elif i == n:
        position_guidance = (
            "### 结束片段注意事项\n"
            "- **自然收尾**：提供适当的总结或结论\n"
            "- **内容完整**：确保主题论述完整\n"
            "- **避免新主题**：不引入新的主要概念\n"
        )
    else:
        position_guidance = (
            "### 中间片段注意事项\n"
            "- **平滑过渡**：与前文内容自然衔接\n"
            "- **主题延续**：保持核心主题的连续性\n"
            "- **逻辑连贯**：论述思路清晰连贯\n"
        )

    # Context awareness guidance
    context_guidance = ""
    if prev_context:
        keywords = _extract_context_keywords(prev_context)
        if keywords:
            context_guidance = (
                "### 上下文衔接指导\n"
                f"- **前文关键词**：{keywords}\n"
                "- **概念一致**：保持术语和定义的一致性\n"
                "- **逻辑延续**：延续前文的论述思路\n"
            )

    # Attention guidance
    attention_guidance = (
        "### 注意力分配指导\n"
        "- **重点关注**：本片段的核心内容和逻辑结构\n"
        "- **次要关注**：格式规范和术语一致性\n"
        "- **忽略内容**：重复的前文信息和过渡性语句\n"
    )

    # Output validation (3 mandatory checks)
    output_validation = (
        "### 输出前验证（3秒自检）\n"
        "1. 是否复述了【前文回顾】或【生成结尾】的段落？→ 立即删除\n"
        "2. 标题层级是否为 ## / ### / ####？代码围栏（```）是否配平？→ 修正\n"
        "3. 术语是否与前文保持一致？→ 对齐统一\n"
    )

    return (prohibition_list +
            base_instructions +
            content_guidance.get(content_type, content_guidance["technical"]) +
            position_guidance +
            context_guidance +
            attention_guidance +
            output_validation)


# -------------------------------
# Providers
# -------------------------------

def call_openai(model: str, messages: List[Dict[str, str]], api_key: str | None = None, base: str | None = None,
                max_tokens: int = 2000, temperature: float = 0.2) -> str:
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("openai package is required for provider=openai. Please `pip install openai`. ")

    # Avoid mutating process-wide env in concurrent runs; pass credentials directly
    client = OpenAI(
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
        base_url=base or os.getenv("OPENAI_BASE_URL"),
        timeout=600.0,  # 10 minutes timeout
    )
    api_start = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=temperature,
        max_tokens=max_tokens,
    )
    api_elapsed = time.time() - api_start
    with _PRINT_LOCK:
        print(f"[timer] API call completed in {api_elapsed:.2f}s")
    return resp.choices[0].message.content or ""


def _is_connection_or_ssl_error(e: Exception) -> bool:
    """True if the exception is a transient connection/SSL error worth retrying."""
    if e is None:
        return False
    err_str = str(e).lower()
    if "ssl" in err_str or "eof occurred" in err_str or "connection" in err_str or "max retries" in err_str:
        return True
    return isinstance(e,
                      (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def call_openai_compat(
        model: str,
        messages: List[Dict[str, str]],
        api_key: str,
        base: str,
        max_tokens: int = 2000,
        temperature: float = 0.2,
        timeout: int = 120,
        stream: bool = False,
        show_thinking: bool = False,
) -> str:
    api_start = time.time()
    url = base.rstrip('/') + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Charset": "utf-8",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True
        # Stream Server-Sent Events
        headers["Accept"] = "text/event-stream"
        conn_retries = 3
        conn_backoff = [4, 8, 16]
        for conn_attempt in range(conn_retries):
            try:
                r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout, stream=True)
                break
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                if conn_attempt < conn_retries - 1:
                    wait = conn_backoff[conn_attempt]
                    with _PRINT_LOCK:
                        print(
                            f"[api] 连接/SSL 暂时失败 (attempt {conn_attempt + 1}/{conn_retries}): {e}. {wait}秒后重试...")
                    time.sleep(wait)
                else:
                    raise
        try:
            # Log error body on non-2xx
            if r.status_code >= 400:
                try:
                    with _PRINT_LOCK:
                        print(f"[api:error] status={r.status_code} url={url}")
                        print(f"[api:error] body: {r.text[:4000]}")
                except Exception:
                    pass
            r.raise_for_status()
        except requests.HTTPError:
            # Re-raise after best-effort logging above
            raise
        # Force UTF-8 decoding for SSE
        try:
            r.encoding = "utf-8"
        except Exception:
            pass
        result: List[str] = []
        # Add read timeout control to prevent infinite blocking
        chunk_timeout = max(60, timeout * 2)  # Allow 2x connection timeout for full read
        start_reading = time.time()
        try:
            for raw_line in r.iter_lines(decode_unicode=True, chunk_size=512):
                # Check read timeout
                elapsed = time.time() - start_reading
                if elapsed > chunk_timeout:
                    with _PRINT_LOCK:
                        print(f"\n[api:timeout] 流式读取超过 {chunk_timeout}秒，强制终止")
                    break
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                # SSE payload lines usually begin with 'data:'
                if line.startswith('data:'):
                    data_str = line.split(':', 1)[1].strip()
                else:
                    data_str = line
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                try:
                    delta = (chunk.get("choices", [{}])[0] or {}).get("delta", {}) or {}
                    rc = delta.get("reasoning_content")
                    if rc and show_thinking:
                        # 批量缓冲输出，减少锁竞争（思维链可以缓冲更久）
                        _get_stream_buffer().append(rc)
                        _thread_local.char_count += len(rc)
                        _thread_local.is_thinking = True  # 标记为思维链内容
                        _flush_stream_buffer(force=False, is_thinking=True)
                    dc = delta.get("content")
                    if dc:
                        # 批量缓冲输出，减少锁竞争（正文需要保持一定实时性）
                        _get_stream_buffer().append(dc)
                        _thread_local.char_count += len(dc)
                        _thread_local.is_thinking = False  # 标记为正文内容
                        _flush_stream_buffer(force=False, is_thinking=False)
                        result.append(dc)
                except Exception:
                    # best-effort streaming; ignore malformed deltas
                    continue
        except Exception as stream_err:
            with _PRINT_LOCK:
                print(f"\n[api:stream_error] 流式读取异常：{stream_err}")
            # Only re-raise if no content was received at all
            if not result:
                raise
        finally:
            # 强制刷新剩余缓冲
            _flush_stream_buffer(force=True)
            # Ensure connection is always closed to release resources
            try:
                r.close()
            except Exception:
                pass
        api_elapsed = time.time() - api_start
        with _PRINT_LOCK:
            print(f"\n[计时] API调用（流式）耗时 {api_elapsed:.2f}秒")
        return "".join(result)
    else:
        headers["Accept"] = "application/json"
        conn_retries = 3
        conn_backoff = [4, 8, 16]
        for conn_attempt in range(conn_retries):
            try:
                r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=timeout)
                break
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                if conn_attempt < conn_retries - 1:
                    wait = conn_backoff[conn_attempt]
                    with _PRINT_LOCK:
                        print(
                            f"[api] 连接/SSL 暂时失败 (attempt {conn_attempt + 1}/{conn_retries}): {e}. {wait}秒后重试...")
                    time.sleep(wait)
                else:
                    raise
        try:
            if r.status_code >= 400:
                try:
                    with _PRINT_LOCK:
                        print(f"[api:error] status={r.status_code} url={url}")
                        print(f"[api:error] body: {r.text[:4000]}")
                except Exception:
                    pass
            r.raise_for_status()
        except requests.HTTPError:
            raise
        data = r.json()
        api_elapsed = time.time() - api_start
        with _PRINT_LOCK:
            print(f"[timer] API call completed in {api_elapsed:.2f}s")
        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            raise RuntimeError(f"Unexpected response: {data}")


def call_gemini(model: str, messages: List[Dict[str, str]], api_key: str, max_tokens: int = 2000,
                temperature: float = 0.2, stream: bool = False) -> str:
    try:
        from google import genai
        from google.genai import types
    except Exception as e:
        raise RuntimeError("google-genai package is required for provider=gemini. Please `pip install google-genai`. ")

    api_start = time.time()
    client = genai.Client(api_key=api_key)

    # Convert OpenAI-style messages to Gemini format
    contents = []
    for msg in messages:
        role = "user" if msg["role"] in ["user", "system"] else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg["content"])],
            )
        )

    generate_content_config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    # Stream and collect
    result = []
    # Add read timeout control
    chunk_timeout = 600  # 10 minutes for full streaming read
    start_reading = time.time()
    try:
        for chunk in client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=generate_content_config,
        ):
            # Check read timeout
            elapsed = time.time() - start_reading
            if elapsed > chunk_timeout:
                with _PRINT_LOCK:
                    print(f"\n[api:timeout] Gemini流式读取超过 {chunk_timeout}秒，强制终止")
                break
            if chunk.text:
                if stream:
                    # 批量缓冲输出，减少锁竞争
                    _get_stream_buffer().append(chunk.text)
                    _thread_local.char_count += len(chunk.text)
                    _flush_stream_buffer(force=False)
                result.append(chunk.text)
    except Exception as stream_err:
        with _PRINT_LOCK:
            print(f"\n[api:stream_error] Gemini流式读取异常：{stream_err}")
        # Only re-raise if no content was received
        if not result:
            raise
    finally:
        # 强制刷新剩余缓冲
        _flush_stream_buffer(force=True)

    api_elapsed = time.time() - api_start
    with _PRINT_LOCK:
        print(f"\n[计时] API调用（gemini流式）耗时 {api_elapsed:.2f}秒")
    return "".join(result)


def call_kimi(model: str, messages: List[Dict[str, str]], api_key: str, base_url: str = "https://api.moonshot.cn/v1",
              max_tokens: int = 2000, temperature: float = 0.2, stream: bool = False,
              show_thinking: bool = False) -> str:
    try:
        import openai
    except Exception as e:
        raise RuntimeError("openai package is required for provider=kimi. Please `pip install openai`. ")

    api_start = time.time()
    # Set timeout in client (default 600s for long-running thinking models)
    client = openai.Client(
        base_url=base_url,
        api_key=api_key,
        timeout=600.0,  # 10 minutes timeout for API calls
    )

    # Use streaming to handle thinking models
    stream_resp = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )

    result = []
    thinking = False
    # Add read timeout control
    chunk_timeout = 600  # 10 minutes for full streaming read
    start_reading = time.time()
    try:
        for chunk in stream_resp:
            # Check read timeout
            elapsed = time.time() - start_reading
            if elapsed > chunk_timeout:
                with _PRINT_LOCK:
                    print(f"\n[api:timeout] Kimi流式读取超过 {chunk_timeout}秒，强制终止")
                break
            if chunk.choices:
                choice = chunk.choices[0]
                # Handle reasoning content for thinking models
                if choice.delta and hasattr(choice.delta, "reasoning_content"):
                    if not thinking:
                        thinking = True
                    # Skip reasoning content in final output
                    if show_thinking:
                        # Echo reasoning tokens to console without persisting
                        try:
                            rc = getattr(choice.delta, "reasoning_content")
                            if rc:
                                # 批量缓冲输出，减少锁竞争（思维链可以缓冲更久）
                                _get_stream_buffer().append(rc)
                                _thread_local.char_count += len(rc)
                                _thread_local.is_thinking = True
                                _flush_stream_buffer(force=False, is_thinking=True)
                        except Exception:
                            pass
                    continue
                # Handle regular content
                if choice.delta and choice.delta.content:
                    if thinking:
                        thinking = False
                    if stream:
                        # 批量缓冲输出，减少锁竞争（正文需要保持一定实时性）
                        _get_stream_buffer().append(choice.delta.content)
                        _thread_local.char_count += len(choice.delta.content)
                        _thread_local.is_thinking = False
                        _flush_stream_buffer(force=False, is_thinking=False)
                    result.append(choice.delta.content)
    except Exception as stream_err:
        with _PRINT_LOCK:
            print(f"\n[api:stream_error] Kimi流式读取异常：{stream_err}")
        # Only re-raise if no content was received
        if not result:
            raise
    finally:
        # 强制刷新剩余缓冲
        _flush_stream_buffer(force=True)

    api_elapsed = time.time() - api_start
    with _PRINT_LOCK:
        print(f"\n[计时] API调用（kimi流式）耗时 {api_elapsed:.2f}秒")
    return "".join(result)


# -------------------------------
# CLI
# -------------------------------

def _ensure_utf8_console() -> None:
    """Best-effort console encoding alignment.
    - On Windows: only switch to UTF-8 if the console code page is 65001.
      Otherwise, keep current encoding to avoid mojibake (UTF-8 bytes on non-UTF-8 console).
    - On non-Windows: attempt to set UTF-8 if not already.
    """
    # Idempotent and thread-safe
    global _UTF8_READY
    global _UTF8_LOCK
    if '_UTF8_LOCK' not in globals():
        _UTF8_LOCK = threading.Lock()
    if '_UTF8_READY' not in globals():
        _UTF8_READY = False
    with _UTF8_LOCK:
        if _UTF8_READY:
            return
        try:
            import sys, io, os as _os
            is_windows = _os.name == "nt"
            cp = None
            if is_windows:
                try:
                    import ctypes
                    cp = ctypes.windll.kernel32.GetConsoleOutputCP()
                except Exception:
                    cp = None
            enc = getattr(sys.stdout, "encoding", None) or ""
            want_utf8 = (not is_windows) or (cp == 65001)
            if want_utf8 and "utf" not in enc.lower():
                try:
                    if hasattr(sys.stdout, 'reconfigure'):
                        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
                    if hasattr(sys.stderr, 'reconfigure'):
                        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
                except Exception:
                    # Fallback: wrap buffers
                    if hasattr(sys.stdout, "buffer"):
                        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
                    if hasattr(sys.stderr, "buffer"):
                        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
            else:
                try:
                    if hasattr(sys.stdout, 'reconfigure'):
                        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
                    if hasattr(sys.stderr, 'reconfigure'):
                        sys.stderr.reconfigure(errors="replace")  # type: ignore[attr-defined]
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            _UTF8_READY = True


def _collect_inputs(args: argparse.Namespace) -> List[str]:
    paths: List[str] = []
    # explicit inputs
    if args.inputs:
        paths.extend(args.inputs)
    # glob patterns
    if getattr(args, "inputs_glob", None):
        for pattern in args.inputs_glob:
            for p in glob.glob(pattern, recursive=True):
                if os.path.isfile(p):
                    paths.append(p)
    # input dir + exts
    if getattr(args, "input_dir", None):
        want_exts = [e if e.startswith('.') else f'.{e}' for e in (args.exts or [])]
        want_exts = [e.lower() for e in want_exts]
        for root, _, files in os.walk(args.input_dir):
            for fn in files:
                if not want_exts or os.path.splitext(fn)[1].lower() in want_exts:
                    paths.append(os.path.join(root, fn))
    # inputs from a list file
    if getattr(args, "inputs_file", None):
        with open(args.inputs_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                p = line.strip()
                if p:
                    paths.append(p)
    # dedupe preserve order
    seen = set()
    out = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def run_agent(args: argparse.Namespace) -> None:
    # 与 CLI 的 main() 不同，chapter_pipeline 等会直接 import 并调用本函数，须在此加载 .env
    _ensure_dotenv_loaded()
    agent_start = time.time()
    # Ensure console can display UTF-8 while streaming
    _ensure_utf8_console()
    # system prompts: agent prompt + optional knowledge prompt (e.g., V5/V6)
    system_files = args.system_prompts or []
    # input files: textbook markdown/pdf text OR the four agent outputs for audit
    input_files = _collect_inputs(args)
    if not input_files:
        raise RuntimeError("No input files found. Use --inputs / --inputs-glob / --input-dir or --inputs-file.")

    with _PRINT_LOCK:
        print(f"[timer] Loading {len(input_files)} input file(s)...")
    load_start = time.time()
    messages = build_messages(system_files=system_files, input_files=input_files, extra_instructions=args.extra)
    load_elapsed = time.time() - load_start
    with _PRINT_LOCK:
        print(f"[timer] Input loading completed in {load_elapsed:.2f}s")

    provider = args.provider
    agent_name = (args.agent or "").lower()
    model = args.model
    # Infer default model names if not explicitly provided by the user
    if not model:
        if provider == "kimi":
            model = "kimi-k2-thinking"
        elif "doubao" in agent_name:
            model = "doubao-seed-2-1-turbo-260628"
        elif "deepseek" in agent_name or "reasoner" in agent_name:
            model = "deepseek-v4-pro"
        elif provider == "openai_compat":
            # Fallback when agent name doesn't hint at a model:
            # try to infer from base URL, otherwise raise early with a clear message
            base_lower = (args.base or "").lower()
            if "deepseek" in base_lower:
                model = "deepseek-v4-pro"
            elif "ark" in base_lower or "volces" in base_lower:
                model = "doubao-seed-2-1-turbo-260628"
            else:
                raise RuntimeError(
                    f"Cannot infer model for agent='{args.agent}' provider='{provider}'. "
                    "Please pass --model explicitly.")
    max_tokens = args.max_tokens
    temperature = args.temperature

    # Prepare AutoChunk stats path and reset historical state for this run.
    # 说明：每次 run_agent 调用都会代表“本次运行”的事实状态。
    # 若之前某次运行触发过 AutoChunk 而本次没有触发，我们需要避免旧的 chunk_stats.json
    # 继续在汇总中被当成“当前运行触发”的记录。
    chunk_stats_path: str | None = None
    try:
        out_path = getattr(args, "output", None)
        if out_path:
            chunk_stats_path = out_path.rsplit(".", 1)[0] + ".chunk_stats.json"
            if os.path.exists(chunk_stats_path):
                # 将历史记录重置为 auto_chunk_enabled=False，本次若真正触发 AutoChunk
                # 会在后续逻辑中覆盖写入新的统计信息。
                import json as _json  # 局部别名，避免与上方导入混淆
                try:
                    safe_write_text(
                        chunk_stats_path,
                        _json.dumps(
                            {
                                "agent": args.agent,
                                "auto_chunk_enabled": False,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                except Exception:
                    # 若重置失败不影响主流程，后续如触发 AutoChunk 仍会覆盖写入
                    pass
    except Exception:
        chunk_stats_path = None

    # Pre-trim or chunk messages to respect provider/model context window (DeepSeek focus)
    context_limit = _context_limit_for_model(provider, getattr(args, "base", None), model, agent_name)
    if context_limit:
        # Leave safety buffer of 1024 tokens
        est_tokens = _estimate_tokens_messages(messages)
        prompt_budget = _dynamic_prompt_budget(context_limit, int(max_tokens or 0), est_tokens)
        if est_tokens > prompt_budget:
            if bool(getattr(args, "auto_chunk", False)):
                # 监控统计初始化（每次AutoChunk创建独立dict，线程安全）
                chunk_stats = {
                    "original_tokens": est_tokens,
                    "budget": prompt_budget,
                    "context_limit": context_limit,
                    "utilization_ratio": 0.0,
                    "num_chunks": 0,
                    "quality_score": 0.0,
                    "retune_passes": 0,
                    "overflow_shrink": 0,
                    "compact_mode": 0,
                    "truncate_triggered": 0,
                    "chunk_details": [],  # 每个chunk的详细信息
                }
                chunk_stats["utilization_ratio"] = round((prompt_budget / context_limit) * 100, 1)
                if os.getenv("AUTOCHUNK_VERBOSE_CONSOLE") == "1":
                    with _PRINT_LOCK:
                        print(
                            f"[guard] Estimated prompt tokens ~{est_tokens} exceed budget {prompt_budget} (context {context_limit}, max_tokens {max_tokens}). Auto-chunk enabled → splitting prompt...")
                chunks = _split_user_into_chunk_messages(messages, prompt_budget)
                chunk_stats["num_chunks"] = len(chunks)
                # Evaluate chunk quality and retune if needed (up to two passes)
                try:
                    qs = _evaluate_chunk_quality(chunks)
                    avg_overall = sum(v.get('overall', 0.0) for v in qs.values()) / max(1, len(qs))
                    chunk_stats["quality_score"] = round(avg_overall, 3)
                    if avg_overall < 0.6:  # 降低质量阈值，避免过度调优
                        # PASS 1: Increase semantic window and tighten fallback to improve boundaries
                        chunk_stats["retune_passes"] = 1
                        tuned_window = 1600 if avg_overall >= 0.5 else 2000
                        tuned_fallback = 768 if avg_overall >= 0.5 else 512
                        chunks = _split_user_into_chunk_messages(messages, prompt_budget, semantic_window=tuned_window,
                                                                 fallback_step=tuned_fallback)
                        chunk_stats["num_chunks"] = len(chunks)
                        # PASS 2 (optional): re-evaluate and, if still low, adjust guidance + parameters once
                        try:
                            qs2 = _evaluate_chunk_quality(chunks)
                            avg2 = sum(v.get('overall', 0.0) for v in qs2.values()) / max(1, len(qs2))
                            chunk_stats["quality_score"] = round(avg2, 3)
                            if avg2 < 0.6:  # 降低第二次质量阈值
                                chunk_stats["retune_passes"] = 2
                                total_chunks = len(chunks)
                                dynamic_guidance = 768 if total_chunks <= 1 else min(
                                    1024 + max(0, total_chunks - 2) * 128, 2048)
                                tuned_window2 = min(tuned_window + 400, 2400)
                                tuned_fallback2 = max(384, tuned_fallback // 2)
                                chunks = _split_user_into_chunk_messages(
                                    messages,
                                    prompt_budget,
                                    guidance_margin=dynamic_guidance,
                                    semantic_window=tuned_window2,
                                    fallback_step=tuned_fallback2,
                                )
                                chunk_stats["num_chunks"] = len(chunks)
                        except Exception:
                            pass
                except Exception:
                    pass
                results: List[str] = []
                total_chunks = len(chunks)
                prev_tail: str = ""
                prev_input_tail: str = ""
                # Adaptive initial overlap based on full content type
                try:
                    base_user = next(m.get("content", "") for m in messages if m.get("role") == "user")
                    content_type = _detect_content_type(base_user)
                except Exception:
                    content_type = "technical"
                tail_chars, tail_chars_input = _adaptive_overlap_strategy(content_type, 5000)
                for idx, cm in enumerate(chunks, start=1):
                    # 初始化chunk详情（每个chunk独立dict）
                    chunk_detail = {
                        "chunk_id": idx,
                        "tokens": 0,
                        "has_overflow": False,
                        "has_compact": False,
                        "has_truncate": False,
                    }

                    # Inject per-chunk guidance BEFORE the user message; merge previous chunk tail into the user content prefix
                    cm2 = [m.copy() for m in cm]
                    # locate user index
                    try:
                        user_msg_idx = next(i for i, m in enumerate(cm2) if m.get("role") == "user")
                    except StopIteration:
                        user_msg_idx = len(cm2)
                        cm2.append({"role": "user", "content": ""})
                    # 1) guidance as system before user
                    try:
                        prev_context = ((prev_input_tail or "") + "\n" + (prev_tail or "")).strip()
                    except Exception:
                        prev_context = ""
                    guidance_text = _chunk_guidance_text(idx, total_chunks, content_type=content_type,
                                                         prev_context=prev_context)
                    cm2.insert(user_msg_idx, {"role": "system", "content": guidance_text})
                    # after insertion, user message index shifts by +1
                    user_msg_idx += 1
                    original_user = cm2[user_msg_idx].get("content", "")
                    # Adaptive overlap per chunk based on content type and size
                    try:
                        chunk_size = len(original_user)
                        tail_chars, tail_chars_input = _adaptive_overlap_strategy(content_type, chunk_size)
                    except Exception:
                        pass
                    # 2) context overlap merged into user content prefix (both source tail and output tail)
                    has_any_context = bool(prev_input_tail or prev_tail)
                    if has_any_context:
                        # 使用语义边界截取，而不是简单字符截取
                        src_tail = ""
                        out_tail = ""

                        try:
                            if prev_input_tail:
                                # 找到语义边界
                                boundary = _find_semantic_boundary(prev_input_tail,
                                                                   len(prev_input_tail) - tail_chars_input)
                                src_tail = prev_input_tail[boundary:]
                                # 确保从行首开始
                                nl = src_tail.find("\n")
                                if nl != -1 and nl < 50:
                                    src_tail = src_tail[nl + 1:]
                        except Exception:
                            src_tail = prev_input_tail[-tail_chars_input:] if prev_input_tail else ""

                        try:
                            if prev_tail:
                                boundary = _find_semantic_boundary(prev_tail, len(prev_tail) - tail_chars)
                                out_tail = prev_tail[boundary:]
                                nl2 = out_tail.find("\n")
                                if nl2 != -1 and nl2 < 50:
                                    out_tail = out_tail[nl2 + 1:]
                        except Exception:
                            out_tail = prev_tail[-tail_chars:] if prev_tail else ""

                        # 构建结构化上下文摘要
                        context_summary = ""
                        try:
                            context_summary = _build_context_summary(prev_tail, prev_input_tail)
                        except Exception:
                            pass

                        prefix_parts = []

                        # 1. 结构化上下文摘要（新增）
                        if context_summary:
                            prefix_parts.append(
                                "【上下文状态摘要】\n"
                                f"{context_summary}\n"
                                "→ 请保持术语一致性，延续当前结构。\n\n"
                            )

                        # 2. 源稿参考（优化后的语义边界截取）
                        if src_tail:
                            prefix_parts.append(
                                "【前文回顾（源稿片段）】\n"
                                f"以下为上一片段源文末尾的参考内容（仅供理解上下文，严禁复述）：\n```markdown\n{src_tail.strip()}\n```\n"
                                "⚠️ 严禁对上述内容做任何形式的复述、改写或总结。\n\n"
                            )

                        # 3. 生成结尾参考（优化后的语义边界截取）
                        if out_tail:
                            prefix_parts.append(
                                "【衔接参考（上一片段生成结尾）】\n"
                                f"以下为上一片段生成结尾（用于衔接，严禁复述）：\n```text\n{out_tail.strip()}\n```\n"
                                "⚠️ 请从此处自然衔接，不要重复上述内容。\n\n"
                            )

                        prefix_parts.append("【当前片段输入开始】\n")
                        cm2[user_msg_idx]["content"] = "\n".join(prefix_parts) + original_user
                        # Budget-aware shrinking of embedded context (shrink output tail first, then input tail)
                        try:
                            est_now = _estimate_tokens_messages(cm2)
                            if est_now > prompt_budget:
                                chunk_stats["overflow_shrink"] += 1
                                chunk_detail["has_overflow"] = True
                                # Iteratively half overlap sizes with semantic boundary
                                shrink_out = len(out_tail) if out_tail else 0
                                shrink_in = len(src_tail) if src_tail else 0
                                while est_now > prompt_budget and (shrink_out > 200 or shrink_in > 200):
                                    if shrink_out > 200:
                                        shrink_out = shrink_out * 2 // 3  # 缩减1/3
                                    elif shrink_in > 200:
                                        shrink_in = shrink_in * 2 // 3
                                    # 使用语义边界截取
                                    new_out = ""
                                    new_in = ""
                                    if shrink_out > 0 and prev_tail:
                                        try:
                                            boundary = _find_semantic_boundary(prev_tail, len(prev_tail) - shrink_out)
                                            new_out = prev_tail[boundary:].strip()
                                        except Exception:
                                            new_out = prev_tail[-shrink_out:] if shrink_out else ""
                                    if shrink_in > 0 and prev_input_tail:
                                        try:
                                            boundary = _find_semantic_boundary(prev_input_tail,
                                                                               len(prev_input_tail) - shrink_in)
                                            new_in = prev_input_tail[boundary:].strip()
                                        except Exception:
                                            new_in = prev_input_tail[-shrink_in:] if shrink_in else ""

                                    parts2 = []
                                    # 保留简化的上下文摘要
                                    if context_summary:
                                        parts2.append(f"【上下文摘要】{context_summary[:200]}...\n\n")
                                    if new_in:
                                        parts2.append(
                                            "【前文参考（简化）】\n"
                                            f"```markdown\n{new_in}\n```\n"
                                            "⚠️ 严禁复述。\n\n"
                                        )
                                    if new_out:
                                        parts2.append(
                                            "【衔接参考（简化）】\n"
                                            f"```text\n{new_out}\n```\n"
                                            "⚠️ 请自然衔接。\n\n"
                                        )
                                    parts2.append("【当前片段输入】\n")
                                    cm2[user_msg_idx]["content"] = "\n".join(parts2) + original_user
                                    est_now = _estimate_tokens_messages(cm2)
                                if est_now > prompt_budget:
                                    # Drop overlaps entirely but keep minimal context hint
                                    try:
                                        entities = _extract_key_entities(prev_tail)[:3]
                                        hint = f"【上下文提示】延续术语：{', '.join(entities)}\n\n" if entities else ""
                                        cm2[user_msg_idx]["content"] = hint + original_user
                                    except Exception:
                                        cm2[user_msg_idx]["content"] = original_user
                        except Exception:
                            cm2[user_msg_idx]["content"] = original_user
                    # Guidance compaction: if still over budget, try compact guidance, then drop if needed
                    try:
                        cur_est = _estimate_tokens_messages(cm2)
                        if cur_est > prompt_budget:
                            chunk_stats["compact_mode"] += 1
                            chunk_detail["has_compact"] = True
                            gidx = user_msg_idx - 1
                            compact_txt = _chunk_guidance_text(idx, total_chunks, content_type=content_type,
                                                               prev_context=prev_context, compact=True)
                            if 0 <= gidx < len(cm2) and cm2[gidx].get("role") == "system":
                                cm2[gidx]["content"] = compact_txt
                                cur_est = _estimate_tokens_messages(cm2)
                            if cur_est > prompt_budget and 0 <= gidx < len(cm2) and cm2[gidx].get("role") == "system":
                                # As a last resort, remove guidance
                                cm2.pop(gidx)
                                user_msg_idx -= 1
                    except Exception:
                        pass
                    # Finally, if still too large, truncate user content
                    try:
                        if _estimate_tokens_messages(cm2) > prompt_budget:
                            chunk_stats["truncate_triggered"] += 1
                            chunk_detail["has_truncate"] = True
                            _truncate_user_message_to_budget(cm2, prompt_budget, _PRINT_LOCK)
                    except Exception:
                        pass

                    # 更新chunk详情并记录
                    chunk_detail["tokens"] = _estimate_tokens_messages(cm2)
                    chunk_stats["chunk_details"].append(chunk_detail)

                    # Log token estimate per chunk
                    try:
                        if os.getenv("AUTOCHUNK_VERBOSE_CONSOLE") == "1":
                            with _PRINT_LOCK:
                                print(f"[chunk] {idx}/{total_chunks} ~{_estimate_tokens_messages(cm2)} tokens")
                                print(f"[chunk] Running {idx}/{total_chunks} ...")
                    except Exception:
                        pass

                    # Per-chunk retry with exponential backoff
                    part = ""
                    attempts = 3
                    last_err: Exception | None = None
                    for attempt in range(1, attempts + 1):
                        try:
                            if provider == "openai":
                                part = call_openai(model=model, messages=cm2,
                                                   api_key=args.api_key or os.getenv("OPENAI_API_KEY"),
                                                   base=args.base or os.getenv("OPENAI_BASE_URL"),
                                                   max_tokens=max_tokens, temperature=temperature)
                            elif provider == "openai_compat":
                                key = args.api_key or os.getenv(args.key_env or "OPENAI_API_KEY")
                                base = args.base or os.getenv(args.base_env or "OPENAI_BASE_URL")
                                if not key or not base:
                                    raise RuntimeError(
                                        "For provider=openai_compat, please provide --api-key and --base (or set KEY/BASE envs).")
                                part = call_openai_compat(model=model, messages=cm2, api_key=key, base=base,
                                                          max_tokens=max_tokens, temperature=temperature,
                                                          timeout=int(getattr(args, "timeout", 120)),
                                                          stream=bool(getattr(args, "stream", False)),
                                                          show_thinking=bool(getattr(args, "show_thinking", False)))
                            elif provider == "gemini":
                                key = args.api_key
                                if not key:
                                    raise RuntimeError(
                                        "For provider=gemini, please provide --api-key (or set GEMINI_API_KEY env).")
                                part = call_gemini(model=model, messages=cm2, api_key=key, max_tokens=max_tokens,
                                                   temperature=temperature, stream=bool(getattr(args, "stream", False)))
                            elif provider == "kimi":
                                key = args.api_key or os.getenv("MOONSHOT_API_KEY")
                                if not key:
                                    raise RuntimeError(
                                        "For provider=kimi, please provide --api-key (or set MOONSHOT_API_KEY env).")
                                base = args.base or "https://api.moonshot.cn/v1"
                                part = call_kimi(model=model, messages=cm2, api_key=key, base_url=base,
                                                 max_tokens=max_tokens, temperature=temperature,
                                                 stream=bool(getattr(args, "stream", False)),
                                                 show_thinking=bool(getattr(args, "show_thinking", False)))
                            else:
                                raise RuntimeError(
                                    "Unsupported provider. Use 'openai', 'openai_compat', 'gemini', or 'kimi'.")
                            # success
                            break
                        except Exception as e:
                            last_err = e
                            if attempt < attempts:
                                with _PRINT_LOCK:
                                    print(f"[chunk] Error on {idx} attempt {attempt}/{attempts}: {e}. Retrying...")
                                # 对连接/SSL 类错误使用更长退避，便于网络恢复
                                backoff = 8 * attempt if _is_connection_or_ssl_error(e) else 2 * attempt
                                time.sleep(backoff)
                            else:
                                raise RuntimeError(f"Failed to process chunk {idx} after {attempts} attempts: {e}")

                    results.append(part)
                    # update prev_tail for next chunk context with newline alignment
                    try:
                        if part:
                            raw_tail = part[-(tail_chars * 2):]
                            first_nl = raw_tail.find("\n")
                            if first_nl != -1 and (len(raw_tail) - first_nl) > 10:
                                prev_tail = raw_tail[first_nl + 1:]
                            else:
                                prev_tail = raw_tail
                        else:
                            prev_tail = ""
                    except Exception:
                        prev_tail = ""
                    # update prev_input_tail for next chunk based on current user content
                    try:
                        if original_user:
                            raw_in_tail = original_user[-(tail_chars_input * 2):]
                            fi = raw_in_tail.find("\n")
                            if fi != -1 and (len(raw_in_tail) - fi) > 10:
                                prev_input_tail = raw_in_tail[fi + 1:]
                            else:
                                prev_input_tail = raw_in_tail
                        else:
                            prev_input_tail = ""
                    except Exception:
                        prev_input_tail = ""
                merged_content = "\n\n".join(results)
                safe_write_text(args.output, merged_content)

                # 写入chunk统计信息到同级目录（文件写入已通过safe_write_text原子化）
                try:
                    chunk_stats_path = args.output.rsplit('.', 1)[0] + '.chunk_stats.json'
                    import json
                    # 计算健康度
                    health = 100
                    if chunk_stats['overflow_shrink'] > 0:
                        health -= chunk_stats['overflow_shrink'] * 10
                    if chunk_stats['compact_mode'] > 0:
                        health -= chunk_stats['compact_mode'] * 15
                    if chunk_stats['truncate_triggered'] > 0:
                        health -= chunk_stats['truncate_triggered'] * 25
                    if chunk_stats['quality_score'] < 0.6:  # 降低最终质量阈值
                        health -= 20
                    health = max(0, health)

                    # 创建新的dict保存，避免修改原始chunk_stats
                    # 包含完整字段以便后续验证
                    stats_to_save = {
                        "agent": args.agent,
                        "auto_chunk_enabled": True,
                        "original_tokens": chunk_stats['original_tokens'],
                        "num_chunks": chunk_stats['num_chunks'],
                        "quality_score": chunk_stats['quality_score'],
                        "health_score": health,
                        "has_issues": chunk_stats['overflow_shrink'] > 0 or chunk_stats['compact_mode'] > 0 or
                                      chunk_stats['truncate_triggered'] > 0,
                        "overflow_shrink": chunk_stats['overflow_shrink'],
                        "compact_mode": chunk_stats['compact_mode'],
                        "truncate_triggered": chunk_stats['truncate_triggered'],
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }

                    # 原子写入并验证
                    json_content = json.dumps(stats_to_save, ensure_ascii=False, indent=2)
                    safe_write_text(chunk_stats_path, json_content)

                    # 写入后验证（可选，确保数据完整性）
                    if os.getenv("AUTOCHUNK_VERIFY_WRITE") == "1":
                        try:
                            verify_content = safe_read_text(chunk_stats_path)
                            verify_data = json.loads(verify_content)
                            if verify_data.get("health_score") != health:
                                with _PRINT_LOCK:
                                    print(f"[autochunk:warn] 写入验证失败: {chunk_stats_path}")
                        except Exception:
                            pass
                except Exception as e:
                    # 记录错误但不中断流程
                    if os.getenv("AUTOCHUNK_VERBOSE_CONSOLE") == "1":
                        with _PRINT_LOCK:
                            print(f"[autochunk:error] 写入chunk_stats失败: {e}")

                agent_elapsed = time.time() - agent_start

                # 输出监控统计（_PRINT_LOCK确保输出不交错）
                with _PRINT_LOCK:
                    print(f"[agent:{args.agent}] 已写入: {args.output} ({len(merged_content)} 字符)")
                    print(f"[计时] Agent总执行时间: {agent_elapsed:.2f}秒")
                if os.getenv("AUTOCHUNK_VERBOSE_CONSOLE") == "1":
                    with _PRINT_LOCK:
                        print("\n" + "=" * 70)
                        print("AutoChunk 性能监控".center(70))
                        print("=" * 70)

                        # 基础指标
                        print(f"原始输入Token数   : {chunk_stats['original_tokens']:>8,} tokens")
                        print(
                            f"预算上限          : {chunk_stats['budget']:>8,} tokens (利用率 {chunk_stats['utilization_ratio']:>5}%)")
                        print(f"上下文窗口        : {chunk_stats['context_limit']:>8,} tokens")
                        print(f"切分片段数        : {chunk_stats['num_chunks']:>8} 个")

                        # 质量指标
                        quality_status = "通过" if chunk_stats['quality_score'] >= 0.6 else "警告"
                        print(f"质量评分          : {chunk_stats['quality_score']:>8.3f} [{quality_status}]")
                        print(f"重调次数          : {chunk_stats['retune_passes']:>8} 次")

                        # 详细chunk信息
                        if chunk_stats['chunk_details']:
                            print("\n" + "-" * 70)
                            print("片段详情".center(70))
                            print("-" * 70)
                            for detail in chunk_stats['chunk_details']:
                                chunk_id = detail['chunk_id']
                                tokens = detail['tokens']
                                util = round((tokens / chunk_stats['budget']) * 100, 1)
                                flags = []
                                if detail['has_overflow']:
                                    flags.append('溢出收缩')
                                if detail['has_compact']:
                                    flags.append('压缩模式')
                                if detail['has_truncate']:
                                    flags.append('内容截断')
                                flag_str = f" [{', '.join(flags)}]" if flags else ""
                                print(
                                    f"  片段 {chunk_id:2d}/{chunk_stats['num_chunks']:2d}  : {tokens:>8,} tokens (预算占用 {util:>5.1f}%){flag_str}")

                        # 风险指标（仅在有问题时显示）
                        has_issues = False
                        if chunk_stats['overflow_shrink'] > 0:
                            if not has_issues:
                                print("\n" + "-" * 70)
                                print("警告信息".center(70))
                                print("-" * 70)
                                has_issues = True
                            print(
                                f"  [警告] 溢出收缩       : {chunk_stats['overflow_shrink']:>3} 次 (上下文重叠被缩减)")
                        if chunk_stats['compact_mode'] > 0:
                            if not has_issues:
                                print("\n" + "-" * 70)
                                print("警告信息".center(70))
                                print("-" * 70)
                                has_issues = True
                            print(f"  [警告] 压缩模式       : {chunk_stats['compact_mode']:>3} 次 (引导信息被压缩)")
                        if chunk_stats['truncate_triggered'] > 0:
                            if not has_issues:
                                print("\n" + "-" * 70)
                                print("警告信息".center(70))
                                print("-" * 70)
                                has_issues = True
                            print(
                                f"  [严重] 内容截断       : {chunk_stats['truncate_triggered']:>3} 次 (内容被截断 - 质量损失!)")

                        # 健康度评估
                        health_score = 100
                        if chunk_stats['overflow_shrink'] > 0:
                            health_score -= chunk_stats['overflow_shrink'] * 10
                        if chunk_stats['compact_mode'] > 0:
                            health_score -= chunk_stats['compact_mode'] * 15
                        if chunk_stats['truncate_triggered'] > 0:
                            health_score -= chunk_stats['truncate_triggered'] * 25
                        if chunk_stats['quality_score'] < 0.6:  # 降低最终质量阈值
                            health_score -= 20
                        health_score = max(0, health_score)

                        print("\n" + "=" * 70)
                        if health_score >= 80:
                            status_text = "优秀"
                        elif health_score >= 60:
                            status_text = "良好"
                        else:
                            status_text = "需要改进"
                        print(f"系统健康度        : {health_score:>3}/100 [{status_text}]")

                        if health_score < 80:
                            print("\n建议: 考虑减小章节大小或增加上下文窗口")
                        print("=" * 70 + "\n")
                return
            else:
                with _PRINT_LOCK:
                    print(
                        f"[guard] Estimated prompt tokens ~{est_tokens} exceed budget {prompt_budget} (context {context_limit}, max_tokens {max_tokens}). Truncating user content...")
                _truncate_user_message_to_budget(messages, prompt_budget, _PRINT_LOCK)

    if provider == "openai":
        content = call_openai(model=model, messages=messages, api_key=args.api_key or os.getenv("OPENAI_API_KEY"),
                              base=args.base or os.getenv("OPENAI_BASE_URL"), max_tokens=max_tokens,
                              temperature=temperature)
    elif provider == "openai_compat":
        key = args.api_key or os.getenv(args.key_env or "OPENAI_API_KEY")
        base = args.base or os.getenv(args.base_env or "OPENAI_BASE_URL")
        if not key or not base:
            raise RuntimeError(
                "For provider=openai_compat, please provide --api-key and --base (or set KEY/BASE envs).")
        content = call_openai_compat(model=model, messages=messages, api_key=key, base=base, max_tokens=max_tokens,
                                     temperature=temperature, timeout=int(getattr(args, "timeout", 120)),
                                     stream=bool(getattr(args, "stream", False)),
                                     show_thinking=bool(getattr(args, "show_thinking", False)))
    elif provider == "gemini":
        key = args.api_key
        if not key:
            raise RuntimeError("For provider=gemini, please provide --api-key (or set GEMINI_API_KEY env).")
        content = call_gemini(model=model, messages=messages, api_key=key, max_tokens=max_tokens,
                              temperature=temperature, stream=bool(getattr(args, "stream", False)))
    elif provider == "kimi":
        key = args.api_key or os.getenv("MOONSHOT_API_KEY")
        if not key:
            raise RuntimeError("For provider=kimi, please provide --api-key (or set MOONSHOT_API_KEY env).")
        base = args.base or "https://api.moonshot.cn/v1"
        content = call_kimi(model=model, messages=messages, api_key=key, base_url=base, max_tokens=max_tokens,
                            temperature=temperature, stream=bool(getattr(args, "stream", False)),
                            show_thinking=bool(getattr(args, "show_thinking", False)))
    else:
        raise RuntimeError("Unsupported provider. Use 'openai', 'openai_compat', 'gemini', or 'kimi'.")

    safe_write_text(args.output, content)
    agent_elapsed = time.time() - agent_start
    with _PRINT_LOCK:
        print(f"[agent:{args.agent}] wrote: {args.output} ({len(content)} chars)")
        print(f"[timer] ⏱️  Total agent execution time: {agent_elapsed:.2f}s")


def _namespace_from_job(job: Dict[str, Any]) -> argparse.Namespace:
    """Build an argparse-like Namespace for a single job dict.
    Supported job keys: id, agent, provider, model, system_prompts (list), inputs (list),
    output, max_tokens, temperature, api_key, base, key_env, base_env, extra.
    """
    return argparse.Namespace(
        agent=job.get("agent"),
        provider=job.get("provider", "openai"),
        model=job.get("model"),  # allow run_agent to infer defaults if None
        system_prompts=job.get("system_prompts", []),
        inputs=job.get("inputs", []),
        inputs_glob=job.get("inputs_glob", []),
        input_dir=job.get("input_dir"),
        exts=job.get("exts", [".md", ".txt"]),
        inputs_file=job.get("inputs_file"),
        output=job.get("output"),
        max_tokens=int(job.get("max_tokens", 2000)),
        temperature=float(job.get("temperature", 0.2)),
        api_key=job.get("api_key"),
        base=job.get("base"),
        key_env=job.get("key_env"),
        base_env=job.get("base_env"),
        timeout=int(job.get("timeout", 120)),
        stream=bool(job.get("stream", False)),
        show_thinking=bool(job.get("show_thinking", False)),
        auto_chunk=bool(job.get("auto_chunk", False)),
        extra=job.get("extra"),
        func=run_agent,
    )


def _run_single_job(job: Dict[str, Any]) -> str:
    job_start = time.time()
    job_id = job.get("id", "unknown")
    with _PRINT_LOCK:
        print(f"\n[batch] Starting job: {job_id}")

    # Retry configuration (optional)
    retries = int(job.get("retries", 0) or 0)
    backoff = int(job.get("retry_backoff", 10) or 10)

    attempt = 0
    last_err: Exception | None = None
    while attempt <= retries:
        ns = _namespace_from_job(job)
        # Parameter adjustments on retries to improve robustness
        if attempt == 1:
            try:
                ns.max_tokens = max(800, int(getattr(ns, "max_tokens", 2000) * 0.75))
            except Exception:
                pass
            try:
                ns.timeout = int(getattr(ns, "timeout", 120) * 1.5)
            except Exception:
                pass
            try:
                ns.stream = False
            except Exception:
                pass
        elif attempt >= 2:
            try:
                ns.max_tokens = max(600, int(getattr(ns, "max_tokens", 2000) * 0.6))
            except Exception:
                pass
            try:
                ns.timeout = int(getattr(ns, "timeout", 120) * 2)
            except Exception:
                pass
            try:
                ns.stream = False
            except Exception:
                pass
            try:
                # Keep temperature conservative on later retries
                ns.temperature = min(float(getattr(ns, "temperature", 0.2)), 0.2)
            except Exception:
                pass

        try:
            run_agent(ns)
            job_elapsed = time.time() - job_start
            with _PRINT_LOCK:
                print(
                    f"[timer] ⏱️  Job '{job_id}' completed in {job_elapsed:.2f}s (attempt {attempt + 1}/{retries + 1})")
            return job.get("id", "")
        except Exception as e:
            last_err = e
            attempt += 1
            with _PRINT_LOCK:
                print(f"[batch] job error: {job_id} attempt={attempt}/{retries + 1}: {e}")
            if attempt <= retries:
                sleep_secs = backoff * (2 ** (attempt - 1))
                # Cap individual sleep to avoid excessively long waits
                sleep_secs = min(sleep_secs, 60)
                with _PRINT_LOCK:
                    print(f"[batch] retrying in {sleep_secs}s ...")
                try:
                    time.sleep(sleep_secs)
                except Exception:
                    pass
            else:
                with _PRINT_LOCK:
                    print(f"[batch] job failed after {retries + 1} attempts: {job_id}")
                raise
    # This line should never be reached due to raise above, but add for type checker
    raise RuntimeError(f"Job {job_id} failed after all retries")


def run_batch(args: argparse.Namespace) -> None:
    batch_start = time.time()
    # Ensure console can display UTF-8 while streaming across jobs
    _ensure_utf8_console()
    """Run multiple jobs concurrently with optional dependencies.
    JSON format (list of jobs):
    [
      {
        "id": "kimi_src",
        "agent": "kimi_from_source",
        "provider": "kimi",
        "system_prompts": ["multi_agent/prompts/kimi_agent.md", "md/知识点总结提示词V5.1.md"],
        "inputs": ["exports/multi_agent/inputs/source.md"],
        "output": "exports/multi_agent/inputs/kimi_from_source.md",
        "max_tokens": 16000,
        "temperature": 0.1
      },
      {"id": "merge_kimi", "agent": "kimi_merge", "provider": "kimi", "deps": ["kimi_src", "deepseek_src", "doubao_src"], ...}
    ]
    """
    jobs_path = args.jobs_file
    max_workers = int(args.max_workers or 3)
    try:
        with open(jobs_path, "r", encoding="utf-8", errors="ignore") as f:
            jobs: List[Dict[str, Any]] = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to read jobs file: {jobs_path}: {e}")

    # Ensure IDs and build maps
    id_to_job: Dict[str, Dict[str, Any]] = {}
    for idx, job in enumerate(jobs):
        if "id" not in job or not job.get("id"):
            job["id"] = f"job_{idx + 1}"
        jid = job["id"]
        if jid in id_to_job:
            raise RuntimeError(f"Duplicate job id detected: {jid}")
        id_to_job[jid] = job

    # Guard against duplicate outputs to avoid file clobbering
    out_map: Dict[str, List[str]] = {}
    for jid, jb in id_to_job.items():
        outp = jb.get("output")
        if outp:
            out_map.setdefault(os.path.abspath(outp), []).append(jid)
    dups = {p: ids for p, ids in out_map.items() if len(ids) > 1}
    if dups:
        msg_lines = [f"{p} <- {ids}" for p, ids in dups.items()]
        raise RuntimeError("Duplicate output paths across jobs are not allowed:\n" + "\n".join(msg_lines))

    pending = set(id_to_job.keys())
    done = set()
    inflight: Dict[str, Any] = {}

    def ready_jobs() -> List[str]:
        ready = []
        for jid in list(pending):
            deps = id_to_job[jid].get("deps", []) or []
            if all(d in done for d in deps):
                ready.append(jid)
        return ready

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending or inflight:
            # schedule ready jobs up to capacity
            capacity = max_workers - len(inflight)
            if capacity > 0:
                for jid in ready_jobs()[:capacity]:
                    fut = pool.submit(_run_single_job, id_to_job[jid])
                    inflight[jid] = fut
                    pending.remove(jid)

            if inflight:
                done_fs, _ = wait(list(inflight.values()), timeout=0.2, return_when=FIRST_COMPLETED)
                for f in list(done_fs):
                    # find jid by future
                    found = None
                    for jid, fut in inflight.items():
                        if fut is f:
                            found = jid
                            break
                    if found is None:
                        continue
                    try:
                        f.result()
                        done.add(found)
                        with _PRINT_LOCK:
                            print(f"[batch] job finished: {found}")
                    except Exception as e:
                        done.add(found)
                        with _PRINT_LOCK:
                            print(f"[batch] job failed: {found}: {e}")
                    finally:
                        inflight.pop(found, None)
            else:
                time.sleep(0.1)

    batch_elapsed = time.time() - batch_start
    with _PRINT_LOCK:
        print(f"[batch] all jobs completed: {len(done)} total")
        print(f"[timer] ⏱️  Total batch execution time: {batch_elapsed:.2f}s ({batch_elapsed / 60:.2f} minutes)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("multi_agent.agent_runner")
    sp = p.add_subparsers(dest="cmd", required=True)

    rp = sp.add_parser("run", help="Run one agent with local files as input and write output")
    rp.add_argument("--agent", required=True, help="Agent name (deepseek|doubao|chatgpt|gemini|gemini_audit)")
    rp.add_argument("--provider", choices=["openai", "openai_compat", "gemini", "kimi"], default="openai",
                    help="LLM provider type: openai (OpenAI official or compatible with base_url), openai_compat (generic OpenAI-compatible API), gemini (Google Gemini native SDK)")
    rp.add_argument("--model", required=False, default=None,
                    help="Model name (defaults: kimi→kimi-k2-thinking; deepseek→deepseek-reasoner; doubao→doubao-seed-1-6-thinking-250715)")
    rp.add_argument("--system-prompts", nargs="+", default=[],
                    help="Paths of system prompt markdown files (e.g., agent prompt, V5/V6规范)")
    rp.add_argument("--inputs", nargs="+", default=[], help="Paths of input files (e.g., 教材md，或各agent输出md)")
    rp.add_argument("--inputs-glob", nargs="+", default=[],
                    help="Glob patterns for input files, e.g., 'exports/multi_agent/inputs/*.md' (supports ** recursive)")
    rp.add_argument("--input-dir", default=None, help="Directory to scan for inputs")
    rp.add_argument("--exts", nargs="+", default=[".md", ".txt"],
                    help="File extensions to include when using --input-dir (with leading dot or not)")
    rp.add_argument("--inputs-file", default=None, help="A text file listing input paths (one per line)")
    rp.add_argument("--output", required=True, help="Where to write the agent output markdown")
    rp.add_argument("--max-tokens", type=int, default=2000)
    rp.add_argument("--temperature", type=float, default=0.2)
    rp.add_argument("--api-key", default=None, help="API key (fallback to env)")
    rp.add_argument("--base", default=None, help="API base URL (fallback to env)")
    rp.add_argument("--key-env", default=None, help="Name of env var for API key when using openai_compat")
    rp.add_argument("--base-env", default=None, help="Name of env var for base URL when using openai_compat")
    rp.add_argument("--timeout", type=int, default=120, help="HTTP read timeout seconds for OpenAI-compatible APIs")
    rp.add_argument("--stream", action="store_true",
                    help="Stream tokens to console while generating (also writes full result to file)")
    rp.add_argument("--show-thinking", dest="show_thinking", action="store_true",
                    help="When available (thinking models), echo reasoning_content to console (not saved to file)")
    rp.add_argument("--auto-chunk", action="store_true",
                    help="Automatically split oversized prompts into multiple sequential requests and concatenate outputs")
    rp.add_argument("--extra", default=None, help="Additional system instructions appended after prompts")
    rp.set_defaults(func=run_agent)

    bp = sp.add_parser("batch", help="Run multiple jobs concurrently from a JSON jobs file (supports deps)")
    bp.add_argument("--jobs-file", required=True, help="Path to JSON file describing jobs list")
    bp.add_argument("--max-workers", type=int, default=3, help="Max number of concurrent jobs")
    bp.set_defaults(func=run_batch)

    return p


_dotenv_loaded: bool = False


def _load_project_env_files() -> None:
    """从项目根目录加载 `.env`（与 `project_dotenv` / run_converter 行为一致）。"""
    try:
        from project_dotenv import load_project_dotenv

        load_project_dotenv()
        return
    except ImportError:
        pass
    try:
        from pathlib import Path

        from dotenv import load_dotenv

        root = Path(__file__).resolve().parent.parent
        env_path = root / ".env"
        if env_path.is_file():
            load_dotenv(env_path, override=False)
    except ImportError:
        pass


def _ensure_dotenv_loaded() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _load_project_env_files()
    _dotenv_loaded = True


def main() -> None:
    _ensure_dotenv_loaded()
    p = build_parser()
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
