---
name: book-to-question
description: |
  拆书上题全流程：PDF 拆章 → 转 Markdown → 生成详细版 → 批量上题 →（可选）精简版，含语雀 token 上传。
  五个阶段可单独触发；默认出题配置为每章 60 题 × 2 轮（合并后约 120 题/章）。
  上题直接调用 补题/命题官批量生成.py（支持多轮生成 + 查重合并）。
  触发：拆书、拆书上题、书籍拆分、偏移量、转Markdown、详细版、上题、命题官、语雀上传、精简版、压缩版、精简笔记。
metadata:
  type: skill
  triggers:
    - 拆书
    - 拆书上题
    - 书籍拆分
    - 拆分 PDF
    - 偏移量
    - 转 Markdown
    - 详细版
    - 上题
    - 命题官
    - 命题官批量生成
    - 语雀上传
    - 精简版
    - 压缩版
    - 精简笔记
    - chapter_pipeline1
---

# book-to-question

把「拆书上题」的五段流程（精简版可选）固化下来，能脚本化的都用脚本；外部系统（9401、新教研后台、系统题库）只输出人工清单。

## 使用方式

- 拆书/拆分 PDF/从零开始 → 阶段 0→1；转 Markdown → 阶段 2（先确认阶段 1 产物）；详细版 → 阶段 3；上题/命题官 → 阶段 4；精简版/压缩版 → 阶段 5（需先有详细版）。
- 「全流程」= 0→1→2→3→4，到门禁处停下，上题后再问是否跑阶段 5。
- 开工前先读 `<书目录>/book.json`：有就复述阶段状态并问是否复用，没有就先做阶段 0。

## 环境与路径

- 仓库根 = 当前工作目录。**所有命令都在仓库根执行，用相对路径。**
- Python 固定用 venv：Windows `.venv\Scripts\python.exe`，macOS/Linux `.venv/bin/python`。不要用系统 python。
- 本 skill 在 `.claude\skills\book-to-question\`：脚本 `scripts\`，细节 `references\`。
- 示例默认 Windows（PowerShell，反引号续行）；macOS/Linux 换 `.venv/bin/python`、`/`、`\` 续行，`references/` 每篇都附了 bash 版。
- 命名别混：书目录/章节 PDF/语雀分组用 `<id>-<书名>`；补题正文/配置/题目用 `<id><书名>.md`（无连字符）；详细版/精简版用 `<id>-1 <书名>.md` / `<id>-2 <书名>.md`。organizer 用磁盘目录名当书名。
- 本仓库不含真实密钥，各自配：`cp config.example.json config.json` 填 MinerU token，`.env` 放 DeepSeek/豆包/七牛 key 和 `YUQUE_TOKEN`/`YUQUE_REPO`。本 skill 依赖本仓库的 `补题/`、`multi_agent/` 和提示词文件。

## 首次使用 / 给同事用

- **精简仓库**（根目录有 `bootstrap.py`）：`python bootstrap.py` 建 `.venv`、装依赖、生成配置模板。
- **主仓库**：`.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\bootstrap.py --check` 查 `补题/`、命题官脚本、提示词、依赖和密钥缺项。`题目数分配.py` 当前不需要。

## 硬门禁（必须停下来问用户）

1. `markdown_stage.py organize` 的目标 `<书目录名>_organized` 已存在。
2. 任一拆分结果 >40 页，或单个小节 >40 页。
3. `split --apply` 会覆盖同名 PDF。
4. `chapter_pipeline --force`。
5. 覆盖 `补题/详细版/` 已有文件、`--overwrite` 重跑题目。
6. `yuque_upload.py --apply`（先展示将创建/更新的分组与文档清单）。
7. 删除失败章节 JSON、`_failed.txt`。
8. 9401 后台上传、系统题库录入。

## 阶段 0：建书档案

每本书一次性问清：PDF 路径、书籍 id（纯数字）、书名、书目录。题量配比用阶段 4 的默认值，用户没另说就不用问。

写 `<书目录>/book.json`（`inspect` 会保留这些字段并补页数/偏移量）：

```json
{"id": "1957", "book": "逻辑思维训练教程", "yuque_repo": "",
 "question": {"per_chapter": 120, "batch": 60, "rounds": 2,
              "counts": {"单选题": {"简单": 5, "中等": 6, "困难": 4}},
              "ratio": "基础题约60%，综合题30%，情景题10%", "special": ""},
 "stages": {"split": "pending", "markdown": "pending", "detail": "pending",
            "questions": "pending", "compact": "pending"}}
