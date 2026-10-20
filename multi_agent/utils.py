from __future__ import annotations

import re
import os
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import List, Dict, Tuple, Optional

# -----------------------------
# Markdown parsing primitives
# -----------------------------

CHN_NUM = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}


@dataclass
class Section:
    level: int
    title: str
    path: Tuple[str, ...]
    lines: List[str] = field(default_factory=list)


HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s*(?P<title>.+?)\s*$")
TOP_CHAPTER_RE = re.compile(r"^第\s*([一二三四五六七八九十百千万\d]+)\s*(章|单元).*")
H2_CN_RE = re.compile(r"^[一二三四五六七八九十]+、.+")
H3_NUM_RE = re.compile(r"^\d+、.+")
H4_PAREN_NUM_RE = re.compile(r"^[（(]\d+[)）].+")
H4_PAREN_CN_RE = re.compile(r"^[（(][一二三四五六七八九十]+[)）].+")

TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
ENHANCE_RE = re.compile(r"^\s*【(?P<type>[^】]{1,10})】")
FLOW_ARROW_RE = re.compile(r"→|->|=>")


def _win_long_path(p: str) -> str:
    try:
        if os.name != "nt":
            return p
        if not p:
            return p
        ap = os.path.abspath(p)
        if ap.startswith("\\\\?\\"):
            return ap
        if ap.startswith("\\\\"):
            return "\\\\?\\UNC\\" + ap.lstrip("\\")
        return "\\\\?\\" + ap
    except Exception:
        return p


def normalize_flow(flow: str) -> str:
    """Normalize flow string for deduplication.
    - Unify arrow symbols to →
    - Collapse whitespace
    - Normalize terminology
    """
    # Unify arrow symbols
    f = flow.replace("->", "→").replace("=>", "→")
    # 统一箭头两侧空白：否则 "A -> B" 与 "A→B" 归一化后 key 不同，去重会漏
    f = re.sub(r"\s*→\s*", "→", f)
    # Collapse multiple spaces to single space
    f = re.sub(r"\s+", " ", f)
    # Strip leading/trailing spaces
    f = f.strip()
    # Normalize terminology (reuse normalize_title logic for terms)
    return normalize_title(f)


def dedupe_flows(flows: List[str]) -> List[str]:
    """Deduplicate flow strings, keeping the most detailed version of each unique flow.
    
    Args:
        flows: List of flow strings (e.g., "A → B → C")
    
    Returns:
        Deduplicated list with most detailed version of each flow
    """
    if not flows:
        return []
    
    seen: Dict[str, str] = {}
    for flow in flows:
        key = normalize_flow(flow)
        if not key:
            continue
        # Keep the longest (most detailed) version for each normalized key
        if key not in seen or len(flow) > len(seen[key]):
            seen[key] = flow
    
    # Preserve order of first occurrence
    result = []
    seen_keys = set()
    for flow in flows:
        key = normalize_flow(flow)
        if key and key not in seen_keys:
            seen_keys.add(key)
            result.append(seen[key])
    
    return result


def normalize_title(title: str) -> str:
    t = title.strip()
    # normalize common punctuation and spaces
    t = re.sub(r"[\s\t]+", " ", t)
    # map Chinese numerals to arabic for matching
    for k, v in CHN_NUM.items():
        t = t.replace(k, v)
    # drop some punctuation
    t = re.sub(r"[：:、，,·。()（）【】\-—]+", "", t)
    return t.lower()


def slugify(title: str) -> str:
    t = normalize_title(title)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t


def detect_heading_level(line: str) -> Optional[Tuple[int, str]]:
    m = HEADING_RE.match(line)
    if m:
        return len(m.group("hashes")), m.group("title").strip()
    if TOP_CHAPTER_RE.match(line):
        return 1, line.strip()
    if H2_CN_RE.match(line):
        return 2, line.strip()
    if H3_NUM_RE.match(line):
        return 3, line.strip()
    if H4_PAREN_NUM_RE.match(line) or H4_PAREN_CN_RE.match(line):
        return 4, line.strip()
    return None


