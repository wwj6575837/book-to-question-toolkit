---
name: book-to-question
description: |
  拆书上题全流程：PDF 拆章 → 转 Markdown → 生成详细版 → 批量上题 →（可选）精简版，含语雀 token 上传。
  五个阶段可单独触发；Windows 优先；上题直接调用 补题/命题官批量生成.py（支持多轮生成 + 查重合并）。
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

把「拆书上题」的五段流程（精简版可选）固化成可重复执行的步骤。**能脚本化的都用脚本，外部系统（9401、新教研后台、系统题库）只输出人工清单。**

## 首次使用 / 给同事用

按所在仓库二选一：

- **精简仓库**（根目录有 `bootstrap.py`）：先在仓库根跑顶层自举（建 `.venv`、装最小依赖、生成配置模板）：

```powershell
python bootstrap.py
```

- **主仓库 / 已有项目**（没有顶层 `bootstrap.py`）：跑 skill 自检：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\bootstrap.py --check
```

skill 自检会检查 `补题/` 目录、命题官脚本、提示词、Python 依赖和密钥缺项。  
`题目数分配.py` 当前 skill 不需要，不会复制。

## 使用方式

先判断要做什么：

| 用户说 | 执行 |
|---|---|
| 拆书 / 拆分 PDF / 从零开始 | 阶段 0 → 1 |
| 转 Markdown / MinerU | 阶段 2（先确认阶段 1 产物） |
| 详细版 | 阶段 3 |
| 上题 / 命题官 | 阶段 4 |
| 精简版 / 压缩版 / 精简笔记 | 阶段 5（需先有详细版） |
| 全流程 | 0 → 1 → 2 → 3 → 4，门禁处停下；上题后再问是否跑阶段 5 |

开工前先读 `<书目录>/book.json`；存在就复述当前阶段状态并问是否复用。不存在就先做阶段 0。

## 路径与解释器

- 仓库根：当前工作目录（skill 所在仓库），命令都用相对路径。
- Python：不要用系统 python。Windows 用 `.venv\Scripts\python.exe`；macOS/Linux 用 `.venv/bin/python`。
- 本 skill：`.claude\skills\book-to-question\`，脚本在 `scripts\`。
- 命名别混：
  - 磁盘书目录、章节 PDF、语雀子分组：`<id>-<书名>`；
  - 补题的正文/配置/题目中间文件：`<id><书名>.md`（不带连字符）；
  - 详细版/精简版产物：`<id>-1 <书名>.md` / `<id>-2 <书名>.md`。
- organizer 用磁盘目录名当书名。
- 所有命令都在**仓库根**执行。
- 本仓库不含任何真实密钥。每个使用者自己配置：
  - `cp config.example.json config.json`，填自己的 MinerU token；
  - `.env` 里的 DeepSeek / 豆包 / 七牛 key；
  - `YUQUE_TOKEN` / `YUQUE_REPO`。
  本 skill 依赖仓库里的 `补题/`、`multi_agent/` 和提示词文件，需要同一套仓库。

### macOS / Linux 命令对照

把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`，路径分隔符换成 `/`，PowerShell 的反引号续行换成 `\`：

```bash
cd /path/to/MinerU
.venv/bin/python .claude/skills/book-to-question/scripts/markdown_stage.py convert \
  --pdf-dir "拆书/2552-市政工程识图与构造（第三版）"
.venv/bin/python .claude/skills/book-to-question/scripts/markdown_stage.py organize \
  --source "拆书/2552-市政工程识图与构造（第三版）" --backup-existing
```

详细版：

```bash
.venv/bin/python -m multi_agent.chapter_pipeline \
  --ids 2552 --auto-chunk --max-workers 6 --chapter-max-workers 7 \
  --resume-partial --skip-merge-unchanged \
  --limit-kimi 20 --limit-deepseek 15 --limit-doubao 15 \
  --retries 2 --retry-backoff 10
```

精简版（可选）：

```bash
.venv/bin/python .claude/skills/book-to-question/scripts/compact_stage.py \
  prepare --id 2552 --book "市政工程识图与构造（第三版）"
.venv/bin/python .claude/skills/book-to-question/scripts/compact_stage.py run --id 2552 --yes
.venv/bin/python .claude/skills/book-to-question/scripts/compact_stage.py \
  verify --id 2552 --book "市政工程识图与构造（第三版）"
