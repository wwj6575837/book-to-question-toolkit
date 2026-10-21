# book-to-question-toolkit

拆书上题工具包：PDF 拆章 → 转 Markdown → 生成详细版 → 批量上题 →（可选）精简版。

只包含这条链路需要的脚本、提示词和 Claude Code skill；不包含教材 PDF、导出产物和其他业务数据。

## 环境要求

- Python ≥ 3.12
- 能访问：`mineru.net`、`api.deepseek.com`、`ark.cn-beijing.volces.com`、`www.yuque.com`、七牛上传域名
- 需要自己准备：MinerU token、DeepSeek key、豆包/Ark key、七牛 key、语雀 token（可选 Kimi）

## 快速开始

```bash
git clone <本仓库地址> book-to-question-toolkit
cd book-to-question-toolkit
python bootstrap.py
```

`bootstrap.py` 会：

1. 检查 Python 版本；
2. 没有 `.venv` 就创建；
3. 安装 `requirements.txt` 里的最小依赖；
4. 缺 `config.json` / `.env` 时从模板复制；
5. 调用 skill 的 `bootstrap.py --check` 汇总缺项。

然后填这两个文件（不会被提交）：

- `.env`：DeepSeek / 豆包 / 七牛 / 语雀密钥
- `config.json`：MinerU `api_token`

最后**在仓库根打开 Claude Code**，`.claude/skills/book-to-question/` 才会被发现。

## 从私密文档自动配置密钥

如果手上有一份按本仓库格式写的密钥 Markdown：

```bash
python apply_keys.py --keys-md /path/to/book-to-question-keys.local.md
# 或指定目标项目
python apply_keys.py --keys-md ... --repo-root /path/to/project
```

脚本会合并 `.env`、更新 `config.json` 的 `api_token`，旧文件备份到 `.book-to-question-backup/`；
只输出键名和状态，不输出密钥值。密钥文档不要提交或上传。

## 安装到现有项目

```bash
# 只补缺失，并把内容不同的受管脚本/提示词覆盖成仓库版本（先备份）
python bootstrap.py --install-into /path/to/project

# 目标项目已有 skill，只从 GitHub 拉缺失/变更的流水线文件
python bootstrap.py --install-into /path/to/project --from-github
```

`--from-github` 默认从本精简仓库拉取；主仓库 `mineru-toolkit` 目前是私有的，
用 `--source-repo wwj6575837/mineru-toolkit` 时需要相应的访问权限。

覆盖规则：

- **受管文件**（`manifest.json` 里的脚本、提示词、skill）：缺失就补；内容不同就备份后覆盖；相同跳过。
- **用户文件**（`.env`、`config.json`、`book.json`、`exports/`、`补题/` 产物、`拆书/`）：永远不覆盖。
- 覆盖前的旧文件在 `.book-to-question-backup/<时间戳>/`。
- `--check` 只报告差异；`--no-overwrite` 只补缺失；`--no-install` 不装依赖；`--no-venv` 不建 venv。

## 使用流程

在 Claude Code 里直接说「拆书」「转 Markdown」「详细版」「上题」「精简版」，或查看：

- `.claude/skills/book-to-question/SKILL.md`
- `.claude/skills/book-to-question/references/`

阶段顺序：

1. 拆书（`pdf_tools.py`）
2. 转 Markdown（`run_converter.py` + `advanced_markdown_reorganizer.py`）
3. 详细版（`chapter_pipeline.py`）
4. 上题（`补题/命题官批量生成.py`）
5. 精简版（`chapter_pipeline1.py`，可选，上题后询问）

## 数据去向

跑流程时，数据会分别送到这些外部服务，使用前请确认可以接受：

| 服务 | 收到什么 | 用途 |
|---|---|---|
| MinerU（`mineru.net`） | 上传的 PDF | 版面解析、OCR、图片/表格提取 |
| DeepSeek / 豆包 / Kimi | 章节正文或题目上下文 | 生成详细版、精简版、题目 |
| 七牛云 | Markdown 里引用的图片 | 上传后替换为 CDN 链接 |
| 语雀 | 原书/详细版/精简版 Markdown 文档 | 上传到指定知识库分组 |

拆书产物、缓存、`book.json`、题目 JSON 都只留在本地；`.env` 和 `config.json` 不会被提交。

## 最常卡住的三件事

1. **路径与命名**：所有命令都在仓库根执行；书目录必须是 `<id>-<书名>`。路径含中文/空格时加引号。
2. **密钥与 token**：MinerU `api_token` 在 `config.json`，LLM/七牛/语雀在 `.env`。语雀 401/403 通常是 token 失效或缺 `repo:write`/`doc:write` 权限。
3. **已有产物**：`organize` 遇到已存在的 `_organized` 目录、详细版已存在、题目已生成时都会停下。按提示用 `--backup-existing`，或确认后再加 `--force` / `--overwrite`。

## 版本与同步

部分脚本来自主仓库 `wwj6575837/mineru-toolkit`。本仓库是拆书上题链路的精简副本；主仓库更新后需要同步对应文件。`manifest.json` 列出了受管文件清单。
