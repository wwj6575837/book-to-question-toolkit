# 人工步骤与备选流程

> 命令以 Windows 为例；macOS/Linux 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`，
> 路径分隔符换成 `/`。

## 语雀：token 自动上传

前置：

- Token/namespace 从 `YUQUE_TOKEN` / `YUQUE_REPO`（环境变量或仓库根 `.env`）读取，或每次传 `--repo`。
- Token 需要 `repo:read / repo:write / doc:read / doc:write`。
- 脚本不会打印 token；`book.json` 只存 namespace，不存 token。

**第一步永远是只读拉 TOC**，确认真实结构：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py toc
```

- 省略 `--repo` 时读 `YUQUE_REPO`；两者都没有就报错。
- 如果「原书」「详细版」「精简版」是同一个 repo 下的 TITLE 节点：
  - 原书：`upload --section 原书`（每本书建 `<id>-<书名>` 子分组）；
  - 详细版/精简版：加 `--flat`，文档直接挂在节点下，不建子分组。
- 如果是两个独立知识库：分别用两个 `--repo`，并且不传 `--section`，改传 `--parent-uuid`。
- 找不到父节点时，脚本会列出顶层节点；把目标节点的 uuid 用 `--parent-uuid` 传入。

原书：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py upload `
  --section 原书 `
  --book-id <id> --book "<书名>" `
  --dir "<仓库根>\exports\Organized_Books\<id>-<书名>_organized"
```

详细版：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py upload `
  --section 详细版 --flat `
  --book-id <id> --book "<书名>" `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" `
  --doc-title "<id>-1 <书名>"
```

精简版（可选，上题后）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py upload `
  --section 精简版 --flat `
  --book-id <id> --book "<书名>" `
  --file "exports\multi_agent\final\精简版\<id>-2 <书名>.md" `
  --doc-title "<id>-2 <书名>"
```

- 默认 dry-run；先看清单，确认后加 `--apply`。
- 已存在的同标题文档默认跳过；要覆盖用 `--update`。
- 语雀 API 创建的文档如果被人手工编辑过，可能无法再通过 API 更新；遇到 4xx 改用人工导入。
- 找不到「精简版」节点时先跑 `yuque_upload.py toc`，或改用 `--parent-uuid`。

## 语雀：人工导入（兜底）

1. 打开「2026书籍库」。
2. 在「原书」里新建分组，名字为 `<id><书名>`（例如 `1957逻辑思维训练教程`）。
3. 点分组边上的 `+` → 导入 → 选择 Markdown → 勾选「识别 Markdown 文件中 LaTeX 公式语法」。
4. 选择 `exports\Organized_Books\<id>-<书名>_organized\` 下的 `.md` 文件（可多选，**不要** `000-文件索引.md`）。
5. 详细版：在「详细版」分组里直接导入 `exports\multi_agent\final\书籍库\<id>-1 <书名>.md`。

## 9401 后台

- 用 `exports\multi_agent\final\书籍库\<id>-1 <书名>.md` 的**原始 markdown** 上传。
- 不要先传到语雀、再从语雀复制到 9401（会丢格式/图片）。

## WPS 拆 PDF（备选）

### 方法 A：先对齐再按目录拆

1. 找到书上标码第 1 页在 PDF 中的实际页（例如第 9 页）。
2. WPS：开始 → 拆分合并 → 拆分文档，范围选「第 9 页 - 最后一页」。
3. 拆完的新 PDF 页号与书上标码对齐。
4. 对照目录按章拆；单章 >40 页按小节合并，每份 ≤40 页。
5. 按 `<id>-<书名>-第N章-章节名` 重命名。

### 方法 B：偏移量法（与 skill 的 titles.txt 一致）

1. 找到标码第 1 页的实际 PDF 页，算出偏移量。
2. 直接在原 PDF 上按 `titles.txt` 的页码范围拆分（范围是 PDF 页号）。
3. 用重命名脚本把带页码的文件改成标题名：

```powershell
.venv\Scripts\python.exe rename_split_pdfs.py `
  --titles-txt "D:\...\titles.txt" `
  --folder "D:\...\<id>-<书名>" `
  --apply --no-force-overwrite
```

- 默认不覆盖同名文件；确认可以覆盖时去掉 `--no-force-overwrite`。
- 改完再跑一次 `pdf_tools.py verify`，检查页数和范围。

## 第三批 / 额外批次（人工）

- 现在支持两种自动方式：
  - `rounds: 1`：每章一批；
  - `rounds: 2`：每章两批，各轮落在 `题目/<书>/.分次/<章节>.N.json`，再按题干相似度 ≥0.60 去重合并。
- 需要第三批或想改用网页版/新教研后台时，手工做：
  1. 打开 `md\命题官提示词系统 V3.2.md`。
  2. 用「启动指令模板」拼当前章节的配置：知识点 ID、书籍、标签、命题清单（本批题量）、配比、特殊要求。
  3. 正文用该章的详细版对应章节（或 `_full.md`）。
  4. 把配置和正文发给新教研后台 agent 或 DeepSeek 网页版；网页版没有官方 API，只能人工粘贴。
  5. 返回的 JSON 人工核对后与脚本结果合并，再录入系统题库。
- 提示词里禁止来源指代、禁止图片依赖、只返回 JSON 数组。

## 系统题库

- 目标：新教研后台的 Java 题库（具体页面以实际后台为准）。
- 导入前先跑 `validate_questions.py`，确保每章 JSON 合法、字段齐全。
- 题目 `tags` 使用「书名-第N章…」，`source` 使用「《书名》-章节」。
- 录入后抽查：题型、难度、答案、解析、标签、来源。