def parse_markdown_structure(text: str) -> Dict[str, Section]:
    """Parse markdown-ish text into a flat map of path_slug -> Section.
    We infer headings from # and common Chinese numbering styles.
    Content under each section is kept raw for later merging.
    """
    lines = text.splitlines()
    stack: List[Section] = []
    sections: Dict[str, Section] = {}

    def current_path() -> Tuple[str, ...]:
        return tuple(s.title for s in stack)

    for raw in lines:
        hd = detect_heading_level(raw)
        if hd:
            level, title = hd
            # pop until we are below this level
            while stack and stack[-1].level >= level:
                stack.pop()
            # push new section
            path = current_path() + (title,)
            sec = Section(level=level, title=title, path=path, lines=[])
            stack.append(sec)
            key = "/".join(slugify(x) for x in path)
            # slugify 会把纯中文标题（如「定义」「特点」）压成空串，
            # 导致不同章节算出相同 key 并静默互相覆盖 —— 3 个章节只剩 1 个。
            # 这里做冲突消解：无冲突时 key 与之前完全一致，不改变既有行为。
            if key in sections:
                n = 2
                while f"{key}#{n}" in sections:
                    n += 1
                key = f"{key}#{n}"
            sections[key] = sec
        else:
            if not stack:
                # put preamble content under a virtual root
                root_key = "_preamble"
                if root_key not in sections:
                    sections[root_key] = Section(level=0, title="_preamble", path=("_preamble",), lines=[])
                sections[root_key].lines.append(raw)
            else:
                stack[-1].lines.append(raw)

    return sections


def is_table_line(line: str) -> bool:
    return bool(TABLE_ROW_RE.match(line))


def is_enhancement_line(line: str) -> bool:
    return bool(ENHANCE_RE.match(line))


def is_flow_line(line: str) -> bool:
    return bool(FLOW_ARROW_RE.search(line))


# 列表标记剥离。
# 关键约束："1." 之后必须紧跟空白 —— 否则 "3.14是圆周率" 会被剥成 "14是圆周率"（数据破坏）。
# "、" 与全角括号是中文列表专用分隔符，不可能出现在小数中，因此不要求空白。
# 这与 split_content 的 bullet 判定保持一致，避免"判定为列表项却剥不掉标记"。
BULLET_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"[\-\*\+]\s+"
    r"|\d+\.\s+"
    r"|\d+、\s*"
    r"|[一二三四五六七八九十]+、\s*"
    r"|[（(]\d+[)）]\s*"
    r"|[（(][一二三四五六七八九十]+[)）]\s*"
    r")"
)


def normalize_bullet(line: str) -> str:
    # remove leading list markers and spaces
    t = BULLET_MARKER_RE.sub("", line)
    t = t.strip()
    # collapse inner spaces and punctuation variants
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[，,]", "，", t)
    return t


