# 阶段 3：生成详细版（完整步骤）

在**仓库根**运行。macOS/Linux 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`、`\` 换成 `/`、`` ` `` 续行换成 `\`。

## 1. 跑流水线

```powershell
.venv\Scripts\python.exe -m multi_agent.chapter_pipeline `
  --ids <id> --auto-chunk --max-workers 6 --chapter-max-workers 7 `
  --resume-partial --skip-merge-unchanged `
  --limit-deepseek 15 --limit-doubao 15 --limit-merge 8 `
  --retries 2 --retry-backoff 10
```

```bash
.venv/bin/python -m multi_agent.chapter_pipeline \
  --ids 2552 --auto-chunk --max-workers 6 --chapter-max-workers 7 \
  --resume-partial --skip-merge-unchanged \
  --limit-deepseek 15 --limit-doubao 15 --limit-merge 8 \
  --retries 2 --retry-backoff 10
```

- 产物：`exports\multi_agent\final\书籍库\<id>-1 <书名>.md`。
- **旧仓库报 `unrecognized arguments: --limit-merge`**：先跑 `scripts/install.py` 从包里补齐流水线（见 [install.md](install.md)），或临时删掉该参数。
- **默认不加 `--force`**；只有 organized 内容变更、且用户确认要消耗全书 API 时才加。
- 校验：`## 第X章` 数与 `book.json.chapters`/`000-文件索引.md` 一致；标题层级 `## 章` / `### 节`；无空章、无错序。

## 1b. 并发与限流怎么设（`--limit-*`）

| 参数 | 闸住什么 | 默认 | 备注 |
|---|---|---|---|
| `--chapter-max-workers` | 同时在跑的章数 | 7（`--max-workers` 兜底） | 每章内部固定 2 路：`ThreadPoolExecutor(max_workers=2)` 跑 DeepSeek + 豆包 |
| `--limit-deepseek` | DeepSeek 生成许可 | 3 | 跨章共享；失败重试 `--retries` 次（默认 2），退避 10s→20s→40s…封顶 60s |
| `--limit-doubao` | 豆包生成许可 | 3 | 同上 |
| `--limit-merge` | **专用**合并许可 | 2 | 拿不到时等 `MERGE_BORROW_TIMEOUT_SECONDS`（默认 1s）后**借用 DeepSeek 生成槽**，所以实际 merge 并发 ≈ `limit-merge + 在飞的 deepseek 数`，不是硬上限 2 |
| `--limit-kimi` | （已失效） | 2 | 调用点写死 `enable_kimi=False`，传了也不会调 Kimi，别再写 |

- 单进程在飞的请求上限 ≈ `min(chapter-max-workers × 2, 各信号量可给之和)`。全书最先卡住的通常是 **merge**：它要吞「整章豆包 + DeepSeek」两份全文，所以标准命令把 `--limit-merge` 提到 8。
- 章的大小差异很大（119 章实测：中位 19 KB≈6K token，均值 28 KB，最大 179 KB≈59K token）；179 KB 那种章单次 merge 可能几十秒，属正常。
- 快档（标准命令）：`--chapter-max-workers 7 --limit-deepseek 15 --limit-doubao 15 --limit-merge 8`。稳档（怕限流/想降并发）：`--chapter-max-workers 3 --limit-deepseek 5 --limit-doubao 5 --limit-merge 3`。
- 对应环境变量：`DEEPSEEK_MAX_CONCURRENCY` / `DOUBAO_MAX_CONCURRENCY` / `DEEPSEEK_MERGE_MAX_CONCURRENCY` / `MERGE_BORROW_TIMEOUT_SECONDS`；命令行参数优先。

## 2. 清理与补齐（merge 经常出问题，必须做）

```powershell
# 1) 去重复章、压平跑偏的 ## 小节
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\clean_detail.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" `
  --book-json "<书目录>\book.json" --apply

# 2) merge 丢了后面的节时，从豆包产物补齐
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\supplement_detail.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" `
  --organized-dir "exports\Organized_Books\<id>-<书名>_organized" `
  --book-json "<书目录>\book.json" --apply
```