```

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

每本书开始时一次性问清：

1. PDF 路径、书籍 id（纯数字）、书名、书目录。
2. 上题配置：每章总题量、本批题量、题型×难度配比、特殊要求。

先写 `<书目录>/book.json`（`inspect` 会保留这些字段并补页数/偏移量）：

```json
{
  "id": "1957",
  "book": "逻辑思维训练教程",
  "yuque_repo": "",
  "question": {
    "per_chapter": 120,
    "batch": 40,
    "counts": {"单选题": {"简单": 5, "中等": 3, "困难": 2}},
    "ratio": "基础题约60%，综合题30%，情景题10%",
    "special": ""
  },
  "stages": {"split": "pending", "markdown": "pending", "detail": "pending", "questions": "pending", "compact": "pending"}
}
```

上面的 `counts` 只示意一种题型；**实际必须六类齐全**（单选/多选/判断/组合/填空/简答），完整结构和默认值见 [references/question-config.md](references/question-config.md)。

`yuque_repo` 留空时读环境变量/.env 的 `YUQUE_REPO`；也可以用 `--repo` 覆盖。

然后跑：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\pdf_tools.py inspect `
  --pdf "D:\...\原书.pdf" --out-json "D:\...\<id>-<书名>\book.json"
```

`book.json` 只记事实，不写任何密钥。

## 阶段 1：拆书

详见 [references/checklists.md](references/checklists.md)。

1. **查偏移量**：`inspect` 输出总页数、标码连续性、候选偏移量。偏移量必须用户确认。
   - 扫描件没有文本层：`render --pages 1-20` 导出 PNG，读图确认。
2. **读目录**：用 Read 的 `pages` 参数读目录页（或 render 的 PNG），生成：
   - 标题 `<id>-<书名>-第N章-章节名`；
   - 最后一行为 PDF 页码范围（印刷页 + 偏移量），英文逗号分隔；
   - 写入 `<书目录>/titles.txt`。
3. **大章拆分（40 页规则）**：
   - 原章 ≤40 页：保持 `...-第N章-章名`。
   - 原章 >40 页：从目录读小节页码范围，按顺序贪心合并，使每份 ≤40 页；
     每份用**新节号 + 汇总名**命名：
     `...-第N章-章名-第1节-<汇总名>`、`...-第N章-章名-第2节-<汇总名>`。
     不再拼接原节名；汇总名要能概括合并进来的原小节。
   - 单个小节本身 >40 页：**停下问用户**。
   - 先把拆分计划（包含哪些原小节、页数、新标题）打印出来让用户确认。
4. **校验 + 拆分**：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\pdf_tools.py verify `
  --pdf "D:\...\原书.pdf" --titles-txt "D:\...\titles.txt"
```

   - 重叠/越界/超 40 页会报错；内部空隙会警告（封面/目录/附录允许）。
   - 确认后加 `--apply` 落盘；默认 dry-run。
5. **检查**：绪论/附录保留、章节数与目录一致、无遗漏。
6. **WPS 备选**：见 [references/manual-steps.md](references/manual-steps.md)。

## 阶段 2：转 Markdown

1. 转换（第一次用先 `cp config.example.json config.json`，填自己的 MinerU token）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\markdown_stage.py convert `
  --pdf-dir "D:\...\<id>-<书名>"
```

2. 组织（**门禁：目标已存在必须先问**）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\markdown_stage.py organize `
  --source "D:\...\<id>-<书名>" --backup-existing
```

   - 产物：`exports\Organized_Books\<id>-<书名>_organized\`。
   - `--backup-existing` 会把旧目录改名为 `.bak-时间戳`；用户明确同意删除时才用 `--confirm-delete`。
3. 归一化标题层级（MinerU 常把所有标题都输出成 `##`）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\normalize_headings.py `
  --dir "exports\Organized_Books\<id>-<书名>_organized" --apply
```

   - 每份只保留一个 `##` 章节标题，其余按编号深度降到 `###/####/#####`。
   - 默认 dry-run；`--apply` 会自动备份原文件。
4. 按 [references/checklists.md](references/checklists.md) 逐项检查。
4. 上传语雀原书：见下方「语雀上传」。

