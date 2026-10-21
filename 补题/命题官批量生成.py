#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
脚本二：命题官批量命题
对 拆分后/{数字id}/ 下每一份章节 md：
  1) 弹 tkinter 配置窗（知识点ID/书籍只读；各题型难度题量、配比、特殊要求可填）；
  2) 用本次配置替换 md/命题官提示词系统 V3.2.md 的【启动指令】tex 示例块；
  3) system=替换后整份文档 + user=该章节 md 全文 发给 deepseek-v4-flash；
  4) 返回的题目 JSON 数组存到 补题/题目/{id}{书籍名}/{章节标题}.json。

幂等：已存在的章节 json 默认跳过（弹窗可勾选「覆盖」全量重跑）；
中断后续跑只补缺失；失败章节自动重试 2 次并记入 _failed.txt。

用法：
    .venv/bin/python 补题/命题官批量生成.py            # 交互：输入 id + 书籍名
"""

import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import ROUND_HALF_UP, Decimal
from difflib import SequenceMatcher
from pathlib import Path

from openai import OpenAI

# ============================================================
# API 配置（环境变量优先，其次仓库根 .env；不把 key 写死在代码里）
# ============================================================
def _env_value(key):
    value = os.getenv(key, "").strip()
    if value:
        return value
    for p in (Path(__file__).resolve().parent.parent / ".env",
              Path(__file__).resolve().parent / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


AI_API_KEY = _env_value("DEEPSEEK_API_KEY")
AI_API_BASE = _env_value("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
AI_MODEL = _env_value("DEEPSEEK_MODEL") or "deepseek-v4-flash"
MAX_TOKENS = 65536
TEMPERATURE = 0.5
RETRIES = 2
CONCURRENCY_DEFAULT = 3          # 弹窗默认并发数，与 multi_agent 里 deepseek 的默认一致
DIALOG_FLAG = "--dialog"         # 本脚本以该参数启动时，只跑弹窗子进程
DIALOG_TIMEOUT = 1800            # 秒；弹窗是交互式的，正常几分钟内点完，超时即判为起不来
RUNS_DIRNAME = ".分次"            # 每轮原始结果，合并/查重靠它
DUP_THRESHOLD = 0.60             # 两轮之间题干相似度达到这个数就算重复

ROOT = Path(__file__).resolve().parent                     # 补题/
DIR_DETAIL = ROOT / "详细版"
DIR_SPLIT = ROOT / "拆分后"
DIR_OUT = ROOT / "题目"
PROMPT_FILE = ROOT.parent / "md" / "命题官提示词系统 V3.2.md"

START_MARK = "【命题官】，启动命题任务："

# (题型, 可选难度)  顺序/难度即模板顺序
TYPES = [
    ("单选题", ("简单", "中等", "困难")),
    ("多选题", ("简单", "中等", "困难")),
    ("判断题", ("简单", "中等", "困难")),
    ("组合题", ("简单", "中等", "困难")),
    ("填空题", ("简单", "中等", "困难")),
    ("简答题", ("简单", "中等", "困难")),
]
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# --------------------------------------------------------------------------
# 加载 拆分章节.py（脚本一并存的纯函数）
# --------------------------------------------------------------------------
def _load_split_module():
    mod_path = ROOT / "拆分章节.py"
    spec = importlib.util.spec_from_file_location("split_chapters", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SPLIT = _load_split_module()


def _sanitize_dir(name):
    name = _ILLEGAL.sub("", name).strip().rstrip(".")
    return name or "未命名"


# --------------------------------------------------------------------------
# 配置文本 / 文档替换
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"^第(\d+)章(?:-(.*))?$")


def build_tag(book, label):
    """标签 = 书名-第N章[-描述]。label 是拆分文件名（镜像格式，如 第5章-主配原料-水产品类）。

    书名只做横线统一（不碰其中的空格，免得「X 第二版」被改成「X-第二版」）。
    """
    book = _SPLIT.normalize_label(book, spaces=False) if book else book
    label = _SPLIT.chapter_stem(label) if label else label  # 幂等：破折号归一 + 单元N→第N章
    m = _TAG_RE.match(label.strip())
    if not m:
        return f"{book}-{label}" if book else label
    num, desc = f"第{m.group(1)}章", (m.group(2) or "").strip("-")
    if not book:
        return f"{num}-{desc}" if desc else num
    return f"{book}-{num}-{desc}" if desc else f"{book}-{num}"


def _fix_fields(obj, know_id=None, tag=None):
    """把脚本能确定性修正的字段一次修好。

        know_id    一律覆盖成启动指令里的知识点 ID（模型偶尔整批写错，如把 1992 写成 1010）
        source     把其中的横线类字符统一成 '-'；形状保持模型原样（含「(引申创作)」）
        difficulty 四舍五入到 1 位小数（模型偶尔给 0.35 这种）
        tags       一律覆盖成启动指令里的标签（模型偶尔不照抄，而是按正文标题的原书
                   章号造一个，如把「第3章-乳及乳制品检测」写成正文里的「第2章-乳及
                   乳制品检测」，落盘后会和并章撞号）；原值返回给调用方告警

    题目正文（title/parse/options）一律不碰——那里的破折号是合法中文标点。

    四舍五入用 Decimal 半进位，不用内置 round()：float 里 0.35 实际是 0.3499…，
    round(0.35, 1) 给 0.3，而 round(0.65, 1) 给 0.7，同一批里看着像 bug。

    返回 tags 与 tag 不符的原值（按出现顺序去重）。
    """
    bad_tags = []
    for q in obj:
        if not isinstance(q, dict):
            continue
        if know_id is not None:
            q["know_id"] = know_id
        if isinstance(q.get("source"), str):
            q["source"] = _SPLIT.normalize_label(q["source"], spaces=False)
        if tag and q.get("tags") != tag:
            if q.get("tags") not in bad_tags:
                bad_tags.append(q.get("tags") or "（缺失）")
            q["tags"] = tag
        d = q.get("difficulty")
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            q["difficulty"] = float(
                Decimal(str(d)).quantize(Decimal("0.1"), ROUND_HALF_UP))
    return bad_tags


def generate_chapter(client, doc, id_, book, f, outdir, cfg, out_json=None):
    """生成单章题目并落盘。返回 (ok, 题数, 错误信息)；内部不抛异常，可安全用于线程池。"""
    out_json = out_json or (outdir / f"{f.stem}.json")
    content, err = None, ""
    try:
        tag = build_tag(book, f.stem)
        body = _SPLIT.read_text_any(f)
        config_text = build_config_text(id_, book, tag, cfg["counts"],
                                        cfg["ratio"], cfg["special"])
        system = replace_doc(doc, config_text)

        for attempt in range(RETRIES + 1):
            try:
                resp = client.chat.completions.create(
                    model=AI_MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": body},
                    ],
                    temperature=TEMPERATURE,
                    max_tokens=MAX_TOKENS,
                )
                content = resp.choices[0].message.content
                obj = _to_json_array(content)
                if obj is not None:
                    bad = _fix_fields(obj, int(id_), tag)
                    out_json.write_text(
                        json.dumps(obj, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    if bad:
                        print(f"    ⚠ [{f.stem}] tags 与配置不符，已按配置覆盖："
                              f"{'、'.join(bad)}  →  {tag}")
                    return True, len(obj), ""
                err = "返回内容不是合法 JSON 数组"
            except Exception as e:  # 网络/限流等
                err = f"{type(e).__name__}: {e}"
            if attempt < RETRIES:
                print(f"    … [{f.stem}] 第 {attempt + 1} 次失败（{err}），重试中")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    # 重试耗尽 / 意外异常：原文留档，交给主线程汇总
    try:
        out_json.write_text(content or "", encoding="utf-8")
    except OSError as e:
        err = f"{err}（原文也写不进去：{e}）"
    return False, 0, err


def _chapter_sort_key(stem):
    """章节名排序键：第N章按 N 排，非章节名排最后（避免第10章排到第2章前）。"""
    m = re.match(r"^第(\d+)章", stem)
    return (int(m.group(1)), stem) if m else (9999, stem)


_DUP_STRIP = re.compile(r"[\s，。！？、；：“”‘’（）()【】\[\]《》<>·.,!?;:'\"-]+")


def _valid_json_array(path) -> bool:
    try:
        obj = json.loads(_SPLIT.read_text_any(path))
        return isinstance(obj, list)
    except (OSError, json.JSONDecodeError):
        return False


def _dedupe_questions(items):
    """按题干相似度去重，保留先出现的；返回 (保留列表, 去掉数量)。"""
    kept, titles, dropped = [], [], 0
    for q in items:
        if not isinstance(q, dict):
            continue
        t = _DUP_STRIP.sub("", str(q.get("title", "")))
        if t and any(SequenceMatcher(None, t, s).ratio() >= DUP_THRESHOLD for s in titles):
            dropped += 1
            continue
        kept.append(q)
        titles.append(t)
    return kept, dropped


# --------------------------------------------------------------------------
# 选项均衡：只调整选项顺序与答案字母，不改题干、选项文字和解析结论
# --------------------------------------------------------------------------
_LETTERS = "ABCDEFGH"
_EXPLICIT_OPTION = re.compile(r"(?:以上|上述).{0,4}(?:都|均)|[A-H]\s*(?:和|与|及)\s*[A-H]")
_BARE_LETTER = re.compile(r"(?<![A-Za-z])[A-H](?![A-Za-z])")
_PARSE_PATTERNS = [
    (re.compile(r"(本题选|故选|正确答案(?:是|为)?|答案)(\s*)(\*{0,2})([A-H]+)(\3)"), "answer"),
    (re.compile(r"(选项)(\s*)([A-H])"), "option"),
    (re.compile(r"([A-H])(\s*)(项)"), "item"),
    (re.compile(r"（([A-H])）"), "fullparen"),
    (re.compile(r"\(([A-H])\)"), "halfparen"),
    (re.compile(r"^(\s*[-*]?\s*)([A-H])(\s*[：:])", re.M), "list"),
    (re.compile(r"(?<![\w])([A-H])(\s*[、,，])"), "pair"),
    (re.compile(r"(?<![\w])([A-H])(\s*(?:错误|正确|符合|不符合|为|是|不选|当选))"), "judge"),
]


def _sub_parse(pattern, kind, text, mapping):
    def repl(m):
        if kind == "answer":
            old = m.group(4)
            if any(c not in mapping for c in old):
                return m.group(0)
            return f"{m.group(1)}{m.group(2)}{m.group(3)}" \
                   f"{''.join(mapping[c] for c in old)}{m.group(3)}"
        if kind == "option":
            return f"{m.group(1)}{m.group(2)}{mapping.get(m.group(3), m.group(3))}"
        if kind == "item":
            return f"{mapping.get(m.group(1), m.group(1))}{m.group(2)}{m.group(3)}"
        if kind == "fullparen":
            return f"（{mapping.get(m.group(1), m.group(1))}）"
        if kind == "halfparen":
            return f"({mapping.get(m.group(1), m.group(1))})"
        if kind == "list":
            return f"{m.group(1)}{mapping.get(m.group(2), m.group(2))}{m.group(3)}"
        if kind == "pair":
            return f"{mapping.get(m.group(1), m.group(1))}{m.group(2)}"
        return f"{mapping.get(m.group(1), m.group(1))}{m.group(2)}"

    return pattern.sub(repl, text)


def _remap_parse(parse, mapping):
    """按白名单句式重映射字母；有无法识别的裸字母引用时返回 None。"""
    if not parse:
        return parse
    out = parse
    for pattern, kind in _PARSE_PATTERNS:
        out = _sub_parse(pattern, kind, out, mapping)
    residue = out
    for pattern, _kind in _PARSE_PATTERNS:
        residue = pattern.sub("", residue)
    if _BARE_LETTER.search(residue):
        return None
    return out


def _option_info(q):
    opts = q.get("options")
    if not isinstance(opts, list) or len(opts) < 2:
        return None
    if any(not isinstance(o, dict) for o in opts):
        return None
    keys = [str(o.get("key") or "").strip() for o in opts]
    if len(set(keys)) != len(keys) or any(len(k) != 1 or k not in _LETTERS for k in keys):
        return None
    answer = str(q.get("answer") or "").strip()
    if not answer or any(c not in keys for c in answer) or len(set(answer)) != len(answer):
        return None
    names = [str(o.get("name") or "") for o in opts]
    if len(set(names)) != len(names) or any(_EXPLICIT_OPTION.search(n) for n in names):
        return None
    return opts, keys, answer


def _iter_option_questions(items):
    for q in items:
        if not isinstance(q, dict):
            continue
        if q.get("type") in ("choice", "multiple"):
            yield q
        elif q.get("type") == "group":
            for sub in q.get("subs") or []:
                if isinstance(sub, dict) and sub.get("type") in ("choice", "multiple"):
                    yield sub


def _reorder_choice(opts, answer, target):
    correct = next(o for o in opts if str(o.get("key")) == answer)
    wrong = [o for o in opts if str(o.get("key")) != answer]
    pos = _LETTERS.index(target)
    ordered = wrong[:pos] + [correct] + wrong[pos:]
    keys = [_LETTERS[i] for i in range(len(ordered))]
    mapping = {str(old.get("key")): new for new, old in zip(keys, ordered)}
    new_opts = []
    for new, old in zip(keys, ordered):
        item = dict(old)
        item["key"] = new
        new_opts.append(item)
    return new_opts, target, mapping


def _reorder_multiple(opts, answer, seed):
    def sort_key(o):
        raw = f"{seed}|{o.get('name')}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    ordered = sorted(opts, key=sort_key)
    keys = [_LETTERS[i] for i in range(len(ordered))]
    mapping = {str(old.get("key")): new for new, old in zip(keys, ordered)}
    new_opts = []
    for new, old in zip(keys, ordered):
        item = dict(old)
        item["key"] = new
        new_opts.append(item)
    new_answer = "".join(sorted(mapping[c] for c in answer))
    return new_opts, new_answer, mapping


def _assign_choice_targets(candidates):
    quota = {k: 0 for k in _LETTERS}
    targets = []
    for _q, (_opts, keys, _answer) in candidates:
        target = min(keys, key=lambda k: (quota[k], k))
        quota[target] += 1
        targets.append(target)
    return targets


def balance_chapter(items, seed_prefix):
    """确定性均衡本章选项。返回统计；无法安全处理的题原样保留。"""
    stats = {
        "choice": 0, "multiple": 0, "already": 0, "skipped": 0,
        "reasons": {}, "before": {k: 0 for k in _LETTERS}, "after": {k: 0 for k in _LETTERS},
    }

    def skip(reason):
        stats["skipped"] += 1
        stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1

    choice_candidates = []
    multiple_candidates = []
    for q in _iter_option_questions(items):
        info = _option_info(q)
        if info is None:
            skip("选项结构或文本不可安全重排")
            continue
        _opts, keys, answer = info
        if _remap_parse(str(q.get("parse") or ""), {k: k for k in keys}) is None:
            skip("解析含无法安全映射的字母引用")
            continue
        if q.get("type") == "choice":
            if len(answer) != 1:
                skip("单选答案不是单个字母")
                continue
            choice_candidates.append((q, info))
        else:
            multiple_candidates.append((q, info))

    for q, (_opts, _keys, answer) in choice_candidates:
        stats["before"][answer] += 1

    targets = _assign_choice_targets(choice_candidates)
    for (q, (opts, _keys, answer)), target in zip(choice_candidates, targets):
        original = copy.deepcopy(q)
        if answer == target:
            stats["already"] += 1
            stats["after"][answer] += 1
            continue
        new_opts, new_answer, mapping = _reorder_choice(opts, answer, target)
        new_parse = _remap_parse(str(q.get("parse") or ""), mapping)
        if new_parse is None:
            q.clear()
            q.update(original)
            skip("解析重映射失败")
            stats["after"][answer] += 1
            continue
        q["options"] = new_opts
        q["answer"] = new_answer
        q["parse"] = new_parse
        stats["choice"] += 1
        stats["after"][new_answer] += 1

    for q, (opts, _keys, answer) in multiple_candidates:
        stats["before"][answer[0]] += 1
        original = copy.deepcopy(q)
        new_opts, new_answer, mapping = _reorder_multiple(opts, answer, seed_prefix)
        if new_answer == answer and [o.get("key") for o in new_opts] == [o.get("key") for o in opts]:
            stats["already"] += 1
            stats["after"][answer[0]] += 1
            continue
        new_parse = _remap_parse(str(q.get("parse") or ""), mapping)
        if new_parse is None:
            q.clear()
            q.update(original)
            skip("解析重映射失败")
            stats["after"][answer[0]] += 1
            continue
        q["options"] = new_opts
        q["answer"] = new_answer
        q["parse"] = new_parse
        stats["multiple"] += 1
        stats["after"][new_answer[0]] += 1

    return stats


def format_balance_stats(stats):
    letters = "".join(
        k for k in _LETTERS if stats["before"][k] or stats["after"][k]
    ) or "ABCD"
    before = "/".join(str(stats["before"][k]) for k in letters)
    after = "/".join(str(stats["after"][k]) for k in letters)
    parts = [f"单选 {stats['choice']}", f"多选 {stats['multiple']}"]
    if stats["already"]:
        parts.append(f"已均衡 {stats['already']}")
    if stats["skipped"]:
        reasons = "、".join(f"{k} {v}" for k, v in stats["reasons"].items())
        parts.append(f"跳过 {stats['skipped']}（{reasons}）")
    parts.append(f"A/B/C/D {before} → {after}")
    return "，".join(parts)


def audit_existing(outdir, id_):
    """对已有章节 JSON 只做内存试算，不写盘、不调 API。"""
    files = sorted(
        (p for p in outdir.glob("*.json") if p.name != "all.json"),
        key=lambda p: _chapter_sort_key(p.stem),
    )
    if not files:
        print(f"{outdir} 下没有章节 JSON")
        return
    for p in files:
        try:
            items = json.loads(_SPLIT.read_text_any(p))
        except (OSError, json.JSONDecodeError):
            print(f"{p.name}: 不是合法 JSON，跳过")
            continue
        if not isinstance(items, list):
            continue
        stats = balance_chapter(items, f"{id_}|{p.stem}")
        print(f"{p.stem}: {format_balance_stats(stats)}")


def build_config_text(id_, book, tag, counts, ratio, special):
    """按用户给定模板拼启动指令正文（不含围栏，末尾无换行）。"""
    lines = [START_MARK, f"- 知识点ID：{id_}", f"- 书籍：{book}", f"- 标签：{tag}"]
    lines.append("- 命题清单：")
    for name, diffs in TYPES:
        total = sum(counts[name].get(d, 0) for d in diffs)
        if total == 0:
            continue  # 0 道的不列出
        part = "，".join(f"{d}{counts[name].get(d, 0)}道" for d in diffs)
        if name in ("单选题", "多选题", "判断题"):
            lines.append(f"  - {name}：共 {total} 道  ({part})")
        else:
            lines.append(f"  - {name}：共 {total} 道 （{part}）")
    if ratio.strip():
        lines.append(f"- 题目形式配比：{ratio.strip()}")
    lines.append(f"- 特殊要求：{special}")
    return "\n".join(lines)


def replace_doc(doc, config_text):
    """把文档【启动指令】tex 示例块内的正文换成 config_text，围栏保留。"""
    start = doc.find(START_MARK)
    if start < 0:
        raise RuntimeError(f"提示词文档里找不到启动标记「{START_MARK}」，请检查 {PROMPT_FILE}")
    end = doc.find("\n```", start)  # 指向该示例块闭合围栏前的换行
    if end < 0:
        raise RuntimeError("提示词文档里找不到启动示例块的闭合围栏 ```")
    return doc[:start] + config_text + doc[end:]


def _strip_fence(text):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    return t


def _to_json_array(content):
    """尽量从模型文本里解出 JSON 数组；失败返回 None。"""
    text = _strip_fence(content)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else None
    except json.JSONDecodeError:
        pass
    # 兜底：取首个 '[' 到末个 ']' 再试
    i, j = text.find("["), text.rfind("]")
    if i >= 0 and j > i:
        try:
            obj = json.loads(text[i : j + 1])
            return obj if isinstance(obj, list) else None
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------
# tkinter 配置弹窗（跑在子进程里，见文件末尾的 --dialog 分派）
# --------------------------------------------------------------------------
def _run_dialog_gui(id_, book, out_path):
    """子进程入口：弹窗收集配置，点「开始命题」把 cfg 写成 JSON 到 out_path。

    本函数只在子进程里执行；子进程退出时由系统回收窗口，不依赖 Tk 关闭时序。
    取消/题量全 0 → 不写文件。
    """
    import tkinter as tk

    win = tk.Tk()
    win.title("【命题官】启动命题任务 · 配置")
    win.attributes("-topmost", True)

    # 顶部：只读的知识点ID / 书籍
    tk.Label(win, text="知识点ID：").grid(row=0, column=0, sticky="e", padx=4, pady=2)
    id_var = tk.StringVar(value=str(id_))
    tk.Entry(win, textvariable=id_var, state="readonly", width=20).grid(
        row=0, column=1, sticky="w")
    tk.Label(win, text="书籍：").grid(row=0, column=2, sticky="e", padx=4, pady=2)
    book_var = tk.StringVar(value=book)
    tk.Entry(win, textvariable=book_var, state="readonly", width=34).grid(
        row=0, column=3, sticky="w", columnspan=2)

    # 表头：题型 + 各难度
    tk.Label(win, text="题型").grid(row=1, column=0, padx=4, pady=(6, 2))
    cols = {}
    for c, dname in enumerate(["简单", "中等", "困难"], start=1):
        cols[dname] = c
        tk.Label(win, text=dname).grid(row=1, column=c, padx=2, pady=(6, 2))

    spin_vars = {}
    for r, (name, diffs) in enumerate(TYPES, start=2):
        tk.Label(win, text=name).grid(row=r, column=0, sticky="w", padx=4)
        for dname in diffs:
            v = tk.StringVar(value="0")
            spin_vars[(name, dname)] = v
            tk.Spinbox(win, from_=0, to=9999, width=5, textvariable=v).grid(
                row=r, column=cols[dname], padx=2, pady=1)

    # 题目形式配比（预填，可清空 → 省略该行）
    tk.Label(win, text="题目形式配比：").grid(row=2 + len(TYPES), column=0,
                                            sticky="e", padx=4, pady=(8, 2))
    ratio_var = tk.StringVar(
        value="基础题约60%，综合题30% 情景题约10%（可选，不填则按默认配比）")
    tk.Entry(win, textvariable=ratio_var, width=72).grid(
        row=2 + len(TYPES), column=1, columnspan=5, sticky="w", pady=(8, 2))

    # 特殊要求（多行）
    tk.Label(win, text="特殊要求：").grid(row=3 + len(TYPES), column=0,
                                         sticky="ne", padx=4, pady=2)
    special_txt = tk.Text(win, width=60, height=4)
    special_txt.grid(row=3 + len(TYPES), column=1, columnspan=5,
                     sticky="w", pady=2)

    overwrite_var = tk.BooleanVar(value=False)
    tk.Checkbutton(win, text="覆盖已存在的章节结果（默认跳过已有 json）",
                   variable=overwrite_var).grid(
        row=4 + len(TYPES), column=1, columnspan=5, sticky="w")

    tk.Label(win, text="并发数：").grid(row=5 + len(TYPES), column=0,
                                      sticky="e", padx=4, pady=(4, 2))
    conc_var = tk.StringVar(value=str(CONCURRENCY_DEFAULT))
    tk.Spinbox(win, from_=1, to=8, width=5, textvariable=conc_var).grid(
        row=5 + len(TYPES), column=1, sticky="w", pady=(4, 2))

    def on_submit():
        counts, total_all, data = {}, 0, None
        try:
            # 先在关窗前读完所有控件值（destroy 后 Text 就读不到了）
            for name, diffs in TYPES:
                row = {}
                for dname in diffs:
                    try:
                        val = int(spin_vars[(name, dname)].get())
                    except ValueError:
                        val = 0
                    row[dname] = max(0, val)
                counts[name] = row
                total_all += sum(row.values())
            try:
                conc = max(1, min(8, int(conc_var.get())))
            except ValueError:
                conc = CONCURRENCY_DEFAULT
            if total_all > 0:
                data = {
                    "counts": counts,
                    "ratio": ratio_var.get().strip(),
                    "special": special_txt.get("1.0", "end").strip(),
                    "overwrite": overwrite_var.get(),
                    "concurrency": conc,
                }
        finally:
            win.destroy()
        if data is None:
            print("题量全为 0，本次未生成任何题目。")
        else:
            Path(out_path).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def on_cancel():
        win.destroy()

    def on_return(_event):
        # 焦点在「特殊要求」文本框时要留给回车换行，不抢
        if isinstance(win.focus_get(), tk.Text):
            return
        on_submit()

    tk.Button(win, text="取消", command=on_cancel).grid(
        row=6 + len(TYPES), column=4, pady=10)
    tk.Button(win, text="开始命题", command=on_submit).grid(
        row=6 + len(TYPES), column=5, pady=10)
    win.protocol("WM_DELETE_WINDOW", on_cancel)   # 红叉=取消，别留个没人管的窗口
    win.bind("<Return>", on_return)

    win.mainloop()


def open_config_dialog(id_, book):
    """父进程侧：起子进程弹窗，读回配置；取消/题量为 0/子进程异常 → None。

    子进程退出即窗口消失（系统回收），主进程全程不初始化 Tk。
    """
    import subprocess
    import tempfile

    fd, out_path = tempfile.mkstemp(prefix="mtdlg_", suffix=".json")
    os.close(fd)
    try:
        print("正在打开配置弹窗…")
        try:
            cp = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 DIALOG_FLAG, str(id_), book, out_path],
                timeout=DIALOG_TIMEOUT)
        except subprocess.TimeoutExpired:
            waited = (f"{DIALOG_TIMEOUT // 60} 分钟" if DIALOG_TIMEOUT >= 60
                      else f"{DIALOG_TIMEOUT} 秒")
            print(f"弹窗子进程 {waited}没反应，已放弃"
                  f"（多半是没有图形界面，窗口没能显示出来）。")
            return None
        if cp.returncode != 0:
            print(f"弹窗子进程异常退出（返回码 {cp.returncode}），"
                  f"可能没有图形环境，看上面的报错。")
            return None
        try:
            data = json.loads(_SPLIT.read_text_any(out_path))
        except (OSError, json.JSONDecodeError, ValueError):
            return None  # 取消 或 题量为 0
        if not data:
            return None
        try:
            data["concurrency"] = max(1, min(8, int(data.get("concurrency"))))
        except (TypeError, ValueError):
            data["concurrency"] = CONCURRENCY_DEFAULT
        return data
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _pick_id(cli_id=None):
    ids = _SPLIT.list_ids()
    if not ids:
        print("【命题官】 详细版/ 下没有数字命名的 .md 文件。")
        return None
    if cli_id:
        if cli_id in ids:
            return cli_id
        print(f"详细版/ 下找不到 id {cli_id}，可选：{ids}")
        return None
    if len(sys.argv) > 1 and sys.argv[1].strip().isdigit() and sys.argv[1].strip() in ids:
        return sys.argv[1].strip()
    print("可用知识点（输入数字 id）：")
    for i in ids:
        src = _SPLIT.find_source_md(i)
        kb = src.stat().st_size // 1024 if src else 0
        print(f"  {i}  {_SPLIT.book_of(i) or '（未带书名）'}  ({kb}KB)")
    while True:
        raw = input("请输入数字 id（直接回车退出）：").strip()
        if not raw:
            return None
        if raw in ids:
            return raw
        print(f"没有 {raw}，可选：{ids}")


def _load_cli_config(path):
    """读取 --config JSON；结构同 tkinter 弹窗写出的 cfg。"""
    data = json.loads(_SPLIT.read_text_any(path))
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("配置缺少 counts")
    missing = [name for name, _ in TYPES if name not in counts]
    if missing:
        raise ValueError(f"配置缺少题型：{'、'.join(missing)}")
    for name, diffs in TYPES:
        row = counts.get(name)
        if not isinstance(row, dict):
            raise ValueError(f"counts.{name} 不是对象")
        clean = {}
        for d in diffs:
            v = row.get(d, 0)
            if not isinstance(v, int) or v < 0:
                raise ValueError(f"counts.{name}.{d} 必须是非负整数")
            clean[d] = v
        counts[name] = clean
    total = sum(sum(row.values()) for row in counts.values())
    if total <= 0:
        raise ValueError("题量全为 0")
    return {
        "counts": counts,
        "ratio": str(data.get("ratio", "")).strip(),
        "special": str(data.get("special", "")).strip(),
        "overwrite": bool(data.get("overwrite", False)),
        "concurrency": max(1, min(8, int(data.get("concurrency", CONCURRENCY_DEFAULT)))),
        "rounds": max(1, int(data.get("rounds", 1))),
        "balance_options": bool(data.get("balance_options", True)),
    }


def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description="命题官批量生成")
    p.add_argument("id", nargs="?", help="知识点 id（省略则交互选择）")
    p.add_argument("--config", help="非交互配置 JSON；给了就跳过 tkinter 弹窗")
    p.add_argument("--concurrency", type=int, help="覆盖配置里的并发数")
    p.add_argument("--rounds", type=int, help="每章跑几轮（配置里有 rounds，这里可覆盖）")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的章节 JSON")
    p.add_argument("--resplit", action="store_true",
                   help="按当前详细版覆盖重拆章节，并备份旧的 .分次 结果")
    p.add_argument("--no-balance", action="store_true", help="不重排选项、不均衡答案字母")
    p.add_argument("--audit", action="store_true",
                   help="只对已有章节 JSON 试算均衡结果并打印分布，不写盘、不调 API")
    return p.parse_args()


def main():
    args = _parse_args()
    if not AI_API_KEY and not args.audit:
        print("未配置 DEEPSEEK_API_KEY（环境变量或仓库根 .env），退出。")
        return
    id_ = _pick_id(args.id.strip() if args.id else None)
    if id_ is None:
        return

    # 书名来自 详细版/{id}{书名}.md，不用手输；文件名只有数字时才回退问一次
    try:
        src = _SPLIT.find_source_md(id_)
    except ValueError as e:
        print(e)
        return
    if src is None:
        print(f"找不到 详细版/{id_}*.md")
        return
    stem = _SPLIT.source_stem(id_)
    book = _SPLIT.book_of(id_)
    if book:
        print(f"书籍：{book}（取自 {src.name}）")
    else:
        while not book:
            book = input("文件名里没有书名，请补一个书籍名：").strip()

    # 拆分目录缺章节时自动先拆（复用脚本一）
    split_dir = DIR_SPLIT / stem
    def _chapters():
        return [p for p in split_dir.glob("*.md") if p.stem != "00_文件头"]

    have = _chapters() if split_dir.exists() else []
    if not have:
        print(f"拆分后/{stem}/ 为空，先按 ## 拆分…")
        _, files = _SPLIT.ensure_split(id_)
        have = [p for p in (files or _chapters()) if p.stem != "00_文件头"]
    if not have:
        print("没有可命题的章节文件，退出。")
        return

    if args.config:
        try:
            cfg = _load_cli_config(args.config)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"读取 --config 失败：{e}")
            return
        if args.concurrency:
            cfg["concurrency"] = max(1, min(8, args.concurrency))
        if args.rounds:
            cfg["rounds"] = max(1, args.rounds)
        if args.overwrite:
            cfg["overwrite"] = True
        total = sum(sum(row.values()) for row in cfg["counts"].values())
        print(f"使用配置文件：{args.config}（每轮 {total} 题 × {cfg.get('rounds', 1)} 轮，并发 {cfg['concurrency']}）")
    else:
        cfg = open_config_dialog(id_, book)
        if cfg is None:
            print("未生成任何题目，退出。")
            return
        cfg.setdefault("rounds", 1)

    rounds = max(1, int(cfg.get("rounds", 1)))
    if args.no_balance:
        cfg["balance_options"] = False
    doc = _SPLIT.read_text_any(PROMPT_FILE)
    outdir = DIR_OUT / _sanitize_dir(stem)
    runsdir = outdir / RUNS_DIRNAME
    outdir.mkdir(parents=True, exist_ok=True)
    runsdir.mkdir(parents=True, exist_ok=True)

    if args.audit:
        audit_existing(outdir, id_)
        return

    if args.resplit:
        print(f"按 --resplit 重新拆分 {stem}…")
        _SPLIT.ensure_split(id_, overwrite=True)
        if runsdir.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            runsdir.rename(runsdir.with_name(f"{RUNS_DIRNAME}.bak-{stamp}"))
        runsdir.mkdir(parents=True, exist_ok=True)
        have = _chapters()
        if not have:
            print("重新拆分后没有章节文件，退出。")
            return

    client = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE)

    # 先按章节顺序挑出待生成的（每章 rounds 份，已存在的默认跳过）
    pending = []
    for f in sorted(have, key=lambda p: _chapter_sort_key(p.stem)):
        for run in range(1, rounds + 1):
            target = runsdir / f"{f.stem}.{run}.json"
            if target.exists() and not cfg["overwrite"] and _valid_json_array(target):
                print(f"skip  {f.name} 第{run}轮（已存在且合法）")
            else:
                if target.exists() and not cfg["overwrite"]:
                    print(f"redo  {f.name} 第{run}轮（已有文件不是合法 JSON）")
                pending.append((f, run, target))

    fails = []
    if pending:
        jobs = max(1, int(cfg.get("concurrency", CONCURRENCY_DEFAULT)))
        print(f"待生成 {len(pending)} 份（{len(have)} 章 × {rounds} 轮），并发 {jobs}")
        done = 0
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futures = {ex.submit(generate_chapter, client, doc, id_, book, f,
                                outdir, cfg, target): (f, run)
                       for f, run, target in pending}
            for fut in as_completed(futures):
                f, run = futures[fut]
                done += 1
                try:
                    ok, n, err = fut.result()
                except Exception as e:  # 兜底，不该发生
                    ok, n, err = False, 0, f"{type(e).__name__}: {e}"
                if ok:
                    print(f"[{done}/{len(pending)}] ok    {f.name} 第{run}轮 -> {n} 道题")
                else:
                    fails.append(f"{f.name} 第{run}轮\t{err}")
                    print(f"[{done}/{len(pending)}] FAIL {f.name} 第{run}轮 -> {err}")

    # 每章把各轮结果合并、按题干相似度去重
    all_rounds_ok = True
    for f in sorted(have, key=lambda p: _chapter_sort_key(p.stem)):
        items = []
        for run in range(1, rounds + 1):
            p = runsdir / f"{f.stem}.{run}.json"
            if not _valid_json_array(p):
                all_rounds_ok = False
                continue
            part = json.loads(_SPLIT.read_text_any(p))
            items.extend(part)
        out_ch = outdir / f"{f.stem}.json"
        if items:
            items, dropped = _dedupe_questions(items)
            if cfg.get("balance_options", True):
                stats = balance_chapter(items, f"{id_}|{f.stem}")
                print(f"均衡 {f.stem}: {format_balance_stats(stats)}")
            out_ch.write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"合并 {f.stem}: {len(items)} 题（去重 {dropped}）")
        else:
            out_ch.unlink(missing_ok=True)
            print(f"⚠ {f.stem}: 没有合法轮次结果，已移除旧 JSON")

    # 汇总：只汇总本轮章节文件，避免把 config/progress/备份 JSON 混进来
    have_stems = {f.stem for f in have}
    merged = []
    for p in sorted(outdir.glob("*.json"), key=lambda p: _chapter_sort_key(p.stem)):
        if p.name == "all.json" or p.stem not in have_stems:
            continue
        try:
            obj = json.loads(_SPLIT.read_text_any(p))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            merged.extend(obj)
    all_json = outdir / "all.json"
    if merged:
        all_json.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n汇总：{len(merged)} 道题 -> {all_json.name}")
    else:
        all_json.unlink(missing_ok=True)

    failed_txt = outdir / "_failed.txt"
    if fails or not all_rounds_ok:
        lines = set()
        if failed_txt.exists():
            lines.update(x for x in failed_txt.read_text(encoding="utf-8").splitlines() if x.strip())
        lines.update(fails)
        if not all_rounds_ok:
            lines.add("存在无效/缺失的 .分次 结果")
        failed_txt.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
        print(f"失败/无效 {len(lines)} 条，已记入：{failed_txt}")
    elif failed_txt.exists():
        failed_txt.unlink()
        print("全部成功，历史失败记录已清除。")
    print(f"输出目录：{outdir}")


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == DIALOG_FLAG:
        _run_dialog_gui(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        main()
