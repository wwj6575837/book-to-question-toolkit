# 阶段 2：转 Markdown（完整步骤）

所有命令在**仓库根**执行。Windows 用 `.venv\Scripts\python.exe`；macOS/Linux 换成 `.venv/bin/python`、路径用 `/`、续行用 `\`。下面每步都给 Windows 版，需要时给 bash 版。

## 1. 转换（MinerU）

第一次用先 `cp config.example.json config.json`，填自己的 MinerU token。

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\markdown_stage.py convert `
  --pdf-dir "D:\...\<id>-<书名>"
```

```bash
.venv/bin/python .claude/skills/book-to-question/scripts/markdown_stage.py convert \
  --pdf-dir "拆书/2552-市政工程识图与构造（第三版）"
```

## 2. 组织（**门禁：目标已存在必须先问**）

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\markdown_stage.py organize `
  --source "D:\...\<id>-<书名>" --backup-existing
```

```bash
.venv/bin/python .claude/skills/book-to-question/scripts/markdown_stage.py organize \
  --source "拆书/2552-市政工程识图与构造（第三版）" --backup-existing
```

- 产物：`exports\Organized_Books\<id>-<书名>_organized\`。
- `--backup-existing` 会把旧目录改名为 `.bak-时间戳`；用户明确同意删除时才用 `--confirm-delete`。
- **图片去码**：上传七牛前会自动去掉书页插图里的扫码二维码（白色覆盖，编号文字保留）。
  模板在 `qrcode_templates/`，参数在 `config.json` 的 `qiniu.remove_qrcode`；
  关掉设 `"enabled": false`。换用其它二维码素材时把模板加进 `templates` 列表。
- **纯二维码图**：整张图只有二维码时（去码后非白像素 < `qrcode_only_ratio`，默认 1%），
  直接删除图片引用（连同题注），不上传、不留空白图。

## 3. 归一化标题层级

MinerU 常把所有标题都输出成 `##`：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\normalize_headings.py `
  --dir "exports\Organized_Books\<id>-<书名>_organized" --apply
```

- 每份只保留一个 `##` 章节标题，其余按编号深度降到 `###/####/#####`。
- 默认 dry-run；`--apply` 会自动备份原文件。

## 4. 检查

按 [checklists.md](checklists.md) 的阶段 2 逐项检查。

## 5. 上传语雀原书

见 [yuque-upload.md](yuque-upload.md) 的「原书」一节。

## 补处理：修复已上传图片里的二维码

转换时忘了开去码，或历史书需要补处理：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\repair_qrcode_cdn.py `
  --md-dir "exports\Organized_Books\<id>-<书名>_organized"
```

```bash
.venv/bin/python .claude/skills/book-to-question/scripts/repair_qrcode_cdn.py \
  --md-dir "exports/Organized_Books/2552-市政工程识图与构造（第三版）_organized"
```

- 扫描 md 里的 CDN 图片 URL → 下载 → 去码 → **用相同 key 覆盖上传**，URL 不变；
- 默认 dry-run，报告哪些图片命中；确认后加 `--apply --refresh`；
- `--refresh` 会刷新七牛 CDN 缓存（约 1 分钟生效），否则边缘节点仍返回旧图；
- URL 不变，语雀上的原书/详细版文档无需重传。