def semantic_similarity(a: str, b: str) -> float:
    """Calculate enhanced semantic similarity between two strings.
    
    Uses a multi-signal approach combining:
    1. Character-level sequence matching (SequenceMatcher)
    2. Word/token overlap (Jaccard similarity)
    3. Key term matching (domain-specific terms)
    4. Structural similarity (punctuation patterns, lengths)
    
    Returns a value between 0.0 (completely different) and 1.0 (identical).
    """
    from difflib import SequenceMatcher
    
    # Normalize both strings for comparison
    a_norm = normalize_title(a)
    b_norm = normalize_title(b)
    
    if not a_norm or not b_norm:
        return 0.0
    
    # 完全相同直接返回1.0
    if a_norm == b_norm:
        return 1.0
    
    # --- Signal 1: 字符级序列匹配 ---
    char_sim = SequenceMatcher(None, a_norm, b_norm).ratio()
    
    # --- Signal 2: 词/Token重叠度 (Jaccard) ---
    # 支持中文分词（按字符）和英文分词（按空格/标点）
    def tokenize(text: str) -> set[str]:
        tokens: set[str] = set()
        # 英文单词
        eng_words = re.findall(r'[a-zA-Z]+', text)
        tokens.update(w.lower() for w in eng_words if len(w) > 1)
        # 中文：2-gram 和单字
        cjk_chars = re.findall(r'[\u4e00-\u9fff]', text)
        tokens.update(cjk_chars)
        # 2-gram for better CJK matching
        for i in range(len(cjk_chars) - 1):
            tokens.add(cjk_chars[i] + cjk_chars[i+1])
        # 数字序列
        nums = re.findall(r'\d+', text)
        tokens.update(nums)
        return tokens
    
    a_tokens = tokenize(a_norm)
    b_tokens = tokenize(b_norm)
    
    if a_tokens and b_tokens:
        intersection = len(a_tokens & b_tokens)
        union = len(a_tokens | b_tokens)
        jaccard_sim = intersection / union if union > 0 else 0.0
    else:
        jaccard_sim = char_sim  # fallback
    
    # --- Signal 3: 关键术语匹配（教育/技术领域高频词）---
    KEY_TERMS = {
        # 结构性词汇
        "定义", "概念", "特点", "特征", "分类", "类型", "原理", "原则",
        "步骤", "流程", "方法", "作用", "功能", "优点", "缺点", "区别",
        "联系", "应用", "实例", "示例", "注意", "要点", "总结", "原文", "原句引用",
        # 英文常见术语
        "definition", "concept", "feature", "type", "principle", "method",
        "function", "example", "note", "summary", "step", "process",
    }
    
    def extract_key_terms(text: str) -> set:
        terms = set()
        for term in KEY_TERMS:
            if term in text.lower():
                terms.add(term)
        return terms
    
    a_terms = extract_key_terms(a)
    b_terms = extract_key_terms(b)
    
    if a_terms or b_terms:
        if a_terms and b_terms:
            term_intersection = len(a_terms & b_terms)
            term_union = len(a_terms | b_terms)
            term_sim = term_intersection / term_union if term_union > 0 else 0.0
        else:
            term_sim = 0.0
    else:
        term_sim = 0.5  # 没有关键词时给中性分
    
    # --- Signal 4: 结构相似度（长度比、数字一致性）---
    len_ratio = min(len(a_norm), len(b_norm)) / max(len(a_norm), len(b_norm)) if max(len(a_norm), len(b_norm)) > 0 else 0.0
    
    # 检查数字序列是否一致（对于"第1章"vs"第一章"这类）
    a_nums = re.findall(r'\d+', a)
    b_nums = re.findall(r'\d+', b)
    num_match = 1.0 if a_nums == b_nums else (0.5 if set(a_nums) & set(b_nums) else 0.0)
    
    struct_sim = (len_ratio + num_match) / 2
    
    # --- 加权融合 ---
    # 权重分配：字符匹配40%，词重叠30%，术语匹配15%，结构15%
    final_sim = (
        0.40 * char_sim +
        0.30 * jaccard_sim +
        0.15 * term_sim +
        0.15 * struct_sim
    )
    
    return min(1.0, max(0.0, final_sim))


def cluster_similar_items(items: List[str], threshold: float = 0.75) -> List[str]:
    """Cluster highly similar items and keep the most detailed version from each cluster.
    
    Args:
        items: List of text items to cluster
        threshold: Similarity threshold (0.0-1.0) for grouping items
    
    Returns:
        List of representative items, one per cluster
    """
    if not items:
        return []
    
    clusters: List[List[str]] = []
    for item in items:
        merged = False
        for cluster in clusters:
            # Check similarity with any member in the cluster
            if any(semantic_similarity(item, member) > threshold for member in cluster):
                cluster.append(item)
                merged = True
                break
        if not merged:
            # Create new cluster
            clusters.append([item])
    
    # From each cluster, select the most detailed (longest) version
    return [max(cluster, key=len) for cluster in clusters]