```bash
.venv/bin/python .claude/skills/book-to-question/scripts/clean_detail.py \
  --file "exports/multi_agent/final/书籍库/2552-1 市政工程识图与构造（第三版）.md" \
  --book-json "拆书/2552-市政工程识图与构造（第三版）/book.json" --apply

.venv/bin/python .claude/skills/book-to-question/scripts/supplement_detail.py \
  --file "exports/multi_agent/final/书籍库/2552-1 市政工程识图与构造（第三版）.md" \
  --organized-dir "exports/Organized_Books/2552-市政工程识图与构造（第三版）_organized" \
  --book-json "拆书/2552-市政工程识图与构造（第三版）/book.json" --apply
```

- 常见症状：同一章出现两次；`## 8.2` / `## # 8.1` 这类小节跑成 `##`；后面的节（如 8.7-8.9）整段消失。
- 补齐顺序：先 `clean_detail.py`，再 `supplement_detail.py`；两个都先跑 dry-run 看清单。
- 补齐来源优先看 `*.doubao_from_source.md`，其次 `*.deepseek_from_source.md`；两者都没有内容时才对照原书。

## 3. 公式渲染检查（清理补齐之后必做）

`chapter_pipeline` 在最终拼接时已自动把 `\(...\)` 归一成 `$...$`、`\[...\]` 归一成独立 `$$` 块。存量详细版先跑一次归一化（默认 dry-run，`--apply` 写盘并备份）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\fix_render.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" --apply
```

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\check_formulas.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md"
```

- ERROR（`$$` 未闭合、`$`/`\(`/`\[` 不配对、行内仍用 `\(...\)`、花括号或 `\begin/\end` 不平衡）必须先修，修好前不进入阶段 4。
- WARN（行内 `$` 截断、`\tag` 残留、`$` 与 `\(` 混用、`$$` 与文字同行）列入人工抽查清单。
- 只读检查，不自动改文件；`--json` 输出结构化结果，`--stats` 只看汇总。

静态检查通过后，再用真实 KaTeX 逐条试渲染：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\check_katex.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md"
```

- 解析失败（缺参数、未定义命令、`%` 截断等）必须先修，修好前不进入阶段 4。
- 警告（数学环境里的中文、未知 Unicode 符号）列入抽查；`--strict` 把警告也判失败。
- `--dir` 查整个目录，`--json` 输出结构化结果。依赖 node + katex；找不到时脚本以 2 退出并提示，不影响主流程。
- 可确定修复的问题（根号、中文进数学环境、`${`、`℃`、`×`/`·`、乘号星号）先跑：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\fix_render.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" --apply
```

  默认 dry-run；`--apply` 前会备份成 `<文件名>.bak-<时间戳>`。除符号/根号/中文外，也会把被错误 `$` 拆开的集合式合并回一个公式（`$A={x$ | $x^2>0}$` → `$A=\{x|x^2>0\}$`）。合并有护栏：只在同一行、最多跨 3 段公式、间隔正文 ≤40 字、间隔不含 `* _ [ ] \` # < > ~`，且合并后必须像集合式（含 `|`、`,`、`∈` 等）；否则不动。需要完全关掉用 `--no-merge`。修完重跑静态检查与 KaTeX 复检。

- 规则修不掉的解析错误（缺参数、未定义命令等）走模型兜底：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\fix_render_llm.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" --apply
```

  逐条调用 DeepSeek（默认 `deepseek-v4-flash`，需 `DEEPSEEK_API_KEY`，最多 20 条）；每条修完重新用 KaTeX 验证，无法判断时输出 `__UNFIXABLE__` 并保留原样。明显是更长公式片段（如 `A={x`、`x>2}`）时脚本直接跳过、不调模型，这类通常是 `$` 位置错误，需要人工合并。

## 4. 上传语雀详细版

见 [yuque-upload.md](yuque-upload.md) 的「详细版」一节。