## 阶段 3：生成详细版

在仓库根运行：

```powershell
.venv\Scripts\python.exe -m multi_agent.chapter_pipeline `
  --ids <id> --auto-chunk --max-workers 6 --chapter-max-workers 7 `
  --resume-partial --skip-merge-unchanged `
  --limit-kimi 20 --limit-deepseek 15 --limit-doubao 15 `
  --retries 2 --retry-backoff 10
```

- 产物：`exports\multi_agent\final\书籍库\<id>-1 <书名>.md`。
- **默认不加 `--force`**；只有 organized 内容变更、且用户确认要消耗全书 API 时才加。
- 校验：`## 第X章` 数与 `book.json.chapters`/`000-文件索引.md` 一致；标题层级 `## 章` / `### 节`；无空章、无错序。

**清理与补齐（merge 经常出问题，必须做）：**

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

- 常见症状：同一章出现两次；`## 8.2` / `## # 8.1` 这类小节跑成 `##`；后面的节（如 8.7-8.9）整段消失。
- 补齐顺序：先 `clean_detail.py`，再 `supplement_detail.py`；两个都先跑 dry-run 看清单。
- 补齐来源优先看 `*.doubao_from_source.md`，其次 `*.deepseek_from_source.md`；两者都没有内容时才对照原书。

**公式渲染检查（清理补齐之后必做）：**

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\check_formulas.py `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md"
```

- ERROR（`$$` 未闭合、`$`/`\(`/`\[` 不配对、花括号或 `\begin/\end` 不平衡）必须先修，修好前不进入阶段 4。
- WARN（行内 `$` 截断、`\tag` 残留、`$` 与 `\(` 混用、`$$` 与文字同行）列入人工抽查清单。
- 只读检查，不自动改文件；`--json` 输出结构化结果，`--stats` 只看汇总。

## 阶段 4：上题（按轮次）

1. 准备正文（详细版优先，缺失回退 `_full.md`）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\prep_questions.py `
  --id <id> --book "<书名>" --book-json "D:\...\<id>-<书名>\book.json" --dry-run
```

   - 确认来源、章节数后加 `--apply`。
   - 目标：`补题\详细版\<id><书名>.md`（不带 `-1 ` 前缀）。
   - 有多个同 id 文件会停止，先消歧。
   - 回退拼接会把 `_full.md` 内部标题降两级；若提示「节标题出现在 ## 层」，先修详细版再继续。
2. 生成/检查命题配置：`补题\配置\<id><书名>.json`，结构见 [references/question-config.md](references/question-config.md)。
3. 运行（`overwrite=false`；轮数由配置里的 `rounds` 决定）：
   - `rounds: 1`：每章跑一批；
   - `rounds: 2`：每章跑两批，各自落盘到 `题目/<书>/.分次/<章节>.N.json`，再按题干相似度 ≥0.60 去重合并，适合目标 ~120 题/章。

```powershell
.venv\Scripts\python.exe 补题\命题官批量生成.py <id> `
  --config "补题\配置\<id><书名>.json"
```

   - 轮数由配置里的 `rounds` 决定；也可用 `--rounds 2` 临时覆盖。
   - 合并后默认自动均衡选项：单选答案在 A/B/C/D 间分配，多选组合打散，解析里的字母引用同步改写；无法安全改写的题保持原样并计数。先看效果加 `--audit`（不写盘），关闭用 `--no-balance`。
   - 不加 `--config` 时仍是原来的 tkinter 弹窗。
   - 输出：`补题\题目\<id><书名>\<章节>.json` + `all.json`。
4. 校验：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\validate_questions.py `
  --id <id> --book "<书名>" --config "补题\配置\<id><书名>.json"
```

   - 非 JSON 文件 = 失败章节原文；`_failed.txt` 存在则本阶段不能标记完成。
5. 第三批（新教研后台）与系统题库录入：见 [references/manual-steps.md](references/manual-steps.md)。
6. **上题校验完成后，问用户「要不要跑精简版？」**；要就跑阶段 5，不要就结束。不要自动跑。

## 阶段 5：精简版（可选）

前提：详细版 `exports\multi_agent\final\书籍库\<id>-1 <书名>.md` 已生成。  
输入必须是**详细版**，不是原书 `_full.md`；精简版提示词明确要求从详细版压缩。

1. 准备输入（详细版 → `精简版-按章/<id>/<id>-1 <书名>.md`）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\compact_stage.py `
  prepare --id <id> --book "<书名>"
