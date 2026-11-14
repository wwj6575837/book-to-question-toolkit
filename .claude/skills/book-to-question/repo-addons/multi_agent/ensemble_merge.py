from __future__ import annotations

import re
import time
import hashlib
from functools import lru_cache
from typing import Dict, List, Tuple, Any, Set, Optional
from .utils import (
    parse_markdown_structure, split_content, dedupe_preserve_order, 
    normalize_bullet, stitch_markdown, safe_read_text, ENHANCE_RE, 
    normalize_title, dedupe_flows, semantic_similarity, cluster_similar_items
)
from .agent_runner import _PRINT_LOCK


@lru_cache(maxsize=50000)
def _sim_cached(a_norm: str, b_norm: str) -> float:
    return semantic_similarity(a_norm, b_norm)


def _sim(a: str, b: str) -> float:
    ra = a or ""
    rb = b or ""
    if not ra.strip() or not rb.strip():
        return 0.0
    if len(ra) > 5000 or len(rb) > 5000:
        return semantic_similarity(ra, rb)
    try:
        na = normalize_title(ra)
        nb = normalize_title(rb)
    except Exception:
        na = ra.strip().lower()
        nb = rb.strip().lower()
    if not na or not nb:
        return 0.0
    if na <= nb:
        return _sim_cached(na, nb)
    return _sim_cached(nb, na)


def _align_section_keys_fuzzy(secs_by_agent: Dict[str, Dict[str, Any]], order_agents: List[str]) -> Dict[
    str, Dict[str, Any]]:
    if not secs_by_agent or not order_agents:
        return secs_by_agent

    idx_map = {ag: i for i, ag in enumerate(order_agents)}
    extra_agents = [ag for ag in secs_by_agent.keys() if ag not in idx_map]
    if extra_agents:
        for ag in sorted(extra_agents):
            idx_map[ag] = len(idx_map)
            order_agents = order_agents + [ag]

    threshold = 0.92
    canon_keys_by_group: Dict[Tuple[str, int], List[str]] = {}
    canon_meta: Dict[str, Tuple[str, str]] = {}
    out: Dict[str, Dict[str, Any]] = {ag: {} for ag in secs_by_agent.keys()}

    def _parent_key(k: str) -> str:
        return k.rsplit("/", 1)[0] if "/" in k else ""

    # --- 辅助：提取标题中的数字序列用于强行对齐 ---
    def _get_struct_nums(title: str) -> List[int]:
        """
        从标题中提取数字序列，支持 '1.1' 和 '第一章'。
        用于在 fuzzy match 失败时，挽救那些仅仅是格式不同但编号一致的章节。
        例如：'第1章' vs '项目一 食品营养...'
        """
        import re
        if not title:
            return []

        # 简单中文数字映射
        cn_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}

        nums = []
        # 匹配 阿拉伯数字 或 中文数字
        matches = re.findall(r'\d+|[一二三四五六七八九十]+', title)

        for m in matches:
            if m.isdigit():
                nums.append(int(m))
            else:
                # 处理中文数字：仅处理最简单的单字或双字，足够应对章节层级
                # 例如 "一"->1, "十一"->11
                if len(m) == 1 and m in cn_map:
                    nums.append(cn_map[m])
                elif len(m) == 2 and m.startswith('十') and m[1] in cn_map:
                    nums.append(10 + cn_map[m[1]])
                elif len(m) == 2 and m.endswith('十') and m[0] in cn_map:  # 二十
                    nums.append(cn_map[m[0]] * 10)
                elif m == '十':
                    nums.append(10)
                # 复杂的暂不处理，防止误判
        return nums

    # ---------------------------------------------

    for ag in order_agents:
        sec_map = secs_by_agent.get(ag) or {}
        if not sec_map:
            continue
        for k, sec in sec_map.items():
            try:
                lvl = int(getattr(sec, "level", 0))
            except Exception:
                lvl = 0
            parent = _parent_key(k)
            group = (parent, lvl)
            best_key: Optional[str] = None
            best_score = 0.0

            # 预计算当前标题的特征
            curr_title = getattr(sec, "title", "")
            curr_nums = _get_struct_nums(curr_title)
            curr_len = len(curr_title)

            for ck in canon_keys_by_group.get(group, []):
                origin_ag, origin_title = canon_meta.get(ck, ("", ""))
                if idx_map.get(origin_ag, 10 ** 9) >= idx_map.get(ag, 10 ** 9):
                    continue

                # 1. 基础相似度计算
                try:
                    score = _sim(curr_title, origin_title)
                except Exception:
                    score = 0.0

                # 2. 救援逻辑 (Rescue Logic)
                # 如果相似度不达标，但满足“编号一致 + 长短标题悬殊”的情况，强制对齐
                # 场景：DeepSeek 输出 "第1章" (短)，Doubao 输出 "项目一 食品..." (长)
                if score < threshold:
                    origin_nums = _get_struct_nums(origin_title)
                    origin_len = len(origin_title)

                    # 仅当两者都有编号，且编号完全一致时才考虑救援
                    if curr_nums and origin_nums and curr_nums == origin_nums:
                        # 判定是否为“简写 vs 全称”的关系
                        # 条件：至少有一个标题很短（<12字符，疑似简写），且两者长度差异明显
                        has_short = (curr_len < 12) or (origin_len < 12)

                        # 特殊保护：如果层级相同、编号相同，且其中一个是简短的“第X章/项目X”，
                        # 即使另一个很长，也大概率是同一个章节。
                        if has_short:
                            score = 0.99  # 强制提升分数，视为匹配

                if score > best_score:
                    best_score = score
                    best_key = ck

            canon = best_key if (best_key is not None and best_score >= threshold) else k
            if canon == k and canon not in canon_meta:
                canon_meta[canon] = (ag, curr_title)
                canon_keys_by_group.setdefault(group, []).append(canon)

            if canon not in out[ag]:
                out[ag][canon] = sec
            else:
                try:
                    # 保留内容更长的版本（通常意味着标题也更完整，或者内容更丰富）
                    if len(getattr(sec, "lines", []) or []) > len(getattr(out[ag][canon], "lines", []) or []):
                        out[ag][canon] = sec
                except Exception:
                    pass

    return out