def dedupe_preserve_order(items: List[str]) -> List[str]:
    """Deduplicate items using semantic clustering, preserving order of first occurrence.
    Enhanced version that handles similar but not identical expressions.
    """
    if not items:
        return []
    
    # First pass: exact dedup by normalized key
    seen_keys = set()
    unique_items = []
    for it in items:
        key = normalize_title(it)
        if key and key not in seen_keys:
            seen_keys.add(key)
            unique_items.append(it)
    
    # Second pass: semantic clustering to merge similar items
    # Use a higher threshold (0.8) to only merge very similar items
    clustered = cluster_similar_items(unique_items, threshold=0.8)
    return clustered


def split_content(lines: List[str]) -> Dict[str, List[str]]:
    """Split lines into categories for merging."""
    bullets, paragraphs, tables, flows, enhancements, code, other = [], [], [], [], [], [], []
    in_code = False
    for ln in lines:
        if ln.strip().startswith("```"):
            in_code = not in_code
            code.append(ln)
            continue
        if in_code:
            code.append(ln)
            continue
        if is_table_line(ln):
            tables.append(ln)
        elif is_enhancement_line(ln):
            enhancements.append(ln)
        elif is_flow_line(ln):
            flows.append(ln)
        elif re.match(r"^\s*([\-\*\+]|\d+[\.、]|[一二三四五六七八九十]+、|[（(]\d+[)）]|[（(][一二三四五六七八九十]+[)）])", ln):
            bullets.append(ln)
        elif ln.strip():
            paragraphs.append(ln)
        else:
            other.append(ln)
    return {
        "bullets": bullets,
        "paragraphs": paragraphs,
        "tables": tables,
        "flows": flows,
        "enhancements": enhancements,
        "code": code,
        "other": other,
    }


def stitch_markdown(title: Optional[str], parts: Dict[str, List[str]], heading_level: Optional[int]) -> List[str]:
    out: List[str] = []
    if title and heading_level:
        out.append("#" * heading_level + f" {title}")
    out.extend(parts.get("paragraphs", []))

    # bullets
    blts = parts.get("bullets", [])
    if blts:
        for b in blts:
            if not re.match(r"^\s*[\-\*\+] ", b):
                out.append(f"- {normalize_bullet(b)}")
            else:
                out.append(b)

    # tables must be separated by a blank line before and after
    tbl = parts.get("tables", [])
    if tbl:
        if out and out[-1].strip():
            out.append("")
        out.extend(tbl)
        out.append("")

    # flows
    out.extend(parts.get("flows", []))

    # enhancements (limit per type to 2)
    enh = []
    type_count: Dict[str, int] = {}
    for e in parts.get("enhancements", []):
        m = ENHANCE_RE.match(e)
        if m:
            t = m.group("type")
            if type_count.get(t, 0) < 2:
                type_count[t] = type_count.get(t, 0) + 1
                enh.append(e)
    if enh:
        out.extend(enh)

    # code blocks and others as-is
    out.extend(parts.get("code", []))
    out.extend(parts.get("other", []))
    return out


def safe_read_text(path: str) -> str:
    """Read text file with automatic encoding detection.
    Tries UTF-8 first, falls back to GBK for Chinese files, then CP1252 for legacy files.
    """
    # Try UTF-8 first (most common)
    try:
        with open(_win_long_path(path), "r", encoding="utf-8") as f:
            content = f.read()
            # Validate: check for common UTF-8 issues
            if "\ufffd" not in content and not any(ord(c) > 0x10FFFF for c in content):
                return content
    except (UnicodeDecodeError, UnicodeError):
        pass
    
    # Try GBK for Chinese legacy files
    try:
        with open(_win_long_path(path), "r", encoding="gbk") as f:
            content = f.read()
            # Basic sanity check: should not have replacement characters
            if "\ufffd" not in content:
                return content
    except (UnicodeDecodeError, UnicodeError, LookupError):
        pass
    
    # Try GB18030 (superset of GBK)
    try:
        with open(_win_long_path(path), "r", encoding="gb18030") as f:
            return f.read()
    except (UnicodeDecodeError, UnicodeError, LookupError):
        pass
    
    # Last resort: UTF-8 with error handling
    with open(_win_long_path(path), "r", encoding="utf-8", errors="replace") as f:
        return f.read()


