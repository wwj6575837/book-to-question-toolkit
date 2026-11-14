# 语雀上传（完整步骤）

三步走：配 token → 只读拉 TOC 确认结构 → 按分区 dry-run 上传。macOS/Linux 用 `.venv/bin/python` + `/`。

## 0. 配置 token 与知识库

先从 `.env` 配好自己的语雀 token 和知识库：

```powershell
$env:YUQUE_TOKEN = "<你的 token>"
$env:YUQUE_REPO  = "https://www.yuque.com/<user>/<repo>"
```

```bash
export YUQUE_TOKEN="<你的 token>"
export YUQUE_REPO="https://www.yuque.com/<user>/<repo>"
```

也可以写进仓库根 `.env`。**不要把 token 写进 skill 或 book.json。**

## 1. 先只读拉 TOC，确认结构

原书/详细版是同一知识库的目录还是两个库：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py toc
```

## 2. 原书

脚本会先在 `原书` 下自动建子文件夹 `<id>-<书名>`，再把 `_full` 系列的最终正文传进去：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py upload `
  --section 原书 --book-id <id> --book "<书名>" `
  --dir "<仓库根>\exports\Organized_Books\<id>-<书名>_organized" `
  --include "*_full*.md"
```

- **必须带 `--include "*_full*.md"`**。organized 目录里还有 `.source.md`、
  `.merged_from_source.md`、`.deepseek_from_source.md`、`.slice.md` 等中间产物，
  省略该参数会把它们一起上传（实测一本书 96 个 md，正文只有 16 个）。
  脚本也会按 `EXCLUDE_MARKERS` 自动跳过这些中间产物，但仍建议显式写 `--include`。
- `_full` 系列包含整章 `X_full.md` 和大章拆分的 `X_full-部分N.md`，两类都要传；
  文档标题会自动去掉 `_full`（得到 `X` 或 `X-部分N`）。
- dry-run 的条目数应等于 `000-文件索引.md` 里的章节条目数。

## 3. 详细版

只上传跑出来的那一个文件，平铺在 `详细版` 下，不建子文件夹：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\yuque_upload.py upload `
  --section 详细版 --flat --book-id <id> --book "<书名>" `
  --file "exports\multi_agent\final\书籍库\<id>-1 <书名>.md" `
  --doc-title "<id>-1 <书名>"
```

不要传 `_full.md`、`.source.md` 等中间文件。

## 4. 精简版

见 [compact.md](compact.md) 第 4 步（`--section 精简版 --flat`）。

## 通用规则 

- 省略 `--repo` 时读 `YUQUE_REPO`；两者都没有就报错，不会用任何人的默认值。
- 默认 dry-run；展示清单并获用户确认后加 `--apply`（**硬门禁**）。
- 已存在的同标题文档默认跳过；确认要覆盖时加 `--update`。
- 找不到「原书/详细版」节点时，用 `--parent-uuid` 指定；结构说明见 [manual-steps.md](manual-steps.md)。
- 401/403 = Token 失效或缺 `repo:write/doc:write`；422 = 正文含不支持的 HTML，退回人工导入。
- 语雀 TOC 回读有缓存延迟：报「分组建了但找不到」时直接重跑同一命令，不要重复建分组。
