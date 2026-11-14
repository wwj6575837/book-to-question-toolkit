# 附带脚本（repo-addons）—— 装进仓库的说明

本目录是 skill 运行所需的**仓库侧脚本**，按仓库相对路径镜像。skill 本身只带 `scripts/`，这些文件必须在仓库里存在且为新版，阶段 2/3/4/5 才能跑。

## 内容

| 目录 | 文件 | 用途 |
|---|---|---|
| `multi_agent/` | `chapter_pipeline.py` `chapter_pipeline1.py` `utils.py` `agent_runner.py` `ensemble_merge.py` `__init__.py` | 阶段 3 / 5 流水线（含 `--limit-merge`、`--split-by-section`） |
| `multi_agent/prompts/` | 7 份 agent 提示词 | 流水线按名读取 |
| `md/` | 知识点总结 V5.1、精简版提示词1、标题层级排序、命题官 V3.2、命题 V1.2、教材精简版、题目审查 | 各阶段提示词 |
| `补题/` | `命题官批量生成.py` `拆分章节.py` | 阶段 4 批量上题、阶段 1 拆章 |
| 根目录 | `run_converter.py` `mineru_batch_converter.py` `advanced_markdown_reorganizer.py` `project_dotenv.py` `qiniu_markdown_images.py` `qrcode_removal.py` | 阶段 2 转换与图片处理 |
| 根目录 | `config.example.json` `.env.example` | 配置模板（不含真 key） |
| `qrcode_templates/` | `maiziedu.png` | 去二维码模板 |

**不含**业务数据：`拆书/`、`补题/题目/`、`exports/`、`progress.json`。

## 安装

在仓库根执行（skill 装在 `.claude/skills/book-to-question` 时可省略 `--repo`）：

```powershell
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\install.py --dry-run
.venv\Scripts\python.exe .claude\skills\book-to-question\scripts\install.py
```

规则：缺失→新增；内容不同→备份到 `.book-to-question-backup\install-<时间戳>\` 后替换；相同→跳过。二次运行为全 `SAME`。

装完用 `scripts/preflight.py` 自检，并用 `--emit detail|compact` 取本仓库可用的命令。

## 回滚

把 `.book-to-question-backup\install-<时间戳>\` 下的文件按相对路径拷回仓库。