class SimpleFileLock:
    """A minimal cross-platform file lock using a lock file with O_EXCL.
    Not re-entrant across threads/processes. Intended for short critical sections.
    """

    def __init__(self, lock_path: str, timeout: float = 60.0, poll_interval: float = 0.1) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        start = time.time()
        # Warn if waiting too long for the lock to help diagnose "卡住"现象
        try:
            warn_after = float(os.getenv("LOCK_WARN_AFTER_SECONDS", "10"))  # seconds
            warn_every = float(os.getenv("LOCK_WARN_EVERY_SECONDS", "30"))  # seconds
        except Exception:
            warn_after, warn_every = 10.0, 30.0
        last_warn = start
        while True:
            try:
                # O_EXCL ensures creation fails if file already exists
                self._fd = os.open(_win_long_path(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                # Best-effort: write PID for debugging
                try:
                    os.write(self._fd, str(os.getpid()).encode("utf-8", errors="ignore"))
                except Exception:
                    pass
                return
            except FileExistsError:
                now = time.time()
                # Emit periodic waiting hints
                try:
                    waited = now - start
                    if waited >= warn_after and (now - last_warn) >= max(1.0, warn_every):
                        print(f"[lock:waiting] {self.lock_path} 已等待 {int(waited)}s 获取文件锁...")
                        print("提示：若确认无其他进程在运行，可用以下命令清理过期锁：")
                        print("  .\\clean_locks.ps1 -RepoRoot . -BooksRoot 'exports\\Organized_Books' -ProgressLock 'exports\\multi_agent\\final\\progress.json.lock' -MaxAgeMinutes 5")
                        print(f"  或删除当前锁(确保安全后再执行)：Remove-Item -LiteralPath '{self.lock_path}' -Force")
                        last_warn = now
                except Exception:
                    pass
                if now - start >= self.timeout:
                    raise TimeoutError(f"Timeout acquiring lock: {self.lock_path}")
                time.sleep(self.poll_interval)

    def release(self) -> None:
        try:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
        finally:
            self._fd = None
        try:
            os.unlink(_win_long_path(self.lock_path))
        except FileNotFoundError:
            pass
        except Exception:
            # Best effort: if unlink fails, leave the lockfile; future attempts may timeout
            pass

    def __enter__(self) -> SimpleFileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def safe_write_text(path: str, text: str) -> None:
    dirn = os.path.dirname(path)
    if dirn:
        os.makedirs(_win_long_path(dirn), exist_ok=True)
    # Atomic write via temporary file + replace
    tmp_path = f"{path}.tmp.{os.getpid()}.{int(time.time()*1000)}"
    with open(_win_long_path(tmp_path), "w", encoding="utf-8") as f:
        f.write(text)
    # os.replace is atomic on POSIX and Windows
    # On Windows, os.replace can intermittently fail with Access Denied / Sharing Violation
    # if the destination is temporarily held by antivirus/indexer/previewers.
    # Mitigate with a short bounded retry loop.
    if os.name == "nt":
        try:
            retries = int(os.getenv("SAFE_WRITE_REPLACE_RETRIES", "30"))
        except Exception:
            retries = 30
        try:
            base_sleep = float(os.getenv("SAFE_WRITE_REPLACE_SLEEP_SECONDS", "0.05"))
        except Exception:
            base_sleep = 0.05

        last_exc: Optional[BaseException] = None
        for i in range(max(1, retries)):
            try:
                os.replace(_win_long_path(tmp_path), _win_long_path(path))
                return
            except OSError as e:
                # 5: Access is denied, 32: The process cannot access the file because it is being used by another process
                winerr = getattr(e, "winerror", None)
                if winerr in (5, 32):
                    last_exc = e
                    time.sleep(min(2.0, base_sleep * (1.25 ** i)))
                    continue
                raise
            except PermissionError as e:
                last_exc = e
                time.sleep(min(2.0, base_sleep * (1.25 ** i)))
                continue
        # If we get here, retries exhausted
        raise OSError(f"safe_write_text replace failed after {retries} retries: {path}") from last_exc

    os.replace(_win_long_path(tmp_path), _win_long_path(path))