```

**不要照抄上面这段 `counts`**：它只示意一类，直接拿去跑会缺五个题型。实际必须六类齐全，直接复制 `补题\配置\默认-60题2轮.json` 的完整结构，或见 [question-config.md](references/question-config.md)。`yuque_repo` 留空时读 `YUQUE_REPO`，也可用 `--repo` 覆盖。

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\pdf_tools.py inspect `
  --pdf "D:\...\原书.pdf" --out-json "D:\...\<id>-<书名>\book.json"
```

`book.json` 只记事实，不写任何密钥。

## 阶段 1：拆书

详见 [checklists.md](references/checklists.md)。

1. **查偏移量**：`inspect` 给总页数、标码连续性、候选偏移量；偏移量必须用户确认。扫描件没文本层时 `render --pages 1-20` 导 PNG 读图。
2. **读目录**：用 Read 的 `pages` 读目录页（或 render 的 PNG），生成 `<id>-<书名>-第N章-章节名`，最后一行为 PDF 页码范围（印刷页 + 偏移量，英文逗号分隔），写入 `<书目录>/titles.txt`。
3. **大章拆分（40 页规则）**：≤40 页保持 `...-第N章-章名`；>40 页按小节顺序贪心合并到每份 ≤40 页，用**新节号 + 汇总名**命名 `...-第N章-章名-第1节-<汇总名>`（汇总名概括合并进来的小节，不拼原节名）；单个小节 >40 页**停下问用户**；拆分计划先打印给用户确认。
4. **校验 + 拆分**：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\pdf_tools.py verify `
  --pdf "D:\...\原书.pdf" --titles-txt "D:\...\titles.txt"
```

   重叠/越界/超 40 页报错，内部空隙警告（封面/目录/附录允许）。确认后加 `--apply`，默认 dry-run。
5. **检查**：绪论/附录保留、章节数与目录一致、无遗漏。WPS 备选见 [manual-steps.md](references/manual-steps.md)。

## 阶段 2：转 Markdown

1. `markdown_stage.py convert --pdf-dir "D:\...\<id>-<书名>"`（首次先 `cp config.example.json config.json` 填 MinerU token）。
2. `markdown_stage.py organize --source "D:\...\<id>-<书名>" --backup-existing` —— **门禁：目标已存在必须先问**。产物 `exports\Organized_Books\<id>-<书名>_organized\`；上传前自动去书页二维码，参数在 `config.json` 的 `qiniu.remove_qrcode`。
3. `normalize_headings.py --dir "exports\Organized_Books\<id>-<书名>_organized" --apply` —— 每份只留一个 `##`，其余降到 `###/####/#####`；默认 dry-run，`--apply` 自动备份。
4. 按 [checklists.md](references/checklists.md) 检查后传语雀原书；漏开去码要补处理已上传的图，用 `repair_qrcode_cdn.py --md-dir <organized 目录>`（同 key 覆盖、URL 不变）。

命令全文见 **[markdown.md](references/markdown.md)**。

## 阶段 3：生成详细版

1. 跑流水线 → `exports\multi_agent\final\书籍库\<id>-1 <书名>.md`，标准参数：`-m multi_agent.chapter_pipeline --ids <id> --auto-chunk --max-workers 6 --chapter-max-workers 7 --resume-partial --skip-merge-unchanged --limit-deepseek 15 --limit-doubao 15 --limit-merge 8 --retries 2 --retry-backoff 10`。**默认不加 `--force`**，只有 organized 变更且用户确认要烧 API 时才加。`--limit-kimi` 已失效，别写；并发怎么设见 [detail.md](references/detail.md)「1b」。
2. 校验：`## 第X章` 数与 `book.json.chapters`/`000-文件索引.md` 一致；`## 章`/`### 节` 层级正确；无空章、无错序。
3. **清理与补齐（merge 常出问题，必须做）**：先 `clean_detail.py`（去重复章、压平跑偏的 `##` 小节），再 `supplement_detail.py`（merge 丢后面的节时从豆包产物补齐）；两个都先 dry-run 看清单。
4. **公式检查**：`check_formulas.py`，ERROR（`$$` 未闭合、`$`/`\(`/`\[` 不配对等）必须先修；再跑 `check_katex.py --file <详细版>` 做真实 KaTeX 渲染，解析失败同样必须先修，修好前不进阶段 4。警告（数学环境里的中文、`%`、`\tag` 等）列入人工抽查。
   - 可确定修复的渲染问题先跑 `fix_render.py --file <详细版> --apply`（默认 dry-run，写盘前备份）；它只改数学环境，不猜缺参数等解析错误。修完重跑 `check_formulas.py` 和 `check_katex.py`。
   - 仍有解析错误时跑 `fix_render_llm.py --file <详细版> --apply`：逐条调用 DeepSeek（需 `DEEPSEEK_API_KEY`，默认最多 20 条），每条修完必须重新通过 KaTeX；模型判断必须猜测的输出 `__UNFIXABLE__`，保留原样等人工处理。