```

   - 目标已存在且内容不同会拒绝；确认要更新加 `--force`（先备份）。
   - 先加 `--dry-run` 看源/目标路径。

2. 运行精简版流水线：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\compact_stage.py `
  run --id <id>
```

   - 默认只打印 `chapter_pipeline1` 命令；确认后加 `--yes` 执行。
   - 需要按 `###` 节拆分时加 `--split-by-section`（透传给 `chapter_pipeline1`）。
   - **不要照搬详细版命令**：`chapter_pipeline1.py` 没有 `--limit-kimi`。
   - 也可以直接跑：
     ```powershell
     .venv\Scripts\python.exe -m multi_agent.chapter_pipeline1 `
       --ids <id> --auto-chunk --max-workers 6 --chapter-max-workers 7 `
       --resume-partial --skip-merge-unchanged `
       --limit-deepseek 15 --limit-doubao 15 `
       --retries 2 --retry-backoff 10
     ```

3. 校验产物：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\compact_stage.py `
  verify --id <id> --book "<书名>" --book-json "<书目录>\book.json"
```

   - 产物：`exports\multi_agent\final\精简版\<id>-2 <书名>.md`。
   - 检查 `##` 数量、空章、重复标题、体积比；有警告就人工抽查。
   - 通过后 `book.json` 的 `stages.compact` 记为 `done`。

4. 上传语雀「精简版」分组（先 dry-run）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py upload `
  --section 精简版 --flat --book-id <id> --book "<书名>" `
  --file "exports\multi_agent\final\精简版\<id>-2 <书名>.md" `
  --doc-title "<id>-2 <书名>"
```

   - 确认清单后加 `--apply`。
   - 找不到「精简版」节点时先跑 `yuque_upload.py toc`，或改用 `--parent-uuid`。

## 语雀上传

**先只读拉 TOC，确认结构**（原书/详细版是同一知识库的目录还是两个库）：

先从 `.env` 配好自己的语雀 token 和知识库：

```powershell
$env:YUQUE_TOKEN = "<你的 token>"
$env:YUQUE_REPO  = "https://www.yuque.com/<user>/<repo>"
```

也可以写进仓库根 `.env`。**不要把 token 写进 skill 或 book.json。**

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py toc
```

- 原书：`upload --section 原书 --book-id <id> --book "<书名>" --dir "<仓库根>\exports\Organized_Books\<id>-<书名>_organized"`
- 详细版：`upload --section 详细版 --flat --book-id <id> --book "<书名>" --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" --doc-title "<id>-1 <书名>"`
- 省略 `--repo` 时读 `YUQUE_REPO`；两者都没有就报错，不会用任何人的默认值。
- 默认 dry-run；展示清单并获用户确认后加 `--apply`。
- 找不到「原书/详细版」节点时，用 `--parent-uuid` 指定；结构说明见 [references/manual-steps.md](references/manual-steps.md)。

## 故障处理

| 现象 | 处理 |
|---|---|
| tkinter 弹窗不出现 | 用阶段 4 的 `--config` 非交互方式 |
| 语雀 401/403 | Token 失效或缺 `repo:write/doc:write`；退回人工导入 |
| 语雀 422 | 正文含不支持的 HTML；先清洗或人工导入 |
| `find_source_md` 多命中 | 清理 `补题/详细版/`，同 id 只留一个 |
| 失败章节 JSON 不是数组 | 它存的是模型原文；确认后删除该 json 再重跑 |
| organizer 删目录 | 先 `--backup-existing`，不要直接删 |
| Windows 路径含中文/空格 | 全部加双引号；用 `-` 开头的参数放路径前 |

## 参考

- [references/checklists.md](references/checklists.md)：拆书、详细版、上题逐项检查。
- [references/manual-steps.md](references/manual-steps.md)：语雀人工导入、9401、WPS 备选、新教研后台、系统题库。
- [references/question-config.md](references/question-config.md)：命题配置结构和校验规则。
