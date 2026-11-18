# 阶段 5：精简版（可选，完整步骤）

前提：详细版 `exports\multi_agent\final\书籍库\<id>-1 <书名>.md` 已生成。  
输入必须是**详细版**，不是原书 `_full.md`；精简版提示词明确要求从详细版压缩。

在**仓库根**运行；macOS/Linux 用 `.venv/bin/python` + `/` + `\` 续行。

## 1. 准备输入

详细版 → `精简版-按章/<id>/<id>-1 <书名>.md`：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\compact_stage.py `
  prepare --id <id> --book "<书名>"
```

- 目标已存在且内容不同会拒绝；确认要更新加 `--force`（先备份）。
- 先加 `--dry-run` 看源/目标路径。

## 2. 运行精简版流水线

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\compact_stage.py `
  run --id <id>
```

- 默认只打印 `chapter_pipeline1` 命令；确认后加 `--yes` 执行。
- 需要按 `###` 节拆分时加 `--split-by-section`（透传给 `chapter_pipeline1`）。
- **不要照搬详细版命令**：`chapter_pipeline1.py` 没有 `--limit-kimi`；且两个脚本的 `--limit-kimi` 都已失效（Kimi 生成默认关闭），别再写进命令。
- 旧仓库报 `unrecognized arguments`（`--split-by-section` 或 `--limit-merge`）时，先跑 `scripts/install.py` 补齐（见 [install.md](install.md)）；`compact_stage.py run` 会自动过滤不支持的参数。
- 也可以直接跑：

```powershell
.venv\Scripts\python.exe -m multi_agent.chapter_pipeline1 `
  --ids <id> --auto-chunk --max-workers 6 --chapter-max-workers 7 `
  --resume-partial --skip-merge-unchanged `
  --limit-deepseek 15 --limit-doubao 15 --limit-merge 8 `
  --retries 2 --retry-backoff 10
```

```bash
.venv/bin/python .claude/skills/book-to-question/scripts/compact_stage.py prepare \
  --id 2552 --book "市政工程识图与构造（第三版）"
.venv/bin/python .claude/skills/book-to-question/scripts/compact_stage.py run --id 2552 --yes
```

## 3. 校验产物

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\compact_stage.py `
  verify --id <id> --book "<书名>" --book-json "<书目录>\book.json"
```

- 产物：`exports\multi_agent\final\精简版\<id>-2 <书名>.md`。
- 检查 `##` 数量、空章、重复标题、体积比；有警告就人工抽查。

再做一次节级覆盖检查，确认精简版保留了详细版的每一节：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\check_coverage.py `
  --detail "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" `
  --compact "exports\multi_agent\final\精简版\<id>-2 <书名>.md"
```

- 详细版有的节在精简版找不到就是 error；精简版节长度 <详细版 10% 告警。
- 通过后 `book.json` 的 `stages.compact` 记为 `done`。

## 3b. 图片按详细版回链

流水线在每章 merge 后自动回链（`--no-relink` 关闭，`--relink-dedupe` 顺带删重复图）。存量成品可单独跑：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\compact_stage.py `
  relink --id <id> --book "<书名>"
```

- 默认 dry-run，只报告错链、重复图、详细版有而精简版无的图、无法对齐项。
- 确认后加 `--apply` 写回（先备份 `.bak-relink-<时间戳>`）；`--dedupe` 才删除重复图。
- 逐章按 `##` 标题配对；标题对不上时退化为整文件回链并打印低置信提示。

## 4. 上传语雀「精简版」分组

先 dry-run，确认清单后加 `--apply`：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py upload `
  --section 精简版 --flat --book-id <id> --book "<书名>" `
  --file "exports\multi_agent\final\精简版\<id>-2 <书名>.md" `
  --doc-title "<id>-2 <书名>"
```

- 找不到「精简版」节点时先跑 `yuque_upload.py toc`，或改用 `--parent-uuid`。