完整命令与常见症状见 **[detail.md](references/detail.md)**。

## 阶段 4：上题（按轮次）

### 默认配置：用户没另说就用这套

每章 **60 题/轮 × 2 轮**，按题干相似度 ≥0.60 查重合并后**约 120 题/章**。每轮配比：单选 5/6/4、多选 4/6/5、判断 5/6/4、组合 2/3/2、填空 1/2/2、简答 1/1/1（简单/中等/困难）→ **18/24/18=60**，总体严格 3:4:3。完整表格见 [question-config.md](references/question-config.md)。

- 现成配置：`补题\配置\默认-60题2轮.json`（`rounds: 2`），直接 `--config` 走非交互，不必开 tkinter 弹窗。
- 只有用户明确要改题量/配比/轮数/特殊要求时才动，改完写回 `补题\配置\<id><书名>.json`。六类题型键**必须齐全**，`counts` 合计 = 每轮题量，`rounds` = 每章轮数。

### 步骤

1. 准备正文（详细版优先，缺失回退 `_full.md`）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\prep_questions.py `
  --id <id> --book "<书名>" --book-json "D:\...\<id>-<书名>\book.json" --dry-run
```

   确认来源、章节数后加 `--apply`。目标 `补题\详细版\<id><书名>.md`（不带 `-1 ` 前缀）。多个同 id 文件会停止，先消歧。回退拼接会把 `_full.md` 内部标题降两级；提示「节标题出现在 ## 层」时先修详细版。

2. 生成 `补题\配置\<id><书名>.json`，没特殊要求就照抄上表；结构见 [question-config.md](references/question-config.md)。
3. 运行（`overwrite=false`，轮数取配置里的 `rounds`）：

```powershell
.venv\Scripts\python.exe 补题\命题官批量生成.py <id> `
  --config "补题\配置\<id><书名>.json"
```

   - `rounds: 2` 时每章两批各自落盘到 `题目\<id><书名>\.分次\<章节>.N.json`，再按相似度 ≥0.60 去重合并；`--rounds N` 可临时覆盖。
   - 合并后默认均衡选项（单选答案在 A/B/C/D 间分配、多选组合打散、解析字母引用同步改写）；先看效果加 `--audit`（不写盘），关闭用 `--no-balance`。输出 `补题\题目\<id><书名>\<章节>.json` + `all.json`。
4. 校验：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\validate_questions.py `
  --id <id> --book "<书名>" --config "补题\配置\<id><书名>.json"