def _is_formula_substitution_example(text: str) -> bool:
    """
    Heuristic: detect a short formula-substitution style example.
    """
    b = (text or "").strip()
    if not b or len(b) > 160: # Slightly increased threshold
        return False
    
    # Check for LaTeX or Math symbols
    has_latex = ("$" in b) or bool(re.search(r"\\(frac|sqrt|mathrm|sum|int|cdot|times|alpha|beta|gamma|theta|pi|sigma|Delta|Sigma|Omega)", b))
    has_digits = bool(re.search(r"\d", b))
    has_math_ops = bool(re.search(r"[=≈≃≅∼×÷\+\-\*/]", b))
    has_math_symbols = any(sym in b for sym in ("√", "∑", "∫", "≠", "≤", "≥"))
    
    if (has_latex and has_digits) or (has_digits and has_math_ops) or has_math_symbols:
        return True
    return False

def _merge_structure_keys(secs_by_agent: Dict[str, Dict[str, Any]], order_agents: List[str]) -> List[str]:
    """
    Construct a merged skeleton of section keys preserving relative order.
    Strategy: Anchor-based insertion. 
    1. Start with the primary agent's keys as the skeleton.
    2. For subsequent agents, insert missing keys relative to their known neighbors (anchors).
    """
    if not order_agents:
        return []

    # Start with the primary agent's structure
    skeleton = list(secs_by_agent.get(order_agents[0], {}).keys())
    
    # Iteratively merge other agents' structures
    for ag in order_agents[1:]:
        current_keys = list(secs_by_agent.get(ag, {}).keys())
        if not current_keys:
            continue
            
        for i, key in enumerate(current_keys):
            if key in skeleton:
                continue
            
            # Find insertion point: look for the nearest preceding key (anchor) that exists in skeleton
            anchor_idx = -1
            for j in range(i - 1, -1, -1):
                prev_key = current_keys[j]
                if prev_key in skeleton:
                    anchor_idx = skeleton.index(prev_key)
                    break
            
            if anchor_idx != -1:
                # Insert after the found anchor
                skeleton.insert(anchor_idx + 1, key)
            else:
                # No preceding anchor found. Check for succeeding anchor to insert before it?
                # Or just append to the end? Or insert at start?
                # Strategy: If no preceding anchor, try to find a succeeding anchor
                next_anchor_idx = -1
                for j in range(i + 1, len(current_keys)):
                    next_key = current_keys[j]
                    if next_key in skeleton:
                        next_anchor_idx = skeleton.index(next_key)
                        break
                
                if next_anchor_idx != -1:
                    skeleton.insert(next_anchor_idx, key)
                else:
                    # If the section is completely isolated, append it to the end
                    skeleton.append(key)
    
    return skeleton