```

   非 JSON 文件 = 失败章节原文；`_failed.txt` 存在则本阶段不能标完成。
   - 填空题题干必须含**连续作位**（顶层填空与组合题子题同样适用）：统一为 16 个半角下划线
     `________________`，多段作位同样写法。缺作答位、长度不是 16、残留全角 `＿` 均为 error。
   - 以下不达标均为 error，阶段 4 不能标完成：难度只能取 `0.2/0.3/0.4/0.5/0.6/0.7/0.8/0.9`；每章与全书难度比例为 3:4:3；多选题答案为 2–5 项且每章/全书至少 25% 为两项；组合题根题干 ≥150 字且子题 2–5 个；每章题数与 `counts × rounds` 偏差 ≤20%；同章/跨章无完全相同或相似度 ≥0.90 的题；单选答案不出现某字母 >60% 或某字母完全缺失；**任一题选项长度泄露（正确项 ≥ 最长干扰项 ×1.45）即判 error**，全书泄露率 >10% 另计；判断题「是/否」不严重偏置。
   - **题目 LaTeX 校验**（`title` / `options` / `parse`，含组合题子题）以下为 error：`${`（`$` 紧贴花括号，花括号不会渲染，应写成 `$\{...\}$`）、Unicode 根号 `√`（应写成 `$\sqrt{...}$`）、数学环境里的中文/全角标点未包 `\text{}`、公式花括号不配对、`\left/\right` 不配对、`\begin/\end` 不匹配、`$$` 未闭合、JSON 转义残留（解析后仍是 `\\frac`）。空公式、`$$` 与正文同行、`\tag`、行内 `$` 不成对、混用 `\(`/`\[`、单个 `*` 可能被 Markdown 渲染成斜体只告警。
   - **KaTeX 实渲染**：`validate_questions.py` 默认调用 `check_katex.py`，解析失败为 error、KaTeX 警告为 warning；本机没有 node/KaTeX 时自动跳过并提示。`--no-katex` 关闭，`--katex-path` 指定包目录。
   - **自动修正**：`fix_render.py --questions-dir "补题/题目/<id><书名>" --apply` 处理根号、中文进数学环境、`${`、`℃`、乘号星号等可确定问题，并把被 `$` 拆开的集合式（如 `$A={x$ | $x^2>0}$`）合并回一个公式；写盘前逐文件备份，之后必须重跑 `validate_questions.py`。剩余解析错误用 `fix_render_llm.py --questions-dir ... --apply` 逐条兜底（同上，需 `DEEPSEEK_API_KEY`），修完再跑一次 `validate_questions.py`。
   - 相似度 0.60–0.90 的近似题、判断题轻度偏置只报警告，不阻断阶段 4，但应在首次生成时改掉。存量旧批次复验才用 `--lenient`，它会把上述硬门槛（含选项长度泄露）降级为警告；LaTeX 与 KaTeX 渲染错误不受 `--lenient` 影响，始终为 error。

5. 第三批（新教研后台）与系统题库录入见 [manual-steps.md](references/manual-steps.md)。
6. **校验完成后问用户「要不要跑精简版？」**，要就跑阶段 5，不要就结束；不要自动跑。

## 阶段 5：精简版（可选）

输入必须是**详细版**，不是原书 `_full.md`。三步：`compact_stage.py prepare --id <id> --book "<书名>"` → `run --id <id> --yes`（默认只打印命令）→ `verify --id <id> --book "<书名>"`。产物 `exports\multi_agent\final\精简版\<id>-2 <书名>.md`，通过后 `book.json` 的 `stages.compact` 记 `done`，再传语雀「精简版」分组。

流水线默认在每章 merge 后按详细版回链图片（`--no-relink` 关闭，`--relink-dedupe` 顺带删重复图）。存量成品可单独跑 `compact_stage.py relink --id <id> [--apply] [--dedupe]`，默认只报告。

`chapter_pipeline1.py` 的参数不一样：没有 `--limit-kimi`，精简版另有 `--split-by-section`。完整命令见 **[compact.md](references/compact.md)**。

## 语雀上传

`.env` 配 `YUQUE_TOKEN`/`YUQUE_REPO`（**不要写进 skill 或 book.json**）→ `yuque_upload.py toc` 只读确认结构 → 分区 dry-run 上传，用户确认后加 `--apply`：原书（`--dir <organized 目录>`，**必须带 `--include "*_full*.md"`**）、详细版（`--section 详细版 --flat --file <单文件>`）、精简版（`--section 精简版 --flat`）。同标题默认跳过，覆盖加 `--update`。

命令全文、清单核对与报错见 **[yuque-upload.md](references/yuque-upload.md)**。

## 故障处理

卡住先查 **[troubleshooting.md](references/troubleshooting.md)**：弹窗不出、语雀 401/422、TOC 缓存与孤儿 DOC、字母引用误报、失败章节 JSON、merge 重复章/丢节、公式未修不得上题、题数明显偏少。别靠猜。

## 参考

- [question-config.md](references/question-config.md)：默认题量 60×2、配置结构、校验规则。
- [checklists.md](references/checklists.md)：拆书/详细版/上题逐项检查。
- [markdown.md](references/markdown.md)、[detail.md](references/detail.md)、[compact.md](references/compact.md)：阶段 2/3/5 全部命令。
- [yuque-upload.md](references/yuque-upload.md)、[troubleshooting.md](references/troubleshooting.md)：语雀上传、故障对照。
- [install.md](references/install.md)：仓库脚本缺失或缺参数（`--limit-merge`/`--split-by-section`）时，用包里的 `repo-addons/` 一键补齐。
- [manual-steps.md](references/manual-steps.md)：人工导入、9401、WPS、新教研后台、系统题库。