def _parse_table_structure(table_text: str) -> Tuple[Optional[List[str]], List[str], int]:
    """Parses a markdown table into (headers, data_rows, valid_row_count)."""
    lines = [ln.strip() for ln in table_text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None, [], 0
    
    # Check for separator line
    sep_idx = -1
    header_sep_re = re.compile(r"^\|?(\s*:?-{3,}:?\s*\|?)+$")
    for i, ln in enumerate(lines):
        if header_sep_re.match(ln):
            sep_idx = i
            break
            
    if sep_idx == -1:
        # Not a valid markdown table
        return None, lines, 0

    # Extract headers
    header_line = lines[sep_idx - 1]
    headers = [c.strip() for c in header_line.strip("|").split("|")]
    
    # Extract rows (skip header and separator)
    rows = []
    for i in range(len(lines)):
        if i == sep_idx or i == sep_idx - 1:
            continue
        if "|" in lines[i]:
            rows.append(lines[i])
            
    return headers, rows, len(rows)


def _headers_fuzzy_compatible(h1: List[str], h2: List[str], threshold: float = 0.75) -> Tuple[
    bool, float, Optional[Dict[int, int]]]:
    """
    使用 [全局排序优先策略] 检查表头兼容性并计算列映射。
    解决了传统贪婪算法对列顺序敏感的问题，无需引入 numpy/scipy。
    """
    if not h1 or not h2:
        return False, 0.0, None

    # --- A. 生成候选池 ---
    # 只要单列相似度达到阈值的 80%，就放入候选池等待竞争
    min_col_threshold = threshold * 0.8
    candidates = []  # (score, h1_idx, h2_idx)

    for i, col1 in enumerate(h1):
        for j, col2 in enumerate(h2):
            score = _sim(col1, col2)
            if score >= min_col_threshold:
                candidates.append((score, i, j))

    # --- B. 全局排序 (关键步骤) ---
    # 优先锁定全场相似度最高的配对，解决 "ID vs User ID" 问题
    candidates.sort(key=lambda x: x[0], reverse=True)

    # --- C. 锁定映射 ---
    mapping: Dict[int, int] = {}
    used_h1 = set()
    used_h2 = set()
    total_score = 0.0

    for score, i, j in candidates:
        if i not in used_h1 and j not in used_h2:
            mapping[i] = j
            used_h1.add(i)
            used_h2.add(j)
            total_score += score

    # --- D. 兼容性判定 ---
    matches_count = len(mapping)
    min_len = min(len(h1), len(h2))

    # 1. 覆盖率检查：至少匹配了短表的 50% 列数
    # (max(1, ...) 确保即使只有1列也要匹配上)
    required_matches = max(1, int(min_len * 0.5))
    if matches_count < required_matches:
        return False, 0.0, None

    # 2. 平均分检查：仅计算匹配上的列的平均质量
    avg_score = total_score / matches_count if matches_count > 0 else 0.0

    if avg_score >= threshold:
        return True, avg_score, mapping

    return False, avg_score, None


def _normalize_row_for_dedup(row: str) -> str:
    """Normalize a table row for deduplication, handling different formatting styles."""
    # 去除空白和格式差异
    norm = re.sub(r"\s+", "", row)
    # 统一标点符号
    norm = norm.replace("，", ",").replace("。", ".").replace("；", ";")
    # 去除两端的 |
    norm = norm.strip("|")
    return norm

def _merge_tables_content(table_items: List[Tuple[str, str]], order_agents: List[str]) -> List[str]:
    """
    Intelligently merge tables with fuzzy header matching and structural alignment.

    Fixes:
    1. Row/Header column mismatch (rendering misalignment).
    2. Fragile cell splitting (handling escaped pipes).
    3. Performance bottlenecks in row deduplication.
    """
    if not table_items:
        return []

    # --- 1. 内部辅助工具：稳健的行拆分与重组 ---
    def _safe_split_row(row_str: str) -> List[str]:
        r"""使用正则拆分，忽略转义的竖线 (\|)"""
        # 去除首尾空白和两侧的管道符
        content = row_str.strip()
        if content.startswith('|'): content = content[1:]
        if content.endswith('|'): content = content[:-1]
        # 使用负向回顾断言拆分：匹配前面不是反斜杠的竖线
        parts = re.split(r'(?<!\\)\|', content)
        return [p.strip() for p in parts]

    def _safe_join_row(cells: List[str]) -> str:
        """重组行"""
        return "| " + " | ".join(cells) + " |"

    # --- 2. 解析所有表格 ---
    parsed_tables = []
    for ag, text in table_items:
        headers, rows, count = _parse_table_structure(text)
        if headers:
            parsed_tables.append({
                "agent": ag,
                "headers": headers,
                "rows": rows,
                "raw": text,
                "col_count": len(headers),
                "row_count": count
            })

    if not parsed_tables:
        # Fallback: select longest text if parsing fails
        return sorted([t[1] for t in table_items], key=len, reverse=True)[0].splitlines()

    # --- 3. 选择基础表格 (Base Schema) ---
    # 策略：优先选择列数更多(col_count)的表格，以容纳更多信息维度
    def quality_score(pt):
        priority = -99
        if pt["agent"] in order_agents:
            priority = -order_agents.index(pt["agent"])

        # 权重顺序: 列数 > 行数 > 内容长度 > Agent优先级
        content_len = sum(len(r) for r in pt["rows"])
        return (pt["col_count"], pt["row_count"], content_len, priority)

    base = sorted(parsed_tables, key=quality_score, reverse=True)[0]
    base_headers = base["headers"]

    # --- 4. 初始化基础行数据 ---
    # 使用 row_key_map 实现 O(1) 的行查找与更新
    # 结构: { norm_key: index_in_final_rows_list }
    final_rows: List[str] = []
    row_key_map: Dict[str, int] = {}

    for row in base["rows"]:
        norm_key = _normalize_row_for_dedup(row)
        if norm_key not in row_key_map:
            row_key_map[norm_key] = len(final_rows)
            final_rows.append(row)
        else:
            # 如果同一个表内有重复Key，保留信息量更大(更长)的
            idx = row_key_map[norm_key]
            if len(row) > len(final_rows[idx]):
                final_rows[idx] = row

    # --- 5. 融合其他表格数据 ---
    for pt in parsed_tables:
        if pt is base:
            continue

        # 检查表头兼容性并获取列映射
        is_compatible, _, col_mapping = _headers_fuzzy_compatible(
            base_headers, pt["headers"], threshold=0.70
        )

        if is_compatible and col_mapping is not None:
            for row in pt["rows"]:
                # 解析源行
                cells = _safe_split_row(row)

                # 补全源行缺失的单元格 (防止源行本身格式错误)
                while len(cells) < len(pt["headers"]):
                    cells.append("")

                # 映射并重组单元格到 Base 结构
                new_cells = [""] * len(base_headers)
                for base_idx, other_idx in col_mapping.items():
                    # 安全检查：防止 other_idx 越界
                    if other_idx < len(cells):
                        new_cells[base_idx] = cells[other_idx]

                # 重组为字符串
                new_row = _safe_join_row(new_cells)
                norm_key = _normalize_row_for_dedup(new_row)

                # 去重逻辑
                if norm_key not in row_key_map:
                    row_key_map[norm_key] = len(final_rows)
                    final_rows.append(new_row)
                else:
                    # 冲突处理：如果新行包含更多信息，则替换旧行
                    idx = row_key_map[norm_key]
                    existing_row = final_rows[idx]
                    if len(new_row) > len(existing_row):
                        final_rows[idx] = new_row

    # --- 6. 优化表头描述 ---
    # 尝试保留更详细的表头文案
    best_headers = base_headers[:]
    for pt in parsed_tables:
        if pt is base: continue
        is_compat, _, _ = _headers_fuzzy_compatible(base_headers, pt["headers"], threshold=0.70)
        if is_compat:
            for i, (bh, ph) in enumerate(zip(best_headers, pt["headers"])):
                # 如果其他表的对应表头更长且语义相似，则采用
                if len(ph) > len(bh) and _sim(bh, ph) > 0.6:
                    best_headers[i] = ph

    # --- 7. 提取与构建对齐方式 ---
    alignment = []
    try:
        raw_lines = base["raw"].splitlines()
        for ln in raw_lines:
            # 增强正则，匹配Markdown分隔线 (如 |:---|---|)
            if re.match(r"^\s*\|?[\s\-:]+\|[\s\-:|]+\s*$", ln) and "-" in ln:
                parts = ln.strip().strip("|").split("|")
                for p in parts:
                    p = p.strip()
                    if p.startswith(":") and p.endswith(":"):
                        alignment.append(":---:")
                    elif p.endswith(":"):
                        alignment.append("---:")
                    elif p.startswith(":"):
                        alignment.append(":---")
                    else:
                        alignment.append("---")
                break
    except Exception:
        pass

    # 补齐或截断对齐方式以匹配最终表头
    while len(alignment) < len(best_headers):
        alignment.append("---")
    alignment = alignment[:len(best_headers)]

    # --- 8. 构建输出部分 ---
    header_line = "| " + " | ".join(best_headers) + " |"
    sep_line = "| " + " | ".join(alignment) + " |"

    # --- 9. 最终安全网：强制行归一化 (The Final Safety Net) ---
    # 这是修复 "DeepSeek 2列 vs 融合后 3列" 导致错位的关键步骤
    normalized_output_rows = []
    target_col_count = len(best_headers)

    for row in final_rows:
        # 使用稳健拆分
        parts = _safe_split_row(row)

        # 跳过纯空行 (防御性编程)
        if not any(p.strip() for p in parts):
            continue

        # 补齐：如果行数据列数少于表头，补空字符串
        if len(parts) < target_col_count:
            parts.extend([""] * (target_col_count - len(parts)))

        # 截断：如果行数据列数多于表头，强行截断 (防止破坏表格结构)
        # 注意：这依赖于前面的 col_mapping 逻辑已经把数据放到了正确位置
        if len(parts) > target_col_count:
            parts = parts[:target_col_count]

        normalized_output_rows.append(_safe_join_row(parts))

    return [header_line, sep_line] + normalized_output_rows

def _select_best_code(code_items: List[Tuple[str, str]]) -> List[str]:
    """
    Select best code blocks and dedup.
    Scoring: +Length, +Comments, +SyntaxCorrectness(Simulated)
    """
    if not code_items:
        return []
    
    unique_blocks = {}
    
    for ag, code in code_items:
        # Normalize for dedup: remove empty lines and trailing spaces
        norm = re.sub(r"^\s*$\n", "", code, flags=re.MULTILINE).strip()
        if not norm:
            continue
        # Use hash for key
        h = hashlib.md5(norm.encode('utf-8')).hexdigest()
        
        if h not in unique_blocks:
            # Score the code
            # 1. Length
            score = len(code)
            # 2. Comments (proxy for quality)
            comments = code.count("#") + code.count("//")
            score += comments * 10
            # 3. Code block markers (correct markdown)
            if code.startswith("```") and code.endswith("```"):
                score += 50
                
            unique_blocks[h] = {"code": code, "score": score, "agent": ag}
        else:
            # Keep the one with better formatting if content is same (rare)
            if len(code) > len(unique_blocks[h]["code"]):
                 unique_blocks[h]["code"] = code

    # Sort by score
    sorted_blocks = sorted(unique_blocks.values(), key=lambda x: x["score"], reverse=True)
    
    # Return top blocks. Since this is usually one section, we typically want the best version of the code.
    # But if there are multiple DISTINCT code blocks (different hashes), we keep them.
    # return [b["code"] for b in sorted_blocks]
    if sorted_blocks:
        return [sorted_blocks[0]["code"]]
    return []


def _merge_sections(secs_by_agent: Dict[str, Dict[str, Any]], order_agents: List[str]) -> List[str]:
    """Merge sections from multiple agents into final markdown lines.

    Enhancements over a naive merge:
    - Anchor-based section ordering with a primary agent as structural backbone
    - Paragraph selection by consensus, plus informative sentence supplements
    - Table row merging when headers match
    - Code block scoring and deduplication
    - Enhancement consensus with special handling for formula examples
    """

    secs_by_agent = _align_section_keys_fuzzy(secs_by_agent, order_agents)

    # ---- 1. Determine Section Order (structure skeleton) ----
    keys = set()
    for sec_map in secs_by_agent.values():
        keys.update(sec_map.keys())

    # primary agent = first in priority that actually has sections
    primary_agent: Optional[str] = None
    for ag in order_agents:
        if secs_by_agent.get(ag):
            primary_agent = ag
            break

    def parent_key_of(path_key: str) -> str:
        if "/" in path_key:
            return path_key.rsplit("/", 1)[0]
        return ""

    if not primary_agent:
        # Fallback: global ordering similar to original implementation
        def order_key(k: str) -> Tuple[int, int, str]:
            for idx, ag in enumerate(order_agents):
                sec_map = secs_by_agent.get(ag, {})
                if k in sec_map:
                    lvl = sec_map[k].level
                    return (idx, lvl, k)
            return (len(order_agents), 9, k)

        sorted_keys = sorted(keys, key=order_key)
    else:
        # Anchor-based ordering: use primary agent section order as backbone,
        # then insert unique sections from other agents near their anchored neighbours.
        primary_secs = secs_by_agent.get(primary_agent, {})
        placed: List[str] = []
        seen_keys: Set[str] = set()
        for k in primary_secs.keys():
            if k in keys and k not in seen_keys:
                placed.append(k)
                seen_keys.add(k)

        # Insert sections from other agents
        for ag in order_agents:
            if ag == primary_agent:
                continue
            sec_map = secs_by_agent.get(ag, {})
            if not sec_map:
                continue
            ag_seq = list(sec_map.keys())
            for idx, k in enumerate(ag_seq):
                if k not in keys or k in seen_keys:
                    continue
                insert_at: Optional[int] = None
                parent = parent_key_of(k)
                # Try to place after last sibling under same parent
                if parent:
                    sibling_indices = [i for i, pk in enumerate(placed) if parent_key_of(pk) == parent]
                    if sibling_indices:
                        insert_at = max(sibling_indices) + 1
                # Otherwise, interpolate between nearest already-placed neighbours from this agent
                if insert_at is None:
                    prev_pos: Optional[int] = None
                    j = idx - 1
                    while j >= 0:
                        prev_key = ag_seq[j]
                        if prev_key in seen_keys:
                            try:
                                prev_pos = placed.index(prev_key)
                            except ValueError:
                                prev_pos = None
                            break
                        j -= 1
                    next_pos: Optional[int] = None
                    j = idx + 1
                    while j < len(ag_seq):
                        next_key = ag_seq[j]
                        if next_key in seen_keys:
                            try:
                                next_pos = placed.index(next_key)
                            except ValueError:
                                next_pos = None
                            break
                        j += 1
                    if prev_pos is not None and next_pos is not None:
                        insert_at = prev_pos + 1
                    elif prev_pos is not None:
                        insert_at = prev_pos + 1
                    elif next_pos is not None:
                        insert_at = next_pos
                    else:
                        insert_at = len(placed)
                placed.insert(insert_at, k)
                seen_keys.add(k)

        # Append any remaining keys that never appeared in the primary sequence (very rare)
        for k in keys:
            if k not in seen_keys:
                placed.append(k)
                seen_keys.add(k)
        sorted_keys = placed

    # ---- 2. Merge content per section ----
    out_lines: List[str] = []

    for key in sorted_keys:
        # Representative section for title/level
        rep = None
        rep_agent = None
        for ag in order_agents:
            sec = secs_by_agent.get(ag, {}).get(key)
            if sec is not None:
                rep = sec
                rep_agent = ag
                break
        if rep is None:
            continue


        final_title = getattr(rep, "title", "")
        final_level = getattr(rep, "level", 0)

        for ag in order_agents:
            s = secs_by_agent.get(ag, {}).get(key)
            if s:
                t = getattr(s, "title", "")
                # 简单的启发式：取更长的标题，因为它通常包含具体的名称
                if len(t) > len(final_title):
                    final_title = t

        agg_parts = {
            "paragraphs": [],
            "bullets": [],
            "tables": [],
            "flows": [],
            "enhancements": [],
            "code": [],
            "other": [],
        }

        paragraphs_by_agent: Dict[str, List[str]] = {}
        bullet_votes: Dict[str, Set[str]] = {}
        bullet_examples: Dict[str, str] = {}
        bullet_order: List[str] = []
        table_items: List[Tuple[str, str]] = []
        flow_items: List[str] = []
        enhancement_items: List[Tuple[str, str]] = []
        code_items: List[Tuple[str, str]] = []
        other_items: List[str] = []

        for ag in order_agents:
            sec = secs_by_agent.get(ag, {}).get(key)
            if not sec:
                continue
            parts = split_content(sec.lines)

            # paragraphs
            paras = parts.get("paragraphs") or []
            if paras:
                paragraphs_by_agent[ag] = paras

            # bullets
            for b in parts.get("bullets", []):
                norm = normalize_bullet(b)
                if not norm:
                    continue
                if norm not in bullet_votes:
                    bullet_votes[norm] = set()
                    bullet_order.append(norm)
                    bullet_examples[norm] = b
                bullet_votes[norm].add(ag)
                # keep the longest exemplar for rendering
                if len(b) > len(bullet_examples[norm]):
                    bullet_examples[norm] = b

            # tables
            if parts.get("tables"):
                table_text = "\n".join(parts["tables"])
                table_items.append((ag, table_text))

            # flows
            if parts.get("flows"):
                flow_items.extend(parts["flows"])

            # enhancements
            if parts.get("enhancements"):
                for e in parts["enhancements"]:
                    enhancement_items.append((ag, e))

            # code
            if parts.get("code"):
                code_text = "\n".join(parts["code"])
                code_items.append((ag, code_text))

            # other
            if parts.get("other"):
                other_items.extend(parts["other"])

        # ---- 2.1 Paragraphs: consensus + info-gain supplements ----
        final_paragraphs: List[str] = []
        candidate_agents = [a for a in order_agents if a in paragraphs_by_agent]

        if candidate_agents:
            texts = {a: " ".join(paragraphs_by_agent[a]).strip() for a in candidate_agents}
            best_agent = candidate_agents[0]
            best_score = -1.0
            for a in candidate_agents:
                score = 0.0
                for b in candidate_agents:
                    if a == b:
                        continue
                    score += _sim(texts[a], texts[b])
                if score > best_score + 1e-6 or (
                    abs(score - best_score) <= 1e-6
                    and order_agents.index(a) < order_agents.index(best_agent)
                ):
                    best_agent = a
                    best_score = score

            base_paras = paragraphs_by_agent.get(best_agent, [])
            final_paragraphs.extend(base_paras)
            base_text_blob = " ".join(base_paras)

            # 信息增益补充：从其他 Agent 中抽取包含数字/实体且不重复的句子
            max_supplements = 3
            for ag in candidate_agents:
                if ag == best_agent or max_supplements <= 0:
                    continue
                other_text = " ".join(paragraphs_by_agent[ag])
                sentences = re.split(r'(?<=[。！？!?])\s+|\n+', other_text)
                for sent in sentences:
                    sent = sent.strip()
                    if len(sent) < 10:
                        continue
                    # 信号：数字、百分号或引号，粗略代表“细节信息/定义”
                    has_valuable_info = bool(re.search(r"\d", sent)) or "%" in sent or "％" in sent or "“" in sent or '"' in sent
                    if not has_valuable_info:
                        continue
                    sim = _sim(sent, base_text_blob)
                    if sim < 0.75:
                        final_paragraphs.append(f"补充说明（{ag}）：{sent}")
                        base_text_blob += " " + sent
                        max_supplements -= 1
                        if max_supplements <= 0:
                            break

        elif rep_agent:
            final_paragraphs = paragraphs_by_agent.get(rep_agent, []) or []

        agg_parts["paragraphs"] = final_paragraphs

        # ---- 2.2 Bullets: semantic clustering + consensus voting ----
        bullet_clusters: List[Dict[str, Any]] = []
        for norm in bullet_order:
            voters = bullet_votes.get(norm, set())
            body = bullet_examples.get(norm, norm)
            merged = False
            for cluster in bullet_clusters:
                if _sim(norm, cluster["representative"]) > 0.75:
                    cluster["items"].append(body)
                    cluster["agents"].update(voters)
                    if len(body) > len(cluster["representative"]):
                        cluster["representative"] = body
                    merged = True
                    break
            if not merged:
                bullet_clusters.append(
                    {
                        "items": [body],
                        "agents": set(voters),
                        "representative": body,
                    }
                )

        selected_bodies: List[str] = []
        # 定义哪些 Agent 的列表项即使只有 1 票也保留 (通常是负责枚举的 Agent)
        # 这里假设 Agent 名称包含 "doubao" (忽略大小写)
        coverage_agents = {a for a in order_agents if "doubao" in a.lower()}

        for cluster in bullet_clusters:
            # 保留条件：
            # 1. 至少 2 个 Agent 提到 (共识)
            # 2. 或者，这是主 Agent (rep_agent) 提到的 (结构锚点)
            # 3. 或者，这是覆盖型 Agent (Doubao) 提到的 (枚举补充)
            is_consensus = len(cluster["agents"]) >= 2
            is_anchor = rep_agent and (rep_agent in cluster["agents"])
            is_coverage_supplement = bool(cluster["agents"].intersection(coverage_agents))

            if is_consensus or is_anchor or is_coverage_supplement:
                selected_bodies.append(cluster["representative"])

        agg_parts["bullets"] = [
            b if re.match(r"^\s*[-\*+] ", b) else f"- {normalize_bullet(b)}"
            for b in dedupe_preserve_order(selected_bodies)
        ]

        # ---- 2.3 Tables: header-based row merging ----
        if table_items:
            agg_parts["tables"] = _merge_tables_content(table_items, order_agents)

        # ---- 2.4 Code: score & dedup ----
        if code_items:
            agg_parts["code"] = _select_best_code(code_items)

        # ---- 2.5 Flows ----
        if flow_items:
            agg_parts["flows"] = dedupe_flows(flow_items)

        # ---- 2.6 Enhancements: type-specific consensus ----
        priority_order = ["辨析", "示例", "原句引用", "类比", "口诀", "自测", "注意"]
        allowed_types = set(priority_order)
        SIMILARITY_THRESHOLDS = {
            "示例": 0.6,
            "辨析": 0.8,
            "原句引用": 0.95,
            "类比": 0.7,
            "口诀": 0.9,
            "自测": 0.75,
            "注意": 0.75,
        }

        clusters: Dict[str, Dict[str, Any]] = {}
        for ag, e in enhancement_items:
            m = ENHANCE_RE.match(e)
            if not m:
                continue
            t = m.group("type")
            if t not in allowed_types:
                continue
            body = e[m.end() :].strip()

            best_cluster_key: Optional[str] = None
            best_similarity = 0.0
            for existing_key in clusters.keys():
                if not existing_key.startswith(f"{t}:"):
                    continue
                existing_body = clusters[existing_key]["representative_body"]
                sim = _sim(body, existing_body)
                if sim > best_similarity:
                    best_similarity = sim
                    best_cluster_key = existing_key

            threshold = SIMILARITY_THRESHOLDS.get(t, 0.7)
            if best_cluster_key is not None and best_similarity > threshold:
                key_to_use = best_cluster_key
            else:
                key_to_use = f"{t}:{normalize_title(body)}"
                if key_to_use not in clusters:
                    clusters[key_to_use] = {
                        "type": t,
                        "agents": set(),
                        "lines": [],
                        "representative_body": body,
                    }

            clusters[key_to_use]["agents"].add(ag)
            clusters[key_to_use]["lines"].append(e)
            if len(body) > len(clusters[key_to_use]["representative_body"]):
                clusters[key_to_use]["representative_body"] = body

        kept_by_type: Dict[str, List[str]] = {}
        for key2, data in clusters.items():
            t = data["type"]
            is_formula_demo = t == "示例" and any(
                _is_formula_substitution_example(line) for line in data["lines"]
            )
            if len(data["agents"]) >= 2 or is_formula_demo:
                best_line = sorted(data["lines"], key=lambda x: -len(x))[0]
                kept_by_type.setdefault(t, []).append(best_line)

        filtered_enh: List[str] = []
        for ptype in priority_order:
            if ptype in kept_by_type and len(filtered_enh) < 4:
                items_sorted = sorted(kept_by_type[ptype], key=lambda x: -len(x))
                filtered_enh.extend(items_sorted[:2])
            if len(filtered_enh) >= 4:
                break

        agg_parts["enhancements"] = filtered_enh

        # keep any other lines as-is at the end of the section
        agg_parts["other"] = other_items

        # Stitch back the section
        section_lines = stitch_markdown(final_title, agg_parts, final_level)

        out_lines.extend(section_lines)
        out_lines.append("")

    return out_lines


def merge_files(agent_files: List[str], agent_names: List[str], primary_order: List[str]) -> str:
    merge_start = time.time()
    with _PRINT_LOCK:
        print(f"[timer] Parsing {len(agent_files)} agent outputs...")
    
    # parse structures
    parse_start = time.time()
    secs_by_agent: Dict[str, Dict[str, Any]] = {}
    for path, name in zip(agent_files, agent_names):
        txt = safe_read_text(path)
        secs_by_agent[name] = parse_markdown_structure(txt)
    parse_elapsed = time.time() - parse_start
    with _PRINT_LOCK:
        print(f"[timer] Parsing completed in {parse_elapsed:.2f}s")

    with _PRINT_LOCK:
        print(f"[timer] Merging sections...")
    section_start = time.time()
    
    # Pass primary_order directly for priority
    lines = _merge_sections(secs_by_agent, primary_order)
    
    section_elapsed = time.time() - section_start
    with _PRINT_LOCK:
        print(f"[timer] Section merging completed in {section_elapsed:.2f}s")
    
    # post: ensure single trailing newline
    while lines and not lines[-1].strip():
        lines.pop()
    
    merge_elapsed = time.time() - merge_start
    with _PRINT_LOCK:
        print(f"[timer] ⏱️  Total merge processing time: {merge_elapsed:.2f}s")
    return "\n".join(lines) + "\n"
